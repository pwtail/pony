"""Async PostgreSQL provider on psycopg3 (asyncio).

Mirrors the sync PostgreSQLProvider connection operations on
psycopg.AsyncConnection / AsyncCursor. All dialect code (SQL building,
converters, schema) is shared with the sync provider.

Async sessions take a connection from psycopg_pool.AsyncConnectionPool
per db session and return it on release. Pools are created lazily, one
per running event loop (a pool is bound to the loop it was opened on).
"""

import asyncio
import threading
from functools import wraps

import psycopg
import psycopg_pool
from psycopg import errors as pg_errors

from pony.orm import core
from pony.orm.core import (
    DataError,
    DatabaseError,
    IntegrityError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
    log_orm,
)
from pony.orm.dbproviders.postgres import PGProvider
from pony.orm.ops import AsyncOps


def async_wrap_dbapi_exceptions(func):
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        try:
            return await func(self, *args, **kwargs)
        except pg_errors.NotSupportedError as e:
            raise NotSupportedError(e)
        except pg_errors.ProgrammingError as e:
            raise ProgrammingError(e)
        except pg_errors.InternalError as e:
            raise InternalError(e)
        except pg_errors.IntegrityError as e:
            raise IntegrityError(e)
        except pg_errors.DataError as e:
            raise DataError(e)
        except pg_errors.OperationalError as e:
            raise OperationalError(e)
        except pg_errors.Error as e:
            raise DatabaseError(e)

    return wrapper


class _AsyncPools:
    """One AsyncConnectionPool per running event loop (pools are loop-bound)."""

    def __init__(self, provider):
        self.provider = provider
        self._pools = {}
        self._lock = threading.Lock()

    def get_pool(self):
        loop = asyncio.get_running_loop()
        with self._lock:
            pool = self._pools.get(loop)
            if pool is None:
                args, pool_kwargs = self.provider._async_pool_args
                pool = psycopg_pool.AsyncConnectionPool(
                    *args,
                    kwargs=pool_kwargs,
                    min_size=1,
                    max_size=10,
                    open=False,  # открывается лениво при первом async-использовании
                    configure=self.provider._async_configure,
                )
                self._pools[loop] = pool
        return pool


class AsyncPostgreSQLProvider(PGProvider):
    """PostgreSQL provider with asyncio connection operations.

    The sync machinery (pool, connection inspection at bind time, schema
    introspection) is inherited from PGProvider and stays sync; async
    sessions take connections from the loop-local async pool.
    """

    def __init__(self, database, *args, **kwargs):
        super().__init__(database, *args, **kwargs)
        self.async_ops = AsyncOps(self)  # self.sync_ops (SyncOps) наследуется от DBAPIProvider
        pool_kwargs = dict(kwargs)
        pool_kwargs.pop("pony_call_on_connect", None)  # внутренние ключи core, не dsn-опции
        pool_kwargs.pop("pony_pool_mockup", None)
        conninfo = pool_kwargs.pop("dsn", None)
        if "database" in pool_kwargs and "dbname" not in pool_kwargs:
            pool_kwargs["dbname"] = pool_kwargs.pop("database")
        pool_kwargs.setdefault("client_encoding", "UTF8")
        if conninfo is not None:
            args = args + (conninfo,)
        configure_cb = getattr(database, "call_on_connect", None)

        async def configure(conn):
            if configure_cb is not None:
                configure_cb(conn)

        self._async_configure = configure
        self._async_pool_args = (args, pool_kwargs)
        self._async_pools = _AsyncPools(self)

    @property
    def async_pool(self):
        return self._async_pools.get_pool()

    @async_wrap_dbapi_exceptions
    async def async_connect(self):
        await self.async_pool.open(wait=False)
        return await self.async_pool.getconn()

    @async_wrap_dbapi_exceptions
    async def async_set_transaction_mode(self, connection, cache):
        assert not cache.in_transaction
        if cache.immediate and connection.autocommit:
            await connection.set_autocommit(False)
            if core.local.debug:
                log_orm("SWITCH FROM AUTOCOMMIT TO TRANSACTION MODE")
        db_session = cache.db_session
        if db_session is not None and db_session.serializable:
            cursor = connection.cursor()
            sql = "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
            if core.local.debug:
                log_orm(sql)
            await cursor.execute(sql)
        elif not cache.immediate and not connection.autocommit:
            await connection.set_autocommit(True)
            if core.local.debug:
                log_orm("SWITCH TO AUTOCOMMIT MODE")
        if db_session is not None and (db_session.serializable or db_session.ddl):
            cache.in_transaction = True

    @async_wrap_dbapi_exceptions
    async def async_execute(self, cursor, sql, arguments=None, returning_id=False):
        if type(arguments) is list:
            assert arguments and not returning_id
            await cursor.executemany(sql, arguments)
        else:
            if arguments is None:
                await cursor.execute(sql)
            else:
                await cursor.execute(sql, arguments)
            if returning_id:
                return (await cursor.fetchone())[0]

    @async_wrap_dbapi_exceptions
    async def async_commit(self, connection, cache=None):
        if core.local.debug:
            log_orm("COMMIT")
        await connection.commit()
        if cache is not None:
            cache.in_transaction = False

    @async_wrap_dbapi_exceptions
    async def async_rollback(self, connection, cache=None):
        if core.local.debug:
            log_orm("ROLLBACK")
        await connection.rollback()
        if cache is not None:
            cache.in_transaction = False

    @async_wrap_dbapi_exceptions
    async def async_release(self, connection, cache=None):
        if cache is not None and cache.db_session is not None and cache.db_session.ddl:
            await self.async_drop(connection, cache)
            return
        if core.local.debug:
            log_orm("RELEASE CONNECTION")
        try:
            await connection.rollback()
            await connection.set_autocommit(True)
            cursor = connection.cursor()
            await cursor.execute("DISCARD ALL", prepare=False)
            prepared = getattr(connection, "prepared", None) or getattr(
                connection, "_prepared", None
            )
            if prepared is not None:
                prepared.clear()
            await connection.set_autocommit(False)
        except BaseException:
            await self.async_drop(connection, cache)
            raise
        await self.async_pool.putconn(connection)

    @async_wrap_dbapi_exceptions
    async def async_drop(self, connection, cache=None):
        if core.local.debug:
            log_orm("CLOSE CONNECTION")
        await connection.close()
        await self.async_pool.putconn(connection)  # пул отбракует закрытое соединение
        if cache is not None:
            cache.in_transaction = False

provider_cls = AsyncPostgreSQLProvider
