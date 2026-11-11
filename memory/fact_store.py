"""MySQL authority for confirmed preferences, with owner-serialized mutations.

Revision checks protect stale edits and body-free receipts prevent retries from
resurrecting deleted data. No preference copies are written to Redis or Chroma.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import uuid

from memory.mysql_database import MySQLDatabase
from memory.preferences import PreferenceValue, extract_candidates


class MemoryConflict(ValueError):
    pass


class MemoryNotFound(LookupError):
    pass


class FactStore(MySQLDatabase):
    TABLES = ('memory_owners', 'memory_records', 'memory_operations', 'memory_captures')

    def __init__(self, dsn='', schema='hellohebut_memory_v2'):
        super().__init__(dsn, schema)

    async def initialize(self):
        await self.initialize_tables(Path(__file__).parent/'migrations/001_facts.sql', self.TABLES)

    async def drop_test_tables(self):
        await super().drop_test_tables(reversed(self.TABLES))

    @staticmethod
    def owner(user_id):
        return hashlib.sha256(user_id.encode()).hexdigest()

    async def _lock(self, conn, owner):
        await conn.execute(f'INSERT INTO {self.table("memory_owners")}(owner_id) VALUES (%s) '
                           'ON DUPLICATE KEY UPDATE owner_id=owner_id', (owner,))
        row = await (await conn.execute(f'SELECT revision FROM {self.table("memory_owners")} WHERE owner_id=%s FOR UPDATE', (owner,))).fetchone()
        expired = await conn.execute(f"UPDATE {self.table('memory_records')} SET status='expired' WHERE owner_id=%s "
                                     "AND status IN ('active','pending') AND valid_until <= CURRENT_TIMESTAMP(6)", (owner,))
        if expired.rowcount:
            row['revision'] = await self._bump(conn, owner)
        return row['revision']

    async def _bump(self, conn, owner):
        await conn.execute(f'UPDATE {self.table("memory_owners")} SET revision=revision+1 WHERE owner_id=%s', (owner,))
        row = await (await conn.execute(f'SELECT revision FROM {self.table("memory_owners")} WHERE owner_id=%s', (owner,))).fetchone()
        return row['revision']

    async def snapshot(self, user_id, *, history=False):
        owner = self.owner(user_id)
        async with self.connection() as conn:
            revision = await self._lock(conn, owner)
            rows = await (await conn.execute(f'SELECT * FROM {self.table("memory_records")} WHERE owner_id=%s ' +
                ('' if history else "AND status IN ('active','pending') ") +
                'ORDER BY created_at DESC, memory_id LIMIT 200', (owner,))).fetchall()
        return {'revision': revision, 'items': [self.public(row) for row in rows]}

    @staticmethod
    def public(row):
        # DATETIME is timezone-naive in the driver; every DB connection is pinned to UTC.
        return {key: (value.replace(tzinfo=timezone.utc).isoformat() if isinstance(value, datetime) else value)
                for key, value in row.items() if key not in {'owner_id', 'active_key'}}

    async def capture(self, user_id, conv_id, request_id, run_id, message, expected_revision):
        candidates = extract_candidates(message)
        if not candidates or expected_revision is None:
            return
        owner = self.owner(user_id)
        capture_id = hashlib.sha256(json.dumps([conv_id, request_id]).encode()).hexdigest()
        async with self.connection() as conn:
            revision = await self._lock(conn, owner)
            existing = await (await conn.execute(f'SELECT 1 FROM {self.table("memory_captures")} WHERE owner_id=%s AND capture_id=%s', (owner, capture_id))).fetchone()
            if existing:
                return
            await conn.execute(f'INSERT INTO {self.table("memory_captures")}(owner_id,capture_id) VALUES (%s,%s)', (owner, capture_id))
            if revision != expected_revision:
                return
            for candidate in candidates:
                existing = await (await conn.execute(f"SELECT 1 FROM {self.table('memory_records')} WHERE owner_id=%s AND fact_key=%s "
                    "AND value=%s AND status IN ('active','pending')", (owner, candidate['key'], candidate['value']))).fetchone()
                if existing:
                    continue
                await conn.execute(f"DELETE FROM {self.table('memory_records')} WHERE owner_id=%s AND fact_key=%s AND status='pending'", (owner, candidate['key']))
                version = await self._bump(conn, owner)
                await self._insert(conn, owner, candidate['key'], candidate['value'], version, 'pending',
                    'chat', request_id, run_id, conv_id, candidate['source_excerpt'], revision, 7)

    async def _insert(self, conn, owner, key, value, version, status, kind, message_id, run_id, conv_id, excerpt, base, days):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        await conn.execute(f'INSERT INTO {self.table("memory_records")}(memory_id,owner_id,fact_key,value,version,status,verification_status,'
            'source_kind,source_message_id,source_run_id,source_conv_id,source_excerpt,base_revision,confirmed_at,valid_until) '
            'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (str(uuid.uuid4()), owner, key, value, version, status,
             'user_confirmed' if status == 'active' else 'source_checked', kind, message_id, run_id, conv_id,
             excerpt, base, now if status == 'active' else None, now + timedelta(days=days)))

    async def mutate(self, user_id, action, operation_id, expected_revision, *, key=None, value=None, memory_id=None, valid_days=180):
        if action not in {'save', 'confirm', 'delete', 'clear', 'reject'}:
            raise ValueError('invalid_memory_action')
        if action == 'save':
            value = PreferenceValue(key=key, value=value).value
        if not 1 <= valid_days <= 365:
            raise ValueError('invalid_memory_validity')
        owner = self.owner(user_id)
        fingerprint = hashlib.sha256(json.dumps([action, key, value, memory_id, valid_days, expected_revision], ensure_ascii=False).encode()).hexdigest()
        async with self.connection() as conn:
            revision = await self._lock(conn, owner)
            existing = await (await conn.execute(f'SELECT fingerprint FROM {self.table("memory_operations")} WHERE owner_id=%s AND operation_id=%s', (owner, operation_id))).fetchone()
            if existing:
                if existing['fingerprint'] != fingerprint:
                    raise MemoryConflict('同一操作编号不能用于不同修改')
                return
            if revision != expected_revision:
                raise MemoryConflict('偏好已发生变更，请刷新后重试')
            row = None
            if action in {'confirm', 'reject'}:
                row = await (await conn.execute(f'SELECT * FROM {self.table("memory_records")} WHERE owner_id=%s AND memory_id=%s', (owner, memory_id))).fetchone()
                if not row or row['status'] != 'pending':
                    raise MemoryNotFound('待确认偏好不存在或已过期')
                key = row['fact_key']
            if action == 'confirm':
                active = await (await conn.execute(f"SELECT version FROM {self.table('memory_records')} WHERE owner_id=%s AND fact_key=%s AND status='active'", (owner, key))).fetchone()
                if active and active['version'] > row['base_revision']:
                    raise MemoryConflict('该候选早于当前偏好，请重新录入')
            version = await self._bump(conn, owner)
            if action in {'save', 'confirm'}:
                await conn.execute(f"UPDATE {self.table('memory_records')} SET status='superseded' WHERE owner_id=%s AND fact_key=%s AND status='active'", (owner, key))
                if action == 'save':
                    await conn.execute(f"DELETE FROM {self.table('memory_records')} WHERE owner_id=%s AND fact_key=%s AND status='pending'", (owner, key))
                    await self._insert(conn, owner, key, value, version, 'active', 'user_input', operation_id,
                                       '', '', value, revision, valid_days)
                else:
                    await conn.execute(f"UPDATE {self.table('memory_records')} SET status='active', verification_status='user_confirmed', "
                        "confirmed_at=CURRENT_TIMESTAMP(6), valid_until=DATE_ADD(CURRENT_TIMESTAMP(6), INTERVAL %s DAY) WHERE owner_id=%s AND memory_id=%s",
                        (valid_days, owner, memory_id))
            elif action == 'delete':
                if key not in {'major_preference','interests','budget_preference','location_preference'}:
                    raise ValueError('invalid_memory_key')
                await conn.execute(f'DELETE FROM {self.table("memory_records")} WHERE owner_id=%s AND fact_key=%s', (owner, key))
            elif action == 'reject':
                await conn.execute(f'DELETE FROM {self.table("memory_records")} WHERE owner_id=%s AND memory_id=%s', (owner, memory_id))
            else:
                await conn.execute(f'DELETE FROM {self.table("memory_records")} WHERE owner_id=%s', (owner,))
            await conn.execute(f'INSERT INTO {self.table("memory_operations")}(owner_id,operation_id,fingerprint) VALUES (%s,%s,%s)', (owner, operation_id, fingerprint))
