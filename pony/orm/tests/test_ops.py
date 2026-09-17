import asyncio
import unittest
from inspect import isfunction

from pony.orm import Database, Required
from pony.orm.drive import Delegate, drive
from pony.orm.session_cache import (
    AbstractSessionCache,
    AsyncSessionCache,
    SyncSessionCache,
)


async def _identity(x):
    return x


async def _chain(x):
    return await _identity(x) + 1


async def _boom():
    raise ValueError("boom")


class TestDrive(unittest.TestCase):
    def test_returns_value(self):
        self.assertEqual(drive(_identity(42)), 42)

    def test_awaits_immediately_completing_coroutines(self):
        self.assertEqual(drive(_chain(41)), 42)

    def test_propagates_exceptions(self):
        with self.assertRaises(ValueError):
            drive(_boom())


class TestSessionCacheWiring(unittest.TestCase):
    """Sync-методы кэша — обычные функции поверх drive, async — Delegate."""

    METHODS = (
        "connect",
        "reconnect",
        "prepare_connection_for_query_execution",
        "flush_and_commit",
        "commit",
        "rollback",
        "release",
        "close",
        "flush",
    )

    def test_abstract_base_has_no_session_methods(self):
        for name in self.METHODS:
            with self.subTest(method=name):
                self.assertNotIn(name, AbstractSessionCache.__dict__)

    def test_sync_methods_are_plain_functions(self):
        for name in self.METHODS:
            with self.subTest(method=name):
                self.assertTrue(isfunction(SyncSessionCache.__dict__[name]))

    def test_async_methods_are_delegates_to_gen(self):
        for name in self.METHODS:
            with self.subTest(method=name):
                descriptor = AsyncSessionCache.__dict__[name]
                self.assertIsInstance(descriptor, Delegate)
                self.assertEqual(descriptor.target, "_gen")
                self.assertEqual(descriptor.name, name)

    def test_mode_is_defined_by_subclasses(self):
        self.assertTrue(issubclass(SyncSessionCache, AbstractSessionCache))
        self.assertTrue(issubclass(AsyncSessionCache, AbstractSessionCache))
        self.assertIs(SyncSessionCache.is_async, False)
        self.assertIs(AsyncSessionCache.is_async, True)


class TestDelegate(unittest.TestCase):
    def test_delegate_returns_gen_coroutine(self):
        class Gen:
            async def calc(self, x):
                return x + 1

        class Cache:
            calc = Delegate('_gen')

            def __init__(self):
                self._gen = Gen()

        self.assertEqual(asyncio.run(Cache().calc(41)), 42)

    def test_delegate_calls_method_on_gen_itself(self):
        class Gen:
            async def who(self):
                return self

        class Cache:
            who = Delegate('_gen')

            def __init__(self):
                self._gen = Gen()

        cache = Cache()
        self.assertIs(asyncio.run(cache.who()), cache._gen)

    def test_delegate_propagates_exceptions(self):
        class Gen:
            async def boom(self):
                raise ValueError("boom")

        class Cache:
            boom = Delegate('_gen')

            def __init__(self):
                self._gen = Gen()

        with self.assertRaises(ValueError):
            asyncio.run(Cache().boom())


class TestSyncOps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = db = Database("sqlite", ":memory:")

        class T(db.Entity):
            name = Required(str)

        cls.T = T
        db.generate_mapping(create_tables=True)

    def test_ops_roundtrip_via_drive(self):
        db = self.db
        ops = db.provider.sync_ops
        connection = drive(ops.connect(db, None))
        try:
            cursor = connection.cursor()
            drive(ops.execute(cursor, "INSERT INTO t(name) VALUES ('x')"))
            drive(ops.commit(connection, None))
            cursor = connection.cursor()
            drive(ops.execute(cursor, "SELECT name FROM t ORDER BY id"))
            self.assertEqual(cursor.fetchall(), [("x",)])
            cursor = connection.cursor()
            new_id = drive(
                ops.execute(
                    cursor,
                    "INSERT INTO t(name) VALUES ('y') RETURNING id",
                    (),
                    True,
                )
            )
            self.assertIsInstance(new_id, int)
        finally:
            drive(ops.drop(connection, None))

    def test_ops_can_be_awaited_normally(self):
        # SyncOps-корутины — обычные async-функции: работают и под event loop
        async def scenario():
            db = self.db
            ops = db.provider.sync_ops
            connection = await ops.connect(db, None)
            try:
                cursor = connection.cursor()
                await ops.execute(cursor, "SELECT 1")
                self.assertEqual(cursor.fetchall(), [(1,)])
            finally:
                await ops.drop(connection, None)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
