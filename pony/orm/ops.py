"""ProviderOps: the single place where sync and async modes differ physically.

Gen-classes are written once in async style; every I/O point goes through
this adapter (``provider.sync_ops`` / ``provider.async_ops``):

- SyncOps  — coroutines with no real await points: the body executes fully
  on the first ``next()``, on top of the sync dbapi machinery. Driven by
  ``drive()`` from pony.orm.drive without an event loop.
- AsyncOps — real async driver calls, awaited normally.
"""


def ops_for(provider, is_async):
    """ProviderOps for the given session mode."""
    return provider.async_ops if is_async else provider.sync_ops


class SyncOps:
    """ProviderOps for the sync driver: awaits complete immediately.

    Методы обязаны оставаться coroutine-функциями: Gen-код делает `await`
    на их результатах, а протокол drive() требует awaitable, завершающийся
    на первом шаге. Тел без реальных await-точек — это нормально.
    """

    def __init__(self, provider):
        self.provider = provider

    async def connect(self, database, cache):
        provider = self.provider
        connection, is_new_connection = provider.connect()
        if is_new_connection:
            database.call_on_connect(connection)
        return connection

    async def set_transaction_mode(self, connection, cache):
        return self.provider.set_transaction_mode(connection, cache)

    async def execute(self, cursor, sql, arguments=None, returning_id=False):
        return self.provider.execute(cursor, sql, arguments, returning_id)

    async def fetchone(self, cursor):
        return cursor.fetchone()

    async def fetchmany(self, cursor, size):
        return cursor.fetchmany(size)

    async def fetchall(self, cursor):
        return cursor.fetchall()

    async def commit(self, connection, cache=None):
        return self.provider.commit(connection, cache)

    async def rollback(self, connection, cache=None):
        return self.provider.rollback(connection, cache)

    async def release(self, connection, cache=None):
        return self.provider.release(connection, cache)

    async def drop(self, connection, cache=None):
        return self.provider.drop(connection, cache)


class AsyncOps:
    """ProviderOps for the async driver: real async provider calls.

    Методы — обычные функции, возвращающие awaitable провайдера, а не
    `async def ...: return await ...`: лишний coroutine-фрейм на каждый
    DB-вызов (~100 ns) ничего не даёт — ожидание делает вызывающий Gen-код.
    """

    def __init__(self, provider):
        self.provider = provider

    def connect(self, database, cache):
        return self.provider.async_connect()

    def set_transaction_mode(self, connection, cache):
        return self.provider.async_set_transaction_mode(connection, cache)

    def execute(self, cursor, sql, arguments=None, returning_id=False):
        return self.provider.async_execute(cursor, sql, arguments, returning_id)

    def fetchone(self, cursor):
        return cursor.fetchone()

    def fetchmany(self, cursor, size):
        return cursor.fetchmany(size)

    def fetchall(self, cursor):
        return cursor.fetchall()

    def commit(self, connection, cache=None):
        return self.provider.async_commit(connection, cache)

    def rollback(self, connection, cache=None):
        return self.provider.async_rollback(connection, cache)

    def release(self, connection, cache=None):
        return self.provider.async_release(connection, cache)

    def drop(self, connection, cache=None):
        return self.provider.async_drop(connection, cache)
