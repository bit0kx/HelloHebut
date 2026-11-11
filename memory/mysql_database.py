"""Async MySQL transactions shared by durable stores; validated table namespaces.

Each operation owns one connection, so cancellation never leaves it in a pool.
Namespaces isolate v2/test tables without needing CREATE DATABASE privileges.
"""
from contextlib import asynccontextmanager
import hashlib
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


class QueryResult:
    def __init__(self, rows, rowcount, lastrowid):
        self.rows, self.rowcount, self.lastrowid = list(rows), rowcount, lastrowid
        self._position = 0

    async def fetchone(self):
        if self._position >= len(self.rows):
            return None
        row = self.rows[self._position]
        self._position += 1
        return row

    async def fetchall(self):
        rows = self.rows[self._position:]
        self._position = len(self.rows)
        return rows


class MySQLConnection:
    def __init__(self, connection):
        self.raw = connection

    async def execute(self, statement, params=()):
        from asyncmy.cursors import DictCursor
        async with self.raw.cursor(cursor=DictCursor) as cursor:
            await cursor.execute(statement, params or None)
            rows = await cursor.fetchall() if cursor.description else []
            return QueryResult(rows, cursor.rowcount, cursor.lastrowid)


class MySQLDatabase:
    def __init__(self, dsn='', namespace='hellohebut_memory_v2'):
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,44}', namespace):
            raise ValueError('invalid_mysql_namespace')
        self.dsn, self.namespace, self.schema = dsn, namespace, namespace

    def table(self, name):
        if not re.fullmatch(r'[a-z][a-z0-9_]*', name):
            raise ValueError('invalid_mysql_table')
        identifier = self.namespace + '_' + name
        if len(identifier) > 64:
            raise ValueError('mysql_identifier_too_long')
        return '`' + identifier + '`'

    def connection_options(self):
        options = {
            'host': os.getenv('MYSQL_HOST', 'localhost'),
            'port': int(os.getenv('MYSQL_PORT', '3306')),
            'db': os.getenv('MYSQL_DATABASE', 'hellohebut'),
            'user': os.getenv('MYSQL_USER', 'hellohebut'),
            'password': os.getenv('MYSQL_PASSWORD', ''),
            'charset': 'utf8mb4', 'autocommit': False,
            'connect_timeout': 3, 'read_timeout': 5,
        }
        if self.dsn:
            parsed = urlsplit(self.dsn)
            if (parsed.scheme not in {'mysql', 'mysql+asyncmy'} or not parsed.hostname
                    or parsed.query or parsed.fragment or not parsed.path.strip('/')):
                raise ValueError('invalid_mysql_dsn')
            options.update(host=parsed.hostname, port=parsed.port or 3306,
                           db=unquote(parsed.path.lstrip('/')),
                           user=unquote(parsed.username or ''), password=unquote(parsed.password or ''))
        return options

    @asynccontextmanager
    async def connection(self):
        import asyncmy  # Lazy: parser/offline tests do not need the native driver.
        connection = await asyncmy.connect(**self.connection_options())
        wrapped = MySQLConnection(connection)
        try:
            await wrapped.execute("SET SESSION time_zone = '+00:00'")
            await wrapped.execute('SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED')
            await wrapped.execute('SET SESSION innodb_lock_wait_timeout = 2')
            await wrapped.execute('SET SESSION max_execution_time = 4000')
            await connection.begin()
            yield wrapped
            await connection.commit()
        except BaseException:
            try:
                await connection.rollback()
            except BaseException:
                pass  # Closing also releases locks after timeout/disconnect.
            raise
        finally:
            connection.close()

    async def initialize_tables(self, migration_path: Path, table_names):
        """Additive DDL under a named lock. MySQL DDL commits implicitly.

        Multi-table creation is not an atomic migration: a subsequent startup
        safely resumes the CREATE IF NOT EXISTS statements after failure.
        """
        template = migration_path.read_text(encoding='utf-8')
        names = {}
        for name in table_names:
            names[name] = self.table(name)
        # FK/CHECK names are database-global and capped at 64 chars, unlike index names.
        for name in re.findall(r'\{((?:fk|ck)_[a-z0-9_]+)\}', template):
            names[name] = '`' + name[:3] + hashlib.sha256((self.namespace + ':' + name).encode()).hexdigest()[:32] + '`'
        sql = template.format_map(names)
        database = self.connection_options()['db']
        lock = 'hhv2_migration_' + hashlib.sha256((database + ':' + self.namespace).encode()).hexdigest()[:40]
        async with self.connection() as conn:
            acquired = await (await conn.execute('SELECT GET_LOCK(%s, 4) AS acquired', (lock,))).fetchone()
            if not acquired or acquired['acquired'] != 1:
                raise TimeoutError('mysql_migration_lock_timeout')
            try:
                for statement in sql.split(';'):
                    if statement.strip():
                        await conn.execute(statement)
            finally:
                await conn.execute('SELECT RELEASE_LOCK(%s)', (lock,))

    async def drop_test_tables(self, table_names):
        if not re.fullmatch(r'test_[a-z_]+_[0-9a-f]{32}', self.namespace):
            raise ValueError('refusing_non_test_namespace')
        async with self.connection() as conn:
            for name in table_names:
                await conn.execute('DROP TABLE IF EXISTS ' + self.table(name))
