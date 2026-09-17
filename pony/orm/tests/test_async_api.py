import asyncio
import os
import unittest

import psycopg

from pony.orm import (
    Database,
    NotLoadedError,
    ObjectNotFound,
    Optional,
    PrimaryKey,
    Required,
    Set,
    TransactionError,
    commit,
    count,
    db_session,
    delete,
    flush,
    get,
    rollback,
    select,
    sum,
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

        class Pair(db.Entity):
            a = Required(int)
            b = Required(int)
            value = Optional(str)
            PrimaryKey(a, b)

        class Tag(db.Entity):
            name = Required(str)
            persons = Set("Person")

        class Person(db.Entity):
            name = Required(str)
            age = Optional(int)
            dept = Required(Dept)
            bio = Optional(str, lazy=True)
            tags = Set(Tag)

        cls.Dept, cls.Person, cls.Tag, cls.Pair = Dept, Person, Tag, Pair
        # сброс возможной устаревшей схемы от прошлых прогонов
        conn = psycopg.connect(DSN)
        conn.autocommit = True
        conn.cursor().execute(
            "DROP TABLE IF EXISTS person, dept, tag, pair, tag_person CASCADE"
        )
        conn.close()
        db.generate_mapping(create_tables=True)

    def setUp(self):
        conn = psycopg.connect(DSN)
        conn.autocommit = True
        conn.cursor().execute("TRUNCATE person, dept, tag, pair CASCADE")
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
                # sync-выборка среза в async-сессии — ошибка с подсказкой
                with self.assertRaises(TransactionError) as cm:
                    len(select(x for x in self.Person)[:1])
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


    def test_aggregate_queries(self):
        """Агрегаты в async-сессии: await query.count()/sum()/min()/max()/exists()/first()."""

        async def scenario():
            async with db_session:
                d = self.Dept(name="IT")
                self.Person(name="ann", age=30, dept=d)
                self.Person(name="bob", age=17, dept=d)
            async with db_session:
                self.assertEqual(await select(x for x in self.Person).count(), 2)
                self.assertEqual(await select(x.age for x in self.Person).sum(), 47)
                self.assertEqual(await select(x.age for x in self.Person).min(), 17)
                self.assertEqual(await select(x.age for x in self.Person).max(), 30)
                self.assertTrue(await select(x for x in self.Person).exists())
                first = await select(x for x in self.Person).order_by(self.Person.age).first()
                self.assertEqual(first.name, "bob")
                self.assertEqual(
                    (await select(x for x in self.Person if x.age == 30).get()).name, "ann"
                )
                # модульные агрегаты по генератору
                self.assertEqual(await count(x for x in self.Person), 2)
                self.assertEqual(await sum(x.age for x in self.Person), 47)

        asyncio.run(scenario())

    def test_slices_and_pagination(self):
        """await query[:n] / limit() / page() / fetch() — срезы в async-сессии."""

        async def scenario():
            async with db_session:
                d = self.Dept(name="IT")
                for name in ("ann", "bob", "cid"):
                    self.Person(name=name, dept=d)
            async with db_session:
                query = select(x for x in self.Person).order_by(self.Person.name)
                self.assertEqual([x.name for x in await query[:2]], ["ann", "bob"])
                self.assertEqual([x.name for x in await query.limit(1, offset=1)], ["bob"])
                self.assertEqual([x.name for x in await query.page(2, pagesize=2)], ["cid"])
                self.assertEqual([x.name for x in await query.fetch(limit=1)], ["ann"])
                # без await — понятная ошибка, а не None
                with self.assertRaises(TransactionError):
                    len(query[:2])

        asyncio.run(scenario())

    def test_lookup_by_attributes(self):
        """Person.get()/exists() и модульный get() в async-сессии."""

        async def scenario():
            async with db_session:
                self.Person(name="ann", age=30, bio="b1", dept=self.Dept(name="IT"))
            async with db_session:
                person = await self.Person.get(name="ann")
                self.assertEqual(person.name, "ann")
                self.assertTrue(await self.Person.exists(name="ann"))
                self.assertFalse(await self.Person.exists(name="zed"))
                self.assertEqual(await get(x.name for x in self.Person), "ann")
                # результат Entity[pk] нужно ожидать — иначе ошибка с подсказкой
                with self.assertRaises(TransactionError) as cm:
                    self.Person[123].name
                self.assertIn("await", str(cm.exception))

        asyncio.run(scenario())

    def test_explicit_transaction_control(self):
        """await flush() / commit() / rollback() внутри async-сессии."""

        async def scenario():
            # сессия ещё ни разу не обращалась к базе (кэш создаётся лениво):
            # await-формы обязаны вернуть корутину, а не None
            async with db_session:
                await flush()
                await rollback()
            async with db_session:
                self.Dept(name="IT")
                await flush()
                await rollback()
            async with db_session:
                self.assertEqual(await select(x for x in self.Dept).count(), 0)
                self.Dept(name="HR")
                await commit()
            async with db_session:
                self.assertEqual(await select(x for x in self.Dept).count(), 1)
                self.Dept(name="Sales")
                await flush()
                self.assertEqual(await select(x for x in self.Dept).count(), 2)

        asyncio.run(scenario())

    def test_bulk_delete(self):
        """delete(gen) и query.delete(bulk=True) в async-сессии.

        Для поштучного удаления коллекции объекта должны быть уже загружены
        (в async нет неявной догрузки); bulk-удаление коллекций не требует.
        """

        async def scenario():
            async with db_session:
                d = self.Dept(name="IT")
                self.Person(name="ann", dept=d)
                self.Person(name="bob", dept=d)
            async with db_session:
                person = await self.Person.get(name="bob")
                await person.tags                       # нужна загруженная m2m-коллекция
                self.assertEqual(await delete(x for x in self.Person if x.name == "bob"), 1)
            async with db_session:
                self.assertEqual(await select(x for x in self.Person).count(), 1)
                self.assertEqual(await select(x for x in self.Person).delete(bulk=True), 1)
            async with db_session:
                self.assertEqual(await select(x for x in self.Person).count(), 0)

        asyncio.run(scenario())

    def test_many_to_many(self):
        """m2m-мутации в async-сессии: создание, add, remove, откат."""

        async def load_tags(person):
            await person.tags
            for tag in person.tags:
                await tag.load()
            return sorted(tag.name for tag in person.tags)

        async def scenario():
            async with db_session:
                self.Person(
                    name="ann", dept=self.Dept(name="IT"), tags=[self.Tag(name="py")]
                )
            async with db_session:
                person = (await select(x for x in self.Person))[0]
                self.assertEqual(await load_tags(person), ["py"])
                person.tags.add(self.Tag(name="orm"))
            async with db_session:
                person = (await select(x for x in self.Person))[0]
                self.assertEqual(await load_tags(person), ["orm", "py"])
                orm_tag = [t for t in person.tags if t.name == "orm"][0]
                person.tags.remove(orm_tag)
            async with db_session:
                person = (await select(x for x in self.Person))[0]
                self.assertEqual(await load_tags(person), ["py"])
                person.tags.add(self.Tag(name="tmp"))
                raise RuntimeError("rollback")
            try:
                pass
            except RuntimeError:
                pass
            async with db_session:
                person = (await select(x for x in self.Person))[0]
                self.assertEqual(await load_tags(person), ["py"])

        try:
            asyncio.run(scenario())
        except RuntimeError:
            pass

    def test_await_entity_by_pk(self):
        """await Entity[pk] в async-сессии: кэш, запрос, составной ключ, отсутствие."""

        async def scenario():
            async with db_session:
                dept = self.Dept(name="IT")
                self.Pair(a=1, b=2, value="x")
                await flush()
                dept_pk = dept.id
            async with db_session:
                dept = await self.Dept[dept_pk]
                self.assertEqual(dept.name, "IT")
                # повторный доступ — попадание в identity map сессии
                self.assertIs(await self.Dept[dept_pk], dept)
                # тот же объект, что и в кортежном синтаксисе
                self.assertIs(await self.Dept[(dept_pk,)], dept)
                pair = await self.Pair[1, 2]
                self.assertEqual(pair.value, "x")
                with self.assertRaises(ObjectNotFound):
                    await self.Dept[123456]
                with self.assertRaises(TransactionError) as cm:
                    self.Dept[dept_pk].name
                self.assertIn("await", str(cm.exception))

        asyncio.run(scenario())

    def test_sync_only_paths_raise_clear_errors(self):
        """Sync-only операции в async-сессии дают TransactionError, а не None."""

        async def scenario():
            async with db_session:
                dept = self.Dept(name="IT")
                await flush()
                with self.assertRaises(TransactionError):
                    len(select(x for x in self.Dept)[:1])
                with self.assertRaises(TransactionError) as cm:
                    self.Dept[dept.id].name
                self.assertIn("await", str(cm.exception))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
