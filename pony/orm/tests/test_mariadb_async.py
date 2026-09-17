import asyncio
import os
import unittest

import mariadb as mariadb_module

from pony.orm import (
    Database,
    NotLoadedError,
    Optional,
    Required,
    Set,
    TransactionError,
    db_session,
    select,
)

MDB = dict(
    user=os.environ.get("PONY_MARIADB_USER", "ponytest"),
    password=os.environ.get("PONY_MARIADB_PASSWORD", "ponytest"),
    host=os.environ.get("PONY_MARIADB_HOST", "127.0.0.1"),
    port=int(os.environ.get("PONY_MARIADB_PORT", "3306")),
    database=os.environ.get("PONY_MARIADB_DB", "pony_mariadb_test"),
)


def _mariadb_available():
    try:
        conn = mariadb_module.connect(**MDB)
    except Exception:
        return False
    else:
        conn.close()
        return True


@unittest.skipUnless(
    _mariadb_available(), "MariaDB 'pony_mariadb_test' is not available"
)
class TestMariaDBAsync(unittest.TestCase):
    """Async-режим провайдера mariadb_async (mariadb.asyncio + mariadb_pool)."""

    @classmethod
    def setUpClass(cls):
        cls.db = db = Database("mariadb_async", **MDB)

        class Dept(db.Entity):
            _table_ = "maria_async_dept"
            name = Required(str)
            persons = Set("Person")

        class Person(db.Entity):
            _table_ = "maria_async_person"
            name = Required(str)
            age = Required(int)
            dept = Optional(Dept)
            bio = Optional(str, lazy=True)

        cls.Dept, cls.Person = Dept, Person
        db.generate_mapping(create_tables=True)

    def setUp(self):
        conn = mariadb_module.connect(**MDB)
        cur = conn.cursor()
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        cur.execute("TRUNCATE maria_async_person")
        cur.execute("TRUNCATE maria_async_dept")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        conn.commit()
        conn.close()

    def test_create_commit_on_exit(self):
        async def scenario():
            async with db_session:
                d = self.Dept(name="IT")
                self.Person(name="ann", age=30, dept=d, bio="b1")
            async with db_session:
                objs = await select(x for x in self.Person)
                self.assertEqual([x.name for x in objs], ["ann"])

        asyncio.run(scenario())

    def test_await_select_and_async_for(self):
        async def scenario():
            async with db_session:
                d = self.Dept(name="IT")
                self.Person(name="ann", age=30, dept=d)
                self.Person(name="bob", age=17, dept=d)
            async with db_session:
                objs = await select(x for x in self.Person).order_by(self.Person.id)
                self.assertEqual([x.name for x in objs], ["ann", "bob"])
                names = []
                async for x in select(x for x in self.Person).order_by(self.Person.id):
                    names.append(x.name)
                self.assertEqual(names, ["ann", "bob"])

        asyncio.run(scenario())

    def test_collection_await(self):
        async def scenario():
            async with db_session:
                d = self.Dept(name="IT")
                self.Person(name="ann", age=30, dept=d)
                self.Person(name="bob", age=17, dept=d)
            async with db_session:
                d = (await select(x for x in self.Dept))[0]
                with self.assertRaises(NotLoadedError):
                    [p.name for p in d.persons]
                await d.persons
                self.assertEqual(sorted(p.name for p in d.persons), ["ann", "bob"])

        asyncio.run(scenario())

    def test_lazy_attr_requires_load(self):
        async def scenario():
            async with db_session:
                d = self.Dept(name="IT")
                self.Person(name="ann", age=30, dept=d, bio="b1")
            async with db_session:
                p = (await select(x for x in self.Person))[0]
                with self.assertRaises(NotLoadedError):
                    p.bio
                await p.load("bio")
                self.assertEqual(p.bio, "b1")

        asyncio.run(scenario())

    def test_write_flushed_on_exit(self):
        async def scenario():
            async with db_session:
                d = self.Dept(name="IT")
                self.Person(name="ann", age=30, dept=d)
            async with db_session:
                p = (await select(x for x in self.Person))[0]
                p.age = 31
            async with db_session:
                p = (await select(x for x in self.Person))[0]
                self.assertEqual(p.age, 31)

        asyncio.run(scenario())

    def test_sync_execution_forbidden_in_async_session(self):
        async def scenario():
            async with db_session:
                d = self.Dept(name="IT")
                self.Person(name="ann", age=30, dept=d)
            async with db_session:
                with self.assertRaises(TransactionError):
                    self.Person[1]

        asyncio.run(scenario())

    def test_cancellation_rolls_back_and_returns_connection(self):
        """Отмена корутины: откат + возврат соединения в пул (риск R2 спеки)."""

        async def scenario():
            async def work():
                async with db_session:
                    d = self.Dept(name="IT")
                    self.Person(name="ann", age=30, dept=d)
                    await asyncio.sleep(10)

            task = asyncio.create_task(work())
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            async with db_session:
                self.assertEqual(await select(x for x in self.Dept), [])
                self.assertEqual(await select(x for x in self.Person), [])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
