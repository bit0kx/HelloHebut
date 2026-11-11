"""PostgreSQL is the sole authority for confirmed preferences, not for chat turns yet.

User-scoped row locks serialize mutations. A revision check protects stale UI edits;
body-free receipts prevent retries from resurrecting deleted data. No vector/cache copy.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

from memory.preferences import PreferenceValue, extract_candidates


class MemoryConflict(ValueError):
    pass


class MemoryNotFound(LookupError):
    pass


class FactStore:
    def __init__(self, dsn: str, schema='hellohebut_memory_v2'):
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', schema):
            raise ValueError('invalid_fact_schema')
        self.dsn, self.schema = dsn, schema

    @asynccontextmanager
    async def connection(self):
        # Lazy import keeps the disabled feature and offline parsers dependency-free.
        import psycopg
        from psycopg.rows import dict_row
        from psycopg import sql
        async with await psycopg.AsyncConnection.connect(
                self.dsn, connect_timeout=3, row_factory=dict_row,
                options='-c statement_timeout=4000 -c lock_timeout=2000') as conn:
            await conn.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(self.schema)))
            yield conn

    async def initialize(self):
        from psycopg import sql
        async with self.connection() as conn:
            # Serialize schema creation across application workers; migrations are additive.
            await conn.execute('SELECT pg_advisory_xact_lock(%s)', (72419001,))
            await conn.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(self.schema)))
            await conn.execute((Path(__file__).parent/'migrations/001_facts.sql').read_text(encoding='utf-8'))

    @staticmethod
    def owner(user_id):
        return hashlib.sha256(user_id.encode()).hexdigest()

    async def _lock(self, conn, owner):
        await conn.execute('INSERT INTO memory_owners(owner_id) VALUES (%s) ON CONFLICT DO NOTHING', (owner,))
        row = await (await conn.execute('SELECT revision FROM memory_owners WHERE owner_id=%s FOR UPDATE', (owner,))).fetchone()
        expired = await conn.execute("UPDATE memory_records SET status='expired' WHERE owner_id=%s "
                                     "AND status IN ('active','pending') AND valid_until <= now()", (owner,))
        if expired.rowcount:
            row['revision'] = await self._bump(conn, owner)
        return row['revision']

    @staticmethod
    async def _bump(conn, owner):
        row = await (await conn.execute('UPDATE memory_owners SET revision=revision+1 WHERE owner_id=%s RETURNING revision', (owner,))).fetchone()
        return row['revision']

    async def snapshot(self, user_id, *, history=False):
        owner = self.owner(user_id)
        async with self.connection() as conn:
            revision = await self._lock(conn, owner)
            rows = await (await conn.execute('SELECT * FROM memory_records WHERE owner_id=%s ' +
                ('' if history else "AND status IN ('active','pending') ") +
                'ORDER BY created_at DESC, memory_id LIMIT 200', (owner,))).fetchall()
        return {'revision': revision, 'items': [self.public(row) for row in rows]}

    @staticmethod
    def public(row):
        return {key: (value.isoformat() if isinstance(value, datetime) else str(value) if isinstance(value, uuid.UUID) else value)
                for key, value in row.items() if key != 'owner_id'}

    async def capture(self, user_id, conv_id, request_id, run_id, message, expected_revision):
        candidates = extract_candidates(message)
        if not candidates or expected_revision is None:
            return
        owner = self.owner(user_id)
        capture_id = hashlib.sha256(json.dumps([conv_id, request_id]).encode()).hexdigest()
        async with self.connection() as conn:
            revision = await self._lock(conn, owner)
            inserted = await conn.execute('INSERT INTO memory_captures VALUES (%s,%s) ON CONFLICT DO NOTHING', (owner, capture_id))
            if not inserted.rowcount or revision != expected_revision:
                return  # Includes an edit/delete made while the answer was in flight.
            # Prevent an unbounded pending inbox from ordinary conversation traffic.
            for candidate in candidates:
                existing = await (await conn.execute("SELECT 1 FROM memory_records WHERE owner_id=%s AND fact_key=%s "
                    "AND value=%s AND status IN ('active','pending')", (owner, candidate['key'], candidate['value']))).fetchone()
                if existing:
                    continue
                await conn.execute("DELETE FROM memory_records WHERE owner_id=%s AND fact_key=%s AND status='pending'", (owner, candidate['key']))
                version = await self._bump(conn, owner)
                await self._insert(conn, owner, candidate['key'], candidate['value'], version, 'pending',
                    'chat', request_id, run_id, conv_id, candidate['source_excerpt'], revision, 7)

    async def _insert(self, conn, owner, key, value, version, status, kind, message_id, run_id, conv_id, excerpt, base, days):
        now = datetime.now(timezone.utc)
        await conn.execute('INSERT INTO memory_records(memory_id,owner_id,fact_key,value,version,status,verification_status,'
            'source_kind,source_message_id,source_run_id,source_conv_id,source_excerpt,base_revision,confirmed_at,valid_until) '
            'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (uuid.uuid4(), owner, key, value, version, status,
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
            existing = await (await conn.execute('SELECT fingerprint FROM memory_operations WHERE owner_id=%s AND operation_id=%s', (owner, operation_id))).fetchone()
            if existing:
                if existing['fingerprint'] != fingerprint:
                    raise MemoryConflict('同一操作编号不能用于不同修改')
                return  # Replay is inert, even if the corresponding fact was subsequently deleted.
            if revision != expected_revision:
                raise MemoryConflict('偏好已发生变更，请刷新后重试')
            row = None
            if action in {'confirm', 'reject'}:
                row = await (await conn.execute('SELECT * FROM memory_records WHERE owner_id=%s AND memory_id=%s', (owner, memory_id))).fetchone()
                if not row or row['status'] != 'pending':
                    raise MemoryNotFound('待确认偏好不存在或已过期')
                key = row['fact_key']
            if action == 'confirm':
                # A newer active fact cannot be replaced by an older pending candidate.
                active = await (await conn.execute("SELECT version FROM memory_records WHERE owner_id=%s AND fact_key=%s AND status='active'", (owner, key))).fetchone()
                if active and active['version'] > row['base_revision']:
                    raise MemoryConflict('该候选早于当前偏好，请重新录入')
            version = await self._bump(conn, owner)
            if action in {'save', 'confirm'}:
                await conn.execute("UPDATE memory_records SET status='superseded' WHERE owner_id=%s AND fact_key=%s AND status='active'", (owner, key))
                if action == 'save':
                    await conn.execute("DELETE FROM memory_records WHERE owner_id=%s AND fact_key=%s AND status='pending'", (owner, key))
                    await self._insert(conn, owner, key, value, version, 'active', 'user_input', operation_id,
                                       '', '', value, revision, valid_days)
                else:
                    await conn.execute("UPDATE memory_records SET status='active', verification_status='user_confirmed', "
                        "confirmed_at=now(), valid_until=now()+(%s * interval '1 day') WHERE owner_id=%s AND memory_id=%s",
                        (valid_days, owner, memory_id))
            elif action == 'delete':
                if key not in {'major_preference','interests','budget_preference','location_preference'}:
                    raise ValueError('invalid_memory_key')
                await conn.execute('DELETE FROM memory_records WHERE owner_id=%s AND fact_key=%s', (owner, key))
            elif action == 'reject':
                await conn.execute('DELETE FROM memory_records WHERE owner_id=%s AND memory_id=%s', (owner, memory_id))
            else:
                await conn.execute('DELETE FROM memory_records WHERE owner_id=%s', (owner,))
            await conn.execute('INSERT INTO memory_operations VALUES (%s,%s,%s)', (owner, operation_id, fingerprint))
