"""Redis-backed v2 turn ledger. Lease check, messages, state and receipt commit atomically."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import secrets
import time
from datetime import datetime
import redis.asyncio as redis

from core.contracts import TaskState
from core.runtime import ConversationBusy, LeaseLost, RequestConflict


COMMIT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return -1 end
local receipt = redis.call('GET', KEYS[4])
if receipt then return 0 end
redis.call('LPUSH', KEYS[2], ARGV[2], ARGV[3])
redis.call('EXPIRE', KEYS[2], ARGV[6])
redis.call('SET', KEYS[3], ARGV[4], 'EX', ARGV[6])
redis.call('SET', KEYS[4], ARGV[5], 'EX', ARGV[6])
redis.call('SADD', KEYS[5], KEYS[4])
redis.call('EXPIRE', KEYS[5], ARGV[6])
return 1
"""
RELEASE = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0"


class TurnStore:
    TTL = 86400

    def __init__(self, url: str):
        self.client = redis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)

    @staticmethod
    def key(kind: str, user_id: str, conv_id: str) -> str:
        identity = json.dumps([user_id, conv_id], ensure_ascii=False, separators=(',', ':'))
        digest = hashlib.sha256(identity.encode()).hexdigest()
        return f'v2:{{{digest}}}:{kind}'

    def receipt_key(self, user_id, conv_id, request_id):
        return self.key('receipt:' + hashlib.sha256(request_id.encode()).hexdigest(), user_id, conv_id)

    @staticmethod
    def message_hash(message):
        return hashlib.sha256(message.encode()).hexdigest()

    async def receipt(self, user_id, conv_id, request_id, message):
        value = await self.client.get(self.receipt_key(user_id, conv_id, request_id))
        if not value:
            return None
        data = json.loads(value)
        if data['message_hash'] != self.message_hash(message):
            raise RequestConflict('同一 client_request_id 不能用于不同消息')
        return data['result']

    async def state(self, user_id, conv_id):
        value = await self.client.get(self.key('state', user_id, conv_id))
        return TaskState.model_validate_json(value) if value else TaskState()

    @asynccontextmanager
    async def guard(self, user_id, conv_id, lease_s=60, wait_s=3):
        key = self.key('lease', user_id, conv_id)
        token = secrets.token_hex(24)
        deadline = time.monotonic() + wait_s
        while not await self.client.set(key, token, nx=True, px=max(1, int(lease_s * 1000))):
            if time.monotonic() >= deadline:
                raise ConversationBusy('会话正在处理，请使用相同请求 ID 稍后重试')
            await asyncio.sleep(0.05)
        try:
            yield token
        finally:
            try:
                await self.client.eval(RELEASE, 1, key, token)
            except Exception:
                pass  # Expiry releases an orphaned lease; commit always checks ownership.

    async def commit(self, user_id, conv_id, request_id, message, result, state, token):
        now = datetime.now().isoformat()
        def raw(role, content):
            return json.dumps({'role': role, 'content': content, 'ts': now,
                               'metadata': {'request_id': request_id}}, ensure_ascii=False)
        receipt = json.dumps({'message_hash': self.message_hash(message), 'result': result}, ensure_ascii=False)
        keys = [self.key(k, user_id, conv_id) for k in ('lease', 'wm', 'state')]
        keys += [self.receipt_key(user_id, conv_id, request_id), self.key('receipts', user_id, conv_id)]
        outcome = await self.client.eval(COMMIT, len(keys), *keys, token, raw('user', message),
                                         raw('assistant', result['response']), state.model_dump_json(), receipt, self.TTL)
        if outcome == -1:
            raise LeaseLost('会话租约已失效，本轮未提交')
        return outcome == 1

    async def clear(self, user_id, conv_id):
        async with self.guard(user_id, conv_id):
            index = self.key('receipts', user_id, conv_id)
            receipts = await self.client.smembers(index)
            await self.client.delete(index, self.key('wm', user_id, conv_id),
                                     self.key('summary', user_id, conv_id), self.key('state', user_id, conv_id), *receipts)

    async def create_session(self):
        token, user_id = secrets.token_urlsafe(32), 'anon_' + secrets.token_hex(16)
        await self.client.setex('v2:session:' + hashlib.sha256(token.encode()).hexdigest(), self.TTL, user_id)
        return {'session_token': token, 'user_id': user_id, 'expires_in': self.TTL}

    async def session_user(self, token):
        if not token or len(token) > 200:
            return None
        return await self.client.get('v2:session:' + hashlib.sha256(token.encode()).hexdigest())

    async def close(self):
        await self.client.aclose()


class MaintenanceQueue:
    """Bounded best-effort maintenance; no unbounded create_task per request."""
    def __init__(self, handler, capacity=32, workers=2):
        self.handler, self.workers = handler, workers
        self.queue = asyncio.Queue(maxsize=capacity)
        self.pending = set()
        self.tasks = []

    def start(self):
        self.tasks = [asyncio.create_task(self._worker()) for _ in range(self.workers)]

    def submit(self, user_id, conv_id):
        key = (user_id, conv_id)
        if key in self.pending or self.queue.full():
            return False
        self.pending.add(key)
        self.queue.put_nowait(key)
        return True

    async def _worker(self):
        while True:
            key = await self.queue.get()
            try:
                await asyncio.wait_for(self.handler(*key), 40)
            except Exception:
                pass  # Best effort; the committed conversation remains intact.
            finally:
                self.pending.discard(key)
                self.queue.task_done()

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
