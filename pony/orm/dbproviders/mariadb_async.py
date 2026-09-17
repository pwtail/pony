"""Async MariaDB provider: коннектор mariadb (2.0RC).

Async-режим на встроенном asyncio-модуле коннектора (mariadb.asyncio) и
официальном пуле mariadb_pool.AsyncConnectionPool (loop-локальный, ленивое
создание — пулу нужен event loop). Sync-часть (диалект, schema, sync-пул)
наследуется от MariaDBProvider. Gen-слой подключается через AsyncOps из
ops — он делегирует этим async_* методам.
"""

import asyncio
import threading
from functools import wraps

import mariadb as mariadb_module
import mariadb.asyncio
import mariadb_pool
from mariadb.constants import CLIENT
from mariadb_pool.pool import AsyncConnectionPool, PoolConfig

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
from pony.orm.dbproviders.mariadb import MariaDBProvider
from pony.orm.ops import AsyncOps


def async_wrap_dbapi_exceptions(func):
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        try:
            return await func(self, *args, **kwargs)
        except mariadb_module.NotSupportedError as e:
            raise NotSupportedError(e)
        except mariadb_module.ProgrammingError as e:
            raise ProgrammingError(e)
        except mariadb_module.InternalError as e:
            raise InternalError(e)
        except mariadb_module.IntegrityError as e:
            raise IntegrityError(e)
        except mariadb_module.DataError as e:
            raise DataError(e)
        except mariadb_module.OperationalError as e:
            raise OperationalError(e)
        except mariadb_module.Error as e:
            raise DatabaseError(e)

    return wrapper


class _AsyncPools:
    """Один AsyncConnectionPool на запущенный event loop (пулы привязаны к loop)."""

    def __init__(self, provider):
        self.provider = provider
        self._pools = {}
        self._lock = threading.Lock()

    def get_pool(self):
        loop = asyncio.get_running_loop()
        with self._lock:
            pool = self._pools.get(loop)
            if pool is None:
                pool = AsyncConnectionPool(
                    connection_factory=mariadb.asyncio.connect,
                    config=PoolConfig(
                        min_size=1, max_size=10, reset_connection=True
                    ),
                    **self.provider._async_pool_params,
                )
                self._pools[loop] = pool
        return pool


class MariadbAsyncProvider(MariaDBProvider):
    """MariaDB-провайдер с asyncio-операциями соединения.

    Sync-машинерия (пул, инспекция соединения при bind, schema) — от
    MariaDBProvider; async-сессии берут соединения из loop-локального пула.
    """

    def __init__(self, _database, *args, **kwargs):
        super().__init__(_database, *args, **kwargs)
        self.async_ops = AsyncOps(self)
        self._async_pool_params = dict(kwargs)
        # как MySQLProvider: rowcount = число совпавших строк (для optimistic check)
        self._async_pool_params.setdefault("client_flag", CLIENT.FOUND_ROWS)
        self._async_pools = _AsyncPools(self)

    @property
    def async_pool(self):
        return self._async_pools.get_pool()

    @async_wrap_dbapi_exceptions
    async def async_connect(self):
        pool = self.async_pool
        await pool.open()
        return await pool.acquire()

    @async_wrap_dbapi_exceptions
    async def async_set_transaction_mode(self, connection, cache):
        assert not cache.in_transaction
        db_session = cache.db_session
        if db_session is not None and db_session.ddl:
            cursor = connection.cursor()
            await cursor.execute("SHOW VARIABLES LIKE 'foreign_key_checks'")
            row = await cursor.fetchone()
            fk = row is not None and row[1] == "ON"
            if fk:
                sql = "SET foreign_key_checks = 0"
                if core.local.debug:
                    log_orm(sql)
                await cursor.execute(sql)
            cache.saved_fk_state = bool(fk)
            cache.in_transaction = True
        cache.immediate = True
        if db_session is not None and db_session.serializable:
            cursor = connection.cursor()
            sql = "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
            if core.local.debug:
                log_orm(sql)
            await cursor.execute(sql)
            cache.in_transaction = True

    @async_wrap_dbapi_exceptions
    async def async_execute(self, cursor, sql, arguments=None, returning_id=False):
        if type(arguments) is list:
            assert arguments and not returning_id
            await cursor.executemany(sql, arguments)
        else:
            await cursor.execute(sql, arguments)
            if returning_id:
                return cursor.lastrowid

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
        pooled = getattr(connection, "_pooled_connection", None)
        if pooled is not None:
            await pooled.return_to_pool()  # reset_connection=True сбрасывает
        else:
            await connection.close()

    @async_wrap_dbapi_exceptions
    async def async_drop(self, connection, cache=None):
        if core.local.debug:
            log_orm("CLOSE CONNECTION")
        pooled = getattr(connection, "_pooled_connection", None)
        if pooled is not None:
            await pooled.closeSilently()
        else:
            await connection.close()
        if cache is not None:
            cache.in_transaction = False


provider_cls = MariadbAsyncProvider
