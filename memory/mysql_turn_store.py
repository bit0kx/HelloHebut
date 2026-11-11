"""MySQL turn authority, fenced leases and one safe post-generation recovery point.

Redis remains the identity provider and a read-only source for one-time legacy import.
No database failure falls back to Redis writes. Not a LangGraph checkpointer.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import time

from core.contracts import TaskState
from core.runtime import ConversationBusy, LeaseLost, RequestConflict
from memory.mysql_database import MySQLDatabase
from memory.turn_store import TurnStore


LEGACY_SNAPSHOT = """
local receipts = {}
for _, key in ipairs(redis.call('SMEMBERS', KEYS[4])) do
    local value = redis.call('GET', key)
    if value then table.insert(receipts, {key, value}) end
end
return {redis.call('GET', KEYS[1]) or '', redis.call('GET', KEYS[2]) or '',
        redis.call('LRANGE', KEYS[3], 0, -1), receipts}
"""


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class MySQLTurnStore(TurnStore):
    durable = True

    def __init__(self, url, *, database=None):
        super().__init__(url)
        self.db = database or MySQLDatabase()
        self.conversations = self.db.table('chat_conversations')
        self.requests = self.db.table('chat_requests')

    async def initialize(self):
        await self.db.initialize_tables(Path(__file__).parent/'migrations/002_turns.sql',
                                        ('chat_conversations', 'chat_requests'))

    @staticmethod
    def identity(user, conv):
        return hashlib.sha256(dump([user, conv]).encode()).hexdigest()

    @staticmethod
    def request_hash(request):
        return hashlib.sha256(request.encode()).hexdigest()

    async def _ensure(self, user, conv):
        key = self.identity(user, conv)
        async with self.db.connection() as conn:
            await conn.execute(f'INSERT INTO {self.conversations} '
                '(conversation_key,state_json,messages_json,summary) VALUES (%s,%s,%s,%s) '
                'ON DUPLICATE KEY UPDATE conversation_key=conversation_key',
                (key, TaskState().model_dump_json(), '[]', ''))
            row = await self._row(conn, key)
            if row['legacy_imported']:
                return key
            # The old application must be stopped before switching authority. Lua captures
            # the surviving Redis state/messages/summary/receipt index at one instant.
            old_state, summary, raws, receipts = await self.client.eval(LEGACY_SNAPSHOT, 4,
                *[self.key(kind, user, conv) for kind in ('state', 'summary', 'wm', 'receipts')])
            state = self.decode_state(old_state) if old_state else TaskState()
            messages = [json.loads(raw) for raw in reversed(raws)]
            for receipt_key, raw in receipts:
                data = json.loads(raw)
                request_hash = receipt_key.rsplit(':', 1)[-1]
                if len(request_hash) != 64 or any(c not in '0123456789abcdef' for c in request_hash):
                    raise ValueError('invalid_legacy_receipt_key')
                await conn.execute(f'INSERT INTO {self.requests} '
                    '(conversation_key,request_hash,message_hash,status,base_revision,result_json,memory_revision) '
                    'VALUES (%s,%s,%s,%s,%s,%s,%s)',
                    (key, request_hash, data['message_hash'], 'committed', 0, dump(data['result']), data.get('memory_revision')))
            await conn.execute(f'UPDATE {self.conversations} SET state_json=%s,messages_json=%s,summary=%s,'
                'revision=%s,legacy_imported=TRUE WHERE conversation_key=%s',
                (state.model_dump_json(), dump(messages), summary, state.revision, key))
        return key

    async def _row(self, conn, key, token=None):
        row = await (await conn.execute(f'SELECT *, '
            f'(lease_until > UTC_TIMESTAMP(6)) AS lease_valid FROM {self.conversations} '
            'WHERE conversation_key=%s FOR UPDATE', (key,))).fetchone()
        if token is not None and (not row or row['lease_token'] != token or not row['lease_valid']):
            raise LeaseLost('会话租约已失效，本轮未提交')
        return row

    async def _request(self, conn, key, request_id, message=None):
        row = await (await conn.execute(f'SELECT *, '
            '(updated_at > UTC_TIMESTAMP(6)-INTERVAL 1 HOUR) AS checkpoint_fresh '
            f'FROM {self.requests} WHERE conversation_key=%s AND request_hash=%s',
            (key, self.request_hash(request_id)))).fetchone()
        if row and message is not None and row['message_hash'] != self.message_hash(message):
            raise RequestConflict('同一 client_request_id 不能用于不同消息')
        return row

    async def receipt_record(self, user_id, conv_id, request_id):
        key = await self._ensure(user_id, conv_id)
        async with self.db.connection() as conn:
            row = await self._request(conn, key, request_id)
        if not row or row['status'] != 'committed':
            return None
        return {'message_hash': row['message_hash'], 'result': json.loads(row['result_json']),
                'memory_revision': row['memory_revision']}

    async def receipt(self, user_id, conv_id, request_id, message):
        key = await self._ensure(user_id, conv_id)
        async with self.db.connection() as conn:
            row = await self._request(conn, key, request_id, message)
        return json.loads(row['result_json']) if row and row['status'] == 'committed' else None

    async def state(self, user_id, conv_id):
        key = await self._ensure(user_id, conv_id)
        async with self.db.connection() as conn:
            row = await self._row(conn, key)
        return self.decode_state(row['state_json'])

    @asynccontextmanager
    async def guard(self, user_id, conv_id, lease_s=60, wait_s=3):
        key = await self._ensure(user_id, conv_id)
        token, deadline = secrets.token_hex(24), time.monotonic() + wait_s
        while True:
            async with self.db.connection() as conn:
                row = await self._row(conn, key)
                available = not row['lease_token'] or not row['lease_valid']
                if available:
                    await conn.execute(f'UPDATE {self.conversations} SET lease_token=%s,'
                        'lease_until=TIMESTAMPADD(MICROSECOND,%s,UTC_TIMESTAMP(6)) WHERE conversation_key=%s',
                        (token, max(1, int(lease_s * 1_000_000)), key))
            if available:
                break
            if time.monotonic() >= deadline:
                raise ConversationBusy('会话正在处理，请使用相同请求 ID 稍后重试')
            await asyncio.sleep(.05)
        try:
            yield token
        finally:
            try:
                async with self.db.connection() as conn:
                    await conn.execute(f'UPDATE {self.conversations} SET lease_token=NULL,lease_until=NULL '
                        'WHERE conversation_key=%s AND lease_token=%s', (key, token))
            except Exception:
                pass  # Server-clock expiry releases orphaned work; every write is fenced.

    async def begin_request(self, user_id, conv_id, request_id, message, token):
        key = self.identity(user_id, conv_id)
        async with self.db.connection() as conn:
            current = await self._row(conn, key, token)
            row = await self._request(conn, key, request_id, message)
            if row and row['status'] == 'computed' and row['base_revision'] == current['revision'] and row['checkpoint_fresh']:
                return {'result': json.loads(row['result_json']), 'state': json.loads(row['state_json']),
                        'memory_revision': row['memory_revision']}
            if row and row['status'] == 'committed':
                raise RequestConflict('请求已提交，请重试读取回执')
            if row:
                await conn.execute(f'UPDATE {self.requests} SET status=%s,base_revision=%s,result_json=NULL,'
                    'state_json=NULL,memory_revision=NULL,updated_at=UTC_TIMESTAMP(6) '
                    'WHERE conversation_key=%s AND request_hash=%s',
                    ('pending', current['revision'], key, self.request_hash(request_id)))
            else:
                await conn.execute(f'INSERT INTO {self.requests} '
                    '(conversation_key,request_hash,message_hash,message_text,status,base_revision) '
                    'VALUES (%s,%s,%s,%s,%s,%s)',
                    (key, self.request_hash(request_id), self.message_hash(message), message, 'pending', current['revision']))

    async def save_checkpoint(self, user_id, conv_id, request_id, message, result, state, token, memory_revision=None):
        key = self.identity(user_id, conv_id)
        async with self.db.connection() as conn:
            current = await self._row(conn, key, token)
            row = await self._request(conn, key, request_id, message)
            if not row or row['status'] != 'pending' or row['base_revision'] != current['revision']:
                raise RequestConflict('请求状态已变化，不能保存过期回答')
            if state.revision != self.decode_state(current['state_json']).revision + 1:
                raise RequestConflict('任务状态版本不连续')
            await conn.execute(f'UPDATE {self.requests} SET status=%s,result_json=%s,state_json=%s,'
                'memory_revision=%s,updated_at=UTC_TIMESTAMP(6) WHERE conversation_key=%s AND request_hash=%s',
                ('computed', dump(result), state.model_dump_json(), memory_revision, key, self.request_hash(request_id)))

    async def commit(self, user_id, conv_id, request_id, message, result, state, token, memory_revision=None):
        key = self.identity(user_id, conv_id)
        async with self.db.connection() as conn:
            current = await self._row(conn, key, token)
            row = await self._request(conn, key, request_id, message)
            if row and row['status'] == 'committed':
                return False
            if (not row or row['status'] != 'computed' or row['base_revision'] != current['revision']
                    or json.loads(row['result_json']) != result
                    or json.loads(row['state_json']) != state.model_dump()
                    or row['memory_revision'] != memory_revision):
                raise RequestConflict('回答快照已变化，不能提交')
            messages = json.loads(current['messages_json'])
            now = datetime.now(timezone.utc).isoformat()
            messages.extend({'role': role, 'content': content, 'ts': now, 'metadata': {'request_id': request_id}}
                            for role, content in [('user', message), ('assistant', result['response'])])
            await conn.execute(f'UPDATE {self.conversations} SET messages_json=%s,state_json=%s,revision=revision+1 '
                'WHERE conversation_key=%s', (dump(messages), state.model_dump_json(), key))
            await self._mark_committed(conn, key, self.request_hash(request_id))
        return True

    async def _mark_committed(self, conn, conversation_key, request_hash):
        await conn.execute(f'UPDATE {self.requests} SET status=%s,updated_at=UTC_TIMESTAMP(6) '
            'WHERE conversation_key=%s AND request_hash=%s', ('committed', conversation_key, request_hash))

    async def working_snapshot(self, user_id, conv_id):
        key = await self._ensure(user_id, conv_id)
        async with self.db.connection() as conn:
            row = await self._row(conn, key)
        return {'messages': json.loads(row['messages_json']), 'summary': row['summary'], 'summary_epoch': row['summary_epoch']}

    async def compress(self, user_id, conv_id, snapshot, cut, summary):
        if not summary or not 0 < cut < len(snapshot['messages']):
            return False
        key = self.identity(user_id, conv_id)
        async with self.db.connection() as conn:
            row = await self._row(conn, key)
            latest = json.loads(row['messages_json'])
            if (row['summary_epoch'] != snapshot['summary_epoch'] or row['summary'] != snapshot['summary']
                    or latest[:len(snapshot['messages'])] != snapshot['messages']):
                return False
            await conn.execute(f'UPDATE {self.conversations} SET messages_json=%s,summary=%s,summary_epoch=summary_epoch+1 '
                'WHERE conversation_key=%s', (dump(latest[cut:]), summary, key))
        return True

    async def clear(self, user_id, conv_id):
        key = self.identity(user_id, conv_id)
        async with self.guard(user_id, conv_id) as token:
            async with self.db.connection() as conn:
                await self._row(conn, key, token)
                await conn.execute(f'DELETE FROM {self.requests} WHERE conversation_key=%s', (key,))
                await conn.execute(f'UPDATE {self.conversations} SET messages_json=%s,state_json=%s,summary=%s,'
                    'revision=revision+1,summary_epoch=summary_epoch+1 WHERE conversation_key=%s',
                    ('[]', TaskState().model_dump_json(), '', key))
        # Keep the imported tombstone; stale Redis is never an authority again.

    async def drop_test_tables(self):
        import re
        if not re.fullmatch(r'test_[a-z_]+_[0-9a-f]{32}', self.db.schema):
            raise ValueError('test_namespace_required')
        async with self.db.connection() as conn:
            await conn.execute(f'DROP TABLE IF EXISTS {self.requests}')
            await conn.execute(f'DROP TABLE IF EXISTS {self.conversations}')
