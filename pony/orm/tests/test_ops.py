import asyncio
import unittest

from pony.orm import Database, Required
from pony.orm.drive import DriveGen, drive


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


class TestDriveGen(unittest.TestCase):
    def test_descriptor_drives_parent_method(self):
        class Base:
            async def calc(self, x):
                return x + 1

        class Sync(Base):
            calc = DriveGen()

        self.assertEqual(Sync().calc(41), 42)

    def test_descriptor_propagates_exceptions(self):
        class Base:
            async def boom(self):
                raise ValueError("boom")

        class Sync(Base):
            boom = DriveGen()

        with self.assertRaises(ValueError):
            Sync().boom()


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
