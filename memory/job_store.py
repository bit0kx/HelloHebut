"""Durable owner-bound work with bounded workers, server-clock leases and safe events."""
import asyncio
from contextlib import suppress
import hashlib
import json
from pathlib import Path
import secrets

from core.runtime import LeaseLost, RequestConflict


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class QueueFull(RuntimeError):
    pass


class JobStore:
    MAX_ATTEMPTS = 3
    STAGES = {'queued', 'running', 'memory_read', 'route', 'required_tools', 'retrieve',
              'answer', 'verify', 'finalize', 'commit', 'completed', 'failed', 'retrying'}

    def __init__(self, database, capacity=512):
        self.db, self.capacity = database, capacity
        self.table = database.table('jobs')
        self.turns = None

    async def initialize(self):
        await self.db.initialize_tables(Path(__file__).parent/'migrations/004_jobs.sql', ('jobs',))

    @staticmethod
    def owner(value):
        return hashlib.sha256(value.encode()).hexdigest()

    async def enqueue(self, owner, kind, key, payload):
        if kind not in {'chat', 'evaluation', 'maintenance'}:
            raise ValueError('unknown_job_kind')
        body = encoded(payload)
        if len(body.encode()) > 1_000_000:
            raise ValueError('job_payload_too_large')
        job_id = self.owner(encoded([owner, kind, key]))
        fingerprint = self.owner(body)
        conversation_key = None
        if kind == 'chat' and self.turns is not None:
            if payload.get('user_id') != owner or not payload.get('conv_id'):
                raise ValueError('job_conversation_owner_required')
            conversation_key = await self.turns._ensure(owner, payload['conv_id'])
        # Named lock is connection-scoped and released by connection.close AFTER commit.
        lock = 'hh_queue_' + self.owner(self.db.schema)[:40]
        async with self.db.connection() as conn:
            acquired = await (await conn.execute('SELECT GET_LOCK(%s,2) AS ok', (lock,))).fetchone()
            if not acquired['ok']:
                raise QueueFull('任务队列繁忙，请稍后重试')
            if conversation_key is not None:
                # Serialize enqueue versus conversation clear. An enqueue that
                # wins this lock is cancelled by a later clear; otherwise it is new work.
                await self.turns._row(conn, conversation_key)
            old = await (await conn.execute(f'SELECT * FROM {self.table} WHERE job_id=%s', (job_id,))).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise RequestConflict('同一请求编号不能用于不同任务内容')
                return self.public(old)
            count = await (await conn.execute(f"SELECT COUNT(*) AS count FROM {self.table} WHERE status IN ('queued','running')")).fetchone()
            if count['count'] >= self.capacity:
                raise QueueFull('任务队列已满，请稍后重试')
            await conn.execute(f'INSERT INTO {self.table}(job_id,owner_hash,kind,fingerprint,payload_json,events_json) '
                'VALUES (%s,%s,%s,%s,%s,%s)', (job_id, self.owner(owner), kind, fingerprint, body, '[]'))
        return await self.get(owner, job_id)

    async def enqueue_turn(self, conn, owner, conv, request, message):
        """Atomic maintenance outbox; called inside the chat commit transaction."""
        payload = {'user_id': owner, 'conv_id': conv, 'request_id': request, 'message': message}
        body = encoded(payload)
        job_id = self.owner(encoded([owner, 'maintenance', conv, request]))
        await conn.execute(f'INSERT INTO {self.table}(job_id,owner_hash,kind,fingerprint,payload_json,events_json) '
            'VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE job_id=job_id',
            (job_id, self.owner(owner), 'maintenance', self.owner(body), body, '[]'))

    async def clear_conversation(self, conn, owner, conv):
        """Cancel and redact conversation jobs in the same transaction as clear.

        Keep the identity/fingerprint tombstone: replaying an old enqueue cannot
        resurrect its payload. Any claimed worker is fenced by the removed lease.
        """
        await conn.execute(f"UPDATE {self.table} SET status='failed',error_type='ConversationCleared',"
            "payload_json='{}',result_json=NULL,events_json='[]',lease_token=NULL,lease_until=NULL,"
            'updated_at=UTC_TIMESTAMP(6) WHERE owner_hash=%s AND kind IN (\'chat\',\'maintenance\') '
            "AND JSON_UNQUOTE(JSON_EXTRACT(payload_json,'$.conv_id'))=%s", (self.owner(owner), conv))

    @staticmethod
    def public(row):
        return {'run_id': row['job_id'], 'kind': row['kind'], 'status': row['status'],
                'attempts': row['attempts'], 'sequence': row['sequence'],
                'events': json.loads(row['events_json']),
                'result': json.loads(row['result_json']) if row['status'] == 'completed' and row['result_json'] else None,
                'error_type': row['error_type'] if row['status'] == 'failed' else ''}

    async def get(self, owner, job_id):
        async with self.db.connection() as conn:
            row = await (await conn.execute(f'SELECT * FROM {self.table} WHERE job_id=%s AND owner_hash=%s',
                                            (job_id, self.owner(owner)))).fetchone()
        return self.public(row) if row else None

    async def claim(self, lease_s=40):
        async with self.db.connection() as conn:
            exhausted = await (await conn.execute(f'SELECT * FROM {self.table} '
                "WHERE status='running' AND lease_until<=UTC_TIMESTAMP(6) AND attempts>=%s "
                'ORDER BY available_at,job_id LIMIT 32 FOR UPDATE SKIP LOCKED', (self.MAX_ATTEMPTS,))).fetchall()
            for old in exhausted:
                receipt, inflight = await self._canonical_chat_receipt(conn, old)
                if inflight:
                    # A paused worker can outlive its job lease between the last
                    # progress check and the chat transaction. Let that fenced
                    # conversation lease settle before deciding the final status.
                    continue
                await conn.execute(f'UPDATE {self.table} SET status=%s,result_json=%s,error_type=%s,'
                    'lease_token=NULL,lease_until=NULL,updated_at=UTC_TIMESTAMP(6) WHERE job_id=%s',
                    ('completed' if receipt is not None else 'failed', encoded(receipt) if receipt is not None else None,
                     '' if receipt is not None else 'WorkerAttemptsExhausted', old['job_id']))
            row = await (await conn.execute(f'SELECT * FROM {self.table} WHERE attempts<%s AND '
                "((status='queued' AND available_at<=UTC_TIMESTAMP(6)) OR (status='running' AND lease_until<=UTC_TIMESTAMP(6))) "
                'ORDER BY available_at,job_id LIMIT 1 FOR UPDATE SKIP LOCKED', (self.MAX_ATTEMPTS,))).fetchone()
            if not row:
                return None
            token = secrets.token_hex(24)
            await conn.execute(f"UPDATE {self.table} SET status='running',attempts=attempts+1,lease_token=%s,"
                'lease_until=TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP(6)),updated_at=UTC_TIMESTAMP(6) WHERE job_id=%s',
                (token, lease_s, row['job_id']))
            return {'id': row['job_id'], 'token': token, 'kind': row['kind'],
                    'payload': json.loads(row['payload_json']), 'attempts': row['attempts'] + 1}

    async def _canonical_chat_receipt(self, conn, row):
        """Recover a lost final task marker without granting another model attempt.

        Read the canonical receipt on the existing connection. Do not acquire a
        conversation lock while holding a job lock (clear uses the inverse order).
        Only trusted server payloads with matching owner and message may recover.
        """
        if row['kind'] != 'chat' or self.turns is None or self.turns.db.schema != self.db.schema:
            return None, False
        payload = json.loads(row['payload_json'])
        if not isinstance(payload, dict) or any(not isinstance(payload.get(key), str) or not payload[key]
                for key in ('user_id', 'conv_id', 'client_request_id', 'message')):
            return None, False
        if (self.owner(payload['user_id']) != row['owner_hash']
                or self.owner(encoded(payload)) != row['fingerprint']):
            return None, False
        key = self.turns.identity(payload['user_id'], payload['conv_id'])
        request = await (await conn.execute(f'SELECT status,message_hash,result_json FROM {self.turns.requests} '
            'WHERE conversation_key=%s AND request_hash=%s',
            (key, self.turns.request_hash(payload['client_request_id'])))).fetchone()
        if request and request['message_hash'] != self.turns.message_hash(payload['message']):
            return None, False
        if request and request['status'] == 'committed':
            return json.loads(request['result_json']), False
        conversation = await (await conn.execute('SELECT lease_token IS NOT NULL '
            f'AND lease_until>UTC_TIMESTAMP(6) AS active FROM {self.turns.conversations} WHERE conversation_key=%s',
            (key,))).fetchone()
        return None, bool(conversation and conversation['active'])

    async def _locked(self, conn, job):
        row = await (await conn.execute(f'SELECT *,lease_until>UTC_TIMESTAMP(6) AS valid FROM {self.table} '
                                        'WHERE job_id=%s FOR UPDATE', (job['id'],))).fetchone()
        if not row or row['status'] != 'running' or row['lease_token'] != job['token'] or not row['valid']:
            raise LeaseLost('task_lease_lost')
        return row

    async def renew(self, job, lease_s=40):
        async with self.db.connection() as conn:
            await self._locked(conn, job)
            await conn.execute(f'UPDATE {self.table} SET lease_until=TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP(6)) '
                               'WHERE job_id=%s', (lease_s, job['id']))

    async def event(self, job, stage):
        if stage not in self.STAGES:
            return  # Never persist a model-supplied status or body as a progress event.
        async with self.db.connection() as conn:
            row = await self._locked(conn, job)
            sequence = row['sequence'] + 1
            events = json.loads(row['events_json'])[-127:]
            events.append({'run_id': job['id'], 'sequence': sequence, 'type': 'progress', 'stage': stage})
            await conn.execute(f'UPDATE {self.table} SET events_json=%s,sequence=%s WHERE job_id=%s',
                               (encoded(events), sequence, job['id']))

    async def finish(self, job, result=None, error=None, retry=True):
        async with self.db.connection() as conn:
            row = await self._locked(conn, job)
            status = 'completed' if error is None else 'queued' if retry and row['attempts'] < self.MAX_ATTEMPTS else 'failed'
            await conn.execute(f'UPDATE {self.table} SET status=%s,result_json=%s,error_type=%s,lease_token=NULL,'
                'lease_until=NULL,available_at=TIMESTAMPADD(SECOND,2,UTC_TIMESTAMP(6)),updated_at=UTC_TIMESTAMP(6) WHERE job_id=%s',
                (status, encoded(result) if error is None else None, type(error).__name__ if error else '', job['id']))

    async def drop_test_tables(self):
        await self.db.drop_test_tables(('jobs',))


class DurableWorkers:
    def __init__(self, store, handlers, workers=2):
        self.store, self.handlers, self.workers = store, handlers, max(1, min(4, workers))
        self.tasks = []

    def start(self):
        self.tasks = [asyncio.create_task(self._loop()) for _ in range(self.workers)]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _execute(self, job):
        from core.runtime import BudgetExceeded
        await self.store.event(job, 'running')
        task = asyncio.create_task(self.handlers[job['kind']](job))
        try:
            async with asyncio.timeout(900 if job['kind'] == 'evaluation' else 90):
                while not task.done():
                    done, _ = await asyncio.wait({task}, timeout=10)
                    if not done:
                        await self.store.renew(job)
                result = await task
            await self.store.finish(job, result)
        except asyncio.CancelledError:
            raise  # Persisted lease expires; another process resumes the same task.
        except Exception as exc:
            with suppress(LeaseLost):
                await self.store.finish(job, error=exc, retry=not isinstance(exc, (RequestConflict, BudgetExceeded, ValueError)))
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self):
        while True:
            try:
                job = await self.store.claim()
                if job:
                    await self._execute(job)
                else:
                    await asyncio.sleep(.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1)  # Database outage leaves durable jobs intact.
