import asyncio
import os
import unittest

import psycopg

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

DSN = os.environ.get("PONY_ASYNC_DSN", "dbname=pony_async_test user=postgres host=localhost")


def _postgres_available():
    try:
        conn = psycopg.connect(DSN)
    except Exception:
        return False
    else:
        conn.close()
        return True


@unittest.skipUnless(
    _postgres_available(), "postgres database 'pony_async_test' is not available"
)
class TestAsyncAPI(unittest.TestCase):
    """The async public API: async db_session, await select, load, guards."""

    @classmethod
    def setUpClass(cls):
        db = cls.db = Database("postgres_async", DSN)

        class Dept(db.Entity):
            name = Required(str)
            persons = Set("Person")

        class Person(db.Entity):
            name = Required(str)
            dept = Required(Dept)
            bio = Optional(str, lazy=True)

        cls.Dept, cls.Person = Dept, Person
        # сброс возможной устаревшей схемы от прошлых прогонов
        conn = psycopg.connect(DSN)
        conn.autocommit = True
        conn.cursor().execute("DROP TABLE IF EXISTS person, dept CASCADE")
        conn.close()
        db.generate_mapping(create_tables=True)

    def setUp(self):
        conn = psycopg.connect(DSN)
        conn.autocommit = True
        conn.cursor().execute("TRUNCATE person, dept CASCADE")
        conn.close()

    def test_create_commit_on_session_exit(self):
        async def scenario():
            async with db_session:
                d = self.Dept(name="IT")
                self.Person(name="ann", dept=d, bio="b1")
            async with db_session:
                objs = await select(x for x in self.Person)
                self.assertEqual([x.name for x in objs], ["ann"])

        asyncio.run(scenario())

    def test_await_select_and_async_for(self):
        async def scenario():
            async with db_session:
                d = self.Dept(name="IT")
                self.Person(name="ann", dept=d)
                self.Person(name="bob", dept=d)
            async with db_session:
                objs = await select(x for x in self.Person).order_by(self.Person.id)
                self.assertEqual([x.name for x in objs], ["ann", "bob"])
                names = []
                async for x in select(x for x in self.Person).order_by(self.Person.id):
                    names.append(x.name)
                self.assertEqual(names, ["ann", "bob"])

        asyncio.run(scenario())

    def test_collection_await_and_iteration(self):
        async def scenario():
            async with db_session:
                d = self.Dept(name="IT")
                self.Person(name="ann", dept=d)
                self.Person(name="bob", dept=d)
            async with db_session:
                d = (await select(x for x in self.Dept))[0]
                with self.assertRaises(NotLoadedError):
                    [p.name for p in d.persons]
                await d.persons
                self.assertEqual(
                    sorted(p.name for p in d.persons), ["ann", "bob"]
                )
                seen = []
                async for p in d.persons:
                    seen.append(p.name)
                self.assertEqual(sorted(seen), ["ann", "bob"])

        asyncio.run(scenario())

    def test_lazy_and_seed_attributes_require_load(self):
        async def scenario():
            async with db_session:
                d = self.Dept(name="IT")
                self.Person(name="ann", dept=d, bio="b1")
            async with db_session:
                p = (await select(x for x in self.Person))[0]
                # dept — seed-объект: его атрибуты требуют load
                with self.assertRaises(NotLoadedError):
                    p.dept.name
                await p.load("dept")
                self.assertEqual(p.dept.name, "IT")
                # lazy-атрибут
                with self.assertRaises(NotLoadedError):
                    p.bio
                await p.load("bio")
                self.assertEqual(p.bio, "b1")
                p.bio = "bios"
                await p.load()
                self.assertEqual(p.bio, "bios")

        asyncio.run(scenario())

    def test_write_flushed_on_session_exit(self):
        async def scenario():
            async with db_session:
                d = self.Dept(name="IT")
                self.Person(name="ann", dept=d)
            async with db_session:
                p = (await select(x for x in self.Person))[0]
                p.name = "anna"
            async with db_session:
                objs = await select(x for x in self.Person)
                self.assertEqual([x.name for x in objs], ["anna"])

        asyncio.run(scenario())

    def test_rollback_on_exception(self):
        async def scenario():
            async with db_session:
                d = self.Dept(name="IT")
                self.Person(name="ann", dept=d)
            try:
                async with db_session:
                    self.Person(name="bob", dept=(await select(x for x in self.Dept))[0])
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            async with db_session:
                objs = await select(x for x in self.Person)
                self.assertEqual([x.name for x in objs], ["ann"])

        asyncio.run(scenario())

    def test_sync_session_forbidden_in_coroutine(self):
        async def scenario():
            with self.assertRaises(TransactionError) as cm:
                with db_session:
                    pass
            self.assertIn("async with", str(cm.exception))

        asyncio.run(scenario())

    def test_sync_execution_forbidden_in_async_session(self):
        async def scenario():
            async with db_session:
                d = self.Dept(name="IT")
                self.Person(name="ann", dept=d)
            async with db_session:
                with self.assertRaises(TransactionError) as cm:
                    self.Person[1]
                self.assertIn("async", str(cm.exception))

        asyncio.run(scenario())

    def test_task_interleaving(self):
        """Две задачи, две сессии, один event loop: пул выдаёт два соединения,
        identity map каждой задачи изолирован."""

        async def worker(n):
            async with db_session:
                d = self.Dept(name=f"D{n}")
                p = self.Person(name=f"p{n}", dept=d)
                await asyncio.sleep(0.02)
                objs = await select(x for x in self.Person)  # flush перед select
                self.assertTrue(any(o is p for o in objs))  # тот же экземпляр

        async def scenario():
            await asyncio.gather(worker(1), worker(2))
            async with db_session:
                objs = await select(x for x in self.Dept)
                self.assertEqual(sorted(x.name for x in objs), ["D1", "D2"])

        asyncio.run(scenario())

    def test_cancellation_rolls_back_and_returns_connection(self):
        """Отмена корутины внутри async-сессии: откат и возврат соединения в пул."""

        async def scenario():
            async def work():
                async with db_session:
                    d = self.Dept(name="IT")
                    self.Person(name="ann", dept=d)
                    await asyncio.sleep(10)

            task = asyncio.create_task(work())
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            # незакоммиченные изменения откатаны, соединение вернулось в пул
            async with db_session:
                objs = await select(x for x in self.Dept)
                self.assertEqual(objs, [])
                objs = await select(x for x in self.Person)
                self.assertEqual(objs, [])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
