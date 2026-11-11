"""A request-scoped, fenced MySQL LangGraph saver; no object deserialization.

Only the root namespace is supported because this graph has no nested subgraphs.
The normal LangGraph async checkpoint/pending-write contract is implemented. This
saver is never exposed as a user-selected thread reader or arbitrary DB browser.
"""
import json
import time

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, WRITES_IDX_MAP

from core.runtime import RequestConflict


TABLES = ('graph_runs', 'graph_checkpoints', 'graph_writes')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


class JsonOnlySerializer:
    def dumps_typed(self, value):
        return 'json', encoded(value).encode('utf-8')

    def loads_typed(self, value):
        kind, raw = value
        if kind != 'json':
            raise ValueError('non_json_checkpoint_rejected')
        return json.loads(raw)


class MySQLCheckpointSaver(BaseCheckpointSaver):
    def __init__(self, turns, user, conv, request, message, token, run, *, version):
        super().__init__(serde=JsonOnlySerializer())
        self.turns, self.db, self.run = turns, turns.db, run
        self.user, self.conv, self.request, self.message, self.token = user, conv, request, message, token
        self.key, self.rkey = turns.identity(user, conv), turns.request_hash(request)
        self.thread_id = self.key + ':' + self.rkey
        self.version = version
        self.runs, self.checkpoints, self.writes = [self.db.table(t) for t in TABLES]
        self.original_trace_id = run.run_id
        self.base_revision = None

    @property
    def config(self):
        return {'configurable': {'thread_id': self.thread_id, 'checkpoint_ns': ''}, 'recursion_limit': 20}

    def _validate(self, config):
        c = config.get('configurable', {})
        if c.get('thread_id') != self.thread_id or c.get('checkpoint_ns', '') != '':
            raise ValueError('checkpoint_scope_mismatch')
        return c

    async def _fence(self, conn):
        current = await self.turns._row(conn, self.key, self.token)
        request = await self.turns._request(conn, self.key, self.request, self.message)
        if not request or request['status'] != 'pending' or request['base_revision'] != current['revision']:
            raise RequestConflict('工作流请求状态已经改变')
        if self.base_revision is not None and current['revision'] != self.base_revision:
            raise RequestConflict('工作流会话版本已经改变')
        return current

    async def prepare(self):
        """Reuse only this same request, workflow schema and still-current task state."""
        async with self.db.connection() as conn:
            current = await self._fence(conn)
            self.base_revision = current['revision']
            row = await (await conn.execute(f'SELECT *, (created_at > UTC_TIMESTAMP(6)-INTERVAL 1 HOUR) AS fresh '
                f'FROM {self.runs} WHERE conversation_key=%s AND request_hash=%s', (self.key, self.rkey))).fetchone()
            reusable = bool(row and row['fresh'] and row['base_revision'] == self.base_revision
                            and row['workflow_version'] == self.version)
            if row:
                # Invalid checkpoints must not turn a reused request ID into a
                # fresh model/tool budget. Only explicit conversation clear deletes it.
                self.original_trace_id = row['trace_id']
                usage = json.loads(row['usage_json'])
                for key in ('tool_calls', 'model_calls', 'input_tokens', 'output_tokens'):
                    setattr(self.run, key, max(getattr(self.run, key), int(usage.get(key, 0))))
                self.run.started -= max(0, usage.get('active_elapsed_ms', 0)) / 1000
                if reusable:
                    self.run.memory_revision = usage.get('memory_revision')
                self.run.events.append({'stage': 'workflow_resume' if reusable else 'workflow_checkpoint_invalidated',
                                        'source_run_id': self.original_trace_id})
                if not reusable:
                    await self._delete(conn, keep_usage=True)
                    await conn.execute(f'UPDATE {self.runs} SET base_revision=%s,workflow_version=%s,'
                        'usage_json=%s,created_at=UTC_TIMESTAMP(6),updated_at=UTC_TIMESTAMP(6) '
                        'WHERE conversation_key=%s AND request_hash=%s',
                        (self.base_revision, self.version, encoded(self._usage()), self.key, self.rkey))
            else:
                await conn.execute(f'INSERT INTO {self.runs} '
                    '(conversation_key,request_hash,base_revision,workflow_version,trace_id,usage_json) VALUES (%s,%s,%s,%s,%s,%s)',
                    (self.key, self.rkey, self.base_revision, self.version, self.original_trace_id, encoded(self._usage())))
        self.run.check_deadline()
        return reusable

    def _usage(self):
        return {**{name: int(getattr(self.run, name)) for name in ('tool_calls', 'model_calls', 'input_tokens', 'output_tokens')},
                'active_elapsed_ms': round((time.monotonic() - self.run.started) * 1000, 3),
                'memory_revision': self.run.memory_revision}

    async def _write_usage(self, conn):
        await conn.execute(f'UPDATE {self.runs} SET usage_json=%s,updated_at=UTC_TIMESTAMP(6) '
            'WHERE conversation_key=%s AND request_hash=%s', (encoded(self._usage()), self.key, self.rkey))

    async def save_usage(self, run=None):
        # Called immediately after reserving a model/tool call, before external work.
        async with self.db.connection() as conn:
            await self._fence(conn)
            await self._write_usage(conn)

    async def _delete(self, conn, *, keep_usage=False):
        for table in (self.writes, self.checkpoints) if keep_usage else (self.writes, self.checkpoints, self.runs):
            await conn.execute(f'DELETE FROM {table} WHERE conversation_key=%s AND request_hash=%s', (self.key, self.rkey))

    def _config(self, checkpoint_id):
        return {'configurable': {'thread_id': self.thread_id, 'checkpoint_ns': '', 'checkpoint_id': checkpoint_id}}

    async def aget_tuple(self, config):
        c = self._validate(config)
        async with self.db.connection() as conn:
            await self._fence(conn)
            sql = f'SELECT * FROM {self.checkpoints} WHERE conversation_key=%s AND request_hash=%s'
            params = [self.key, self.rkey]
            if c.get('checkpoint_id'):
                sql += ' AND checkpoint_id=%s'
                params.append(c['checkpoint_id'])
            row = await (await conn.execute(sql + ' ORDER BY checkpoint_id DESC LIMIT 1', tuple(params))).fetchone()
            if not row:
                return None
            writes = await (await conn.execute(f'SELECT task_id,channel,value_json FROM {self.writes} '
                'WHERE conversation_key=%s AND request_hash=%s AND checkpoint_id=%s ORDER BY task_id,write_index',
                (self.key, self.rkey, row['checkpoint_id']))).fetchall()
        return CheckpointTuple(config=self._config(row['checkpoint_id']), checkpoint=json.loads(row['checkpoint_json']),
            metadata=json.loads(row['metadata_json']),
            parent_config=self._config(row['parent_id']) if row['parent_id'] else None,
            pending_writes=[(r['task_id'], r['channel'], json.loads(r['value_json'])) for r in writes])

    async def alist(self, config, *, filter=None, before=None, limit=None):
        self._validate(config or self.config)
        if before:
            self._validate(before)
        async with self.db.connection() as conn:
            await self._fence(conn)
            rows = await (await conn.execute(f'SELECT checkpoint_id,metadata_json FROM {self.checkpoints} '
                'WHERE conversation_key=%s AND request_hash=%s ORDER BY checkpoint_id DESC', (self.key, self.rkey))).fetchall()
        count = 0
        for row in rows:
            if before and row['checkpoint_id'] >= before['configurable']['checkpoint_id']:
                continue
            metadata = json.loads(row['metadata_json'])
            if filter and any(metadata.get(k) != v for k, v in filter.items()):
                continue
            if limit is not None and count >= limit:
                break
            yield await self.aget_tuple(self._config(row['checkpoint_id']))
            count += 1

    async def aput(self, config, checkpoint, metadata, new_versions):
        c = self._validate(config)
        # JSON encoding happens before SQL. Arbitrary objects cannot reach deserialization.
        payload, meta = encoded(checkpoint), encoded(metadata)
        async with self.db.connection() as conn:
            await self._fence(conn)
            await conn.execute(f'INSERT INTO {self.checkpoints} '
                '(conversation_key,request_hash,checkpoint_id,parent_id,checkpoint_json,metadata_json) VALUES (%s,%s,%s,%s,%s,%s) '
                'ON DUPLICATE KEY UPDATE checkpoint_id=checkpoint_id',
                (self.key, self.rkey, checkpoint['id'], c.get('checkpoint_id'), payload, meta))
            await self._write_usage(conn)
        return self._config(checkpoint['id'])

    async def aput_writes(self, config, writes, task_id, task_path=''):
        c = self._validate(config)
        async with self.db.connection() as conn:
            await self._fence(conn)
            for position, (channel, value) in enumerate(writes):
                index = WRITES_IDX_MAP.get(channel, position)
                payload = encoded(value)
                prior = await (await conn.execute(f'SELECT 1 FROM {self.writes} WHERE conversation_key=%s '
                    'AND request_hash=%s AND checkpoint_id=%s AND task_id=%s AND write_index=%s',
                    (self.key, self.rkey, c['checkpoint_id'], task_id, index))).fetchone()
                if prior:
                    if index < 0:
                        await conn.execute(f'UPDATE {self.writes} SET channel=%s,value_json=%s WHERE conversation_key=%s '
                            'AND request_hash=%s AND checkpoint_id=%s AND task_id=%s AND write_index=%s',
                            (channel, payload, self.key, self.rkey, c['checkpoint_id'], task_id, index))
                else:
                    await conn.execute(f'INSERT INTO {self.writes} '
                        '(conversation_key,request_hash,checkpoint_id,task_id,write_index,channel,value_json) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                        (self.key, self.rkey, c['checkpoint_id'], task_id, index, channel, payload))
            await self._write_usage(conn)

    async def adelete_thread(self, thread_id):
        self._validate({'configurable': {'thread_id': thread_id}})
        async with self.db.connection() as conn:
            await self._fence(conn)
            await self._delete(conn)

    def get_next_version(self, current, channel):
        return (current or 0) + 1
