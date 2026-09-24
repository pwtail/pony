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
            links = Set("Link")

        class Link(db.Entity):
            pair = PrimaryKey(Pair)          # pk = related-объект (составной ключ)
            label = Required(str)

        class Passport(db.Entity):
            number = Required(str)
            person = Required("Person")

        class Tag(db.Entity):
            name = Required(str)
            persons = Set("Person")

        class Person(db.Entity):
            name = Required(str)
            age = Optional(int)
            dept = Required(Dept)
            bio = Optional(str, lazy=True)
            tags = Set(Tag)
            passport = Optional(Passport)

        cls.Dept, cls.Person, cls.Tag, cls.Pair, cls.Link, cls.Passport = (
            Dept,
            Person,
            Tag,
            Pair,
            Link,
            Passport,
        )
        # сброс возможной устаревшей схемы от прошлых прогонов
        conn = psycopg.connect(DSN)
        conn.autocommit = True
        conn.cursor().execute(
            "DROP TABLE IF EXISTS person, dept, tag, pair, link, passport, "
            "tag_person CASCADE"
        )
        conn.close()
        db.generate_mapping(create_tables=True)

    def setUp(self):
        conn = psycopg.connect(DSN)
        conn.autocommit = True
        conn.cursor().execute("TRUNCATE person, dept, tag, pair, link, passport CASCADE")
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

    def test_raw_sql(self):
        """Сырой SQL в async-сессии: db.select / get / exists / execute."""

        async def scenario():
            async with db_session:
                self.Dept(name="IT")
            async with db_session:
                x = "IT"
                self.assertEqual(await self.db.select("name from dept where name = $x"), ["IT"])
                self.assertEqual(await self.db.get("select name from dept where name = $x"), "IT")
                self.assertTrue(await self.db.exists("select 1 from dept where name = $x"))
                self.assertFalse(await self.db.exists("select 1 from dept where name = 'ZZ'"))
                await self.db.execute("update dept set name = 'HR' where name = $x")
            async with db_session:
                self.assertEqual(await self.db.get("select name from dept"), "HR")

        asyncio.run(scenario())

    def test_modes_cannot_be_mixed(self):
        """Sync-сессия внутри async-сессии — ошибка; вне async-сессии sync-код можно."""

        async def scenario():
            async with db_session:
                with self.assertRaises(TransactionError) as cm:
                    with db_session:
                        pass
                self.assertIn("async with", str(cm.exception))
            # вне async-сессии синхронный код работает и внутри корутины:
            # это осознанный блокирующий вызов (критерий — открытая async-сессия)
            with db_session:
                self.Dept(name="sync-in-coroutine")

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
                await commit()          # в сессии без обращений к базе
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
                await person.load("passport")           # и обратная связь без колонок
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

    def test_raw_primary_key_lookup(self):
        """await Entity[raw-pk]: pk — related-объект с составным ключом."""

        async def scenario():
            async with db_session:
                pair = self.Pair(a=1, b=2, value="x")
                self.Link(pair=pair, label="L")
            async with db_session:
                link = await self.Link[1, 2]          # «сырые» колонки pk
                self.assertEqual(link.label, "L")
                self.assertIs(await self.Link[1, 2], link)
                pair = await self.Pair[1, 2]          # seed догружен запросом
                self.assertEqual(pair.value, "x")
                with self.assertRaises(ObjectNotFound):
                    await self.Link[9, 9]

        asyncio.run(scenario())

    def test_load_reverse_attribute_without_columns(self):
        """load() обратной стороны связи «один к одному»."""

        async def scenario():
            async with db_session:
                person = self.Person(name="ann", dept=self.Dept(name="IT"))
                self.Passport(number="123", person=person)
            async with db_session:
                person = await self.Person.get(name="ann")
                with self.assertRaises(NotLoadedError):
                    person.passport
                await person.load("passport")
                self.assertEqual(person.passport.number, "123")
                self.assertEqual(person.passport.person.name, "ann")   # загружен целиком

        asyncio.run(scenario())

    def test_prefetch(self):
        """prefetch() в async-сессии: связи догружаются батчами."""

        async def scenario():
            async with db_session:
                for i in range(3):
                    dept = self.Dept(name=f"d{i}")
                    self.Person(name=f"p{i}", dept=dept, tags=[self.Tag(name=f"t{i}")])
            async with db_session:
                persons = await select(x for x in self.Person).prefetch(self.Person.dept)
                self.assertEqual(
                    sorted({p.dept.name for p in persons}), ["d0", "d1", "d2"]
                )   # to-one связь прочитана без await
            async with db_session:
                depts = await select(x for x in self.Dept).prefetch(self.Dept.persons)
                self.assertEqual(sorted(len(d.persons) for d in depts), [1, 1, 1])
            async with db_session:
                tags = await select(x for x in self.Tag).prefetch(self.Tag.persons)
                self.assertEqual(sorted(len(t.persons) for t in tags), [1, 1, 1])

        asyncio.run(scenario())

    def test_db_session_decorator_on_coroutine(self):
        """@db_session на async-функции: commit, откат и retry."""

        @db_session
        async def add_dept(name):
            self.Dept(name=name)
            return name

        @db_session
        async def fail_to_add():
            self.Dept(name="should-not-survive")
            raise ValueError("boom")

        attempts = []

        @db_session(retry=2, retry_exceptions=[ZeroDivisionError])
        async def flaky():
            attempts.append(1)
            if len(attempts) < 2:
                raise ZeroDivisionError
            self.Dept(name="retried")

        async def scenario():
            self.assertEqual(await add_dept("IT"), "IT")
            with self.assertRaises(ValueError):
                await fail_to_add()
            await flaky()
            async with db_session:
                names = sorted(d.name for d in await select(x for x in self.Dept))
                self.assertEqual(names, ["IT", "retried"])     # без should-not-survive
            self.assertEqual(len(attempts), 2)

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

    def test_with_db_context_manager(self):
        """async with db: ≡ async with db_session:"""

        async def scenario():
            async with self.db:
                d = self.Dept(name="IT")
                self.Person(name="ann", dept=d)
            async with db_session:
                objs = await select(x for x in self.Person)
                self.assertEqual([x.name for x in objs], ["ann"])

        asyncio.run(scenario())

    def test_with_db_nested(self):
        """вложенный async with db: внутри async with db_session: игнорируется"""

        async def scenario():
            async with db_session:
                async with self.db:
                    self.Dept(name="IT")
            async with db_session:
                self.assertEqual(await select(x for x in self.Dept).count(), 1)

        asyncio.run(scenario())

    def test_with_db_rollback_on_exception(self):
        """исключение внутри async with db: откатывает изменения"""

        async def scenario():
            async with self.db:
                self.Dept(name="IT")
            try:
                async with self.db:
                    self.Dept(name="should-not-survive")
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            async with db_session:
                names = sorted(d.name for d in await select(x for x in self.Dept))
                self.assertEqual(names, ["IT"])

        asyncio.run(scenario())

    def test_db_session_attr(self):
        """db.session — per-database скоуп, привязанный к этой базе"""

        async def scenario():
            self.assertIs(self.db.session.database, self.db)
            self.assertIsNot(self.db.session, db_session)
            self.assertIs(self.db.session(), self.db.session)
            async with self.db.session():
                self.Dept(name="IT")
            async with db_session:
                self.assertEqual(await select(x for x in self.Dept).count(), 1)

        asyncio.run(scenario())

    def test_global_db_session_inside_scoped_grants_all(self):
        # глобальный db_session внутри async with db: разрешает использовать всё
        other, OtherDept = self._make_other_db()

        async def scenario():
            async with self.db:
                async with db_session:
                    OtherDept(name="other")
                    self.Dept(name="IT")
            async with db_session:
                self.assertEqual(await select(x for x in OtherDept).count(), 1)
                self.assertEqual(await select(x for x in self.Dept).count(), 1)

        asyncio.run(scenario())

    def test_with_db_nested_other_db(self):
        # async with db2: внутри async with db: разрешён; разрешения копятся
        other, OtherDept = self._make_other_db()

        async def scenario():
            async with self.db:
                async with other:
                    OtherDept(name="other")
                    self.Dept(name="IT")
                # разрешение на other живёт до конца внешнего скоупа
                OtherDept(name="other-2")
            async with db_session:
                self.assertEqual(await select(x for x in OtherDept).count(), 2)
                self.assertEqual(await select(x for x in self.Dept).count(), 1)

        asyncio.run(scenario())

    def test_db_scoped_inside_global_grants_all(self):
        # async with db2: внутри db_session: — доступно всё
        other, OtherDept = self._make_other_db()

        async def scenario():
            async with db_session:
                async with other:
                    OtherDept(name="other")
                    self.Dept(name="IT")
            async with db_session:
                self.assertEqual(await select(x for x in OtherDept).count(), 1)
                self.assertEqual(await select(x for x in self.Dept).count(), 1)

        asyncio.run(scenario())

    def _make_other_db(self):
        other = Database("postgres_async", DSN)

        class OtherDept(other.Entity):
            name = Required(str)

        other.generate_mapping(create_tables=True)

        def drop():
            conn = psycopg.connect(DSN)
            conn.autocommit = True
            conn.cursor().execute("DROP TABLE IF EXISTS otherdept CASCADE")
            conn.close()

        self.addCleanup(drop)
        return other, OtherDept

    def test_db_session_kwargs(self):
        """async with db.session(immediate=True): ≡ db_session(immediate=True)"""

        async def scenario():
            async with self.db.session(immediate=True):
                self.Dept(name="IT")
            async with db_session:
                self.assertEqual(await select(x for x in self.Dept).count(), 1)

        asyncio.run(scenario())

    def test_db_decorator_on_coroutine(self):
        """@db.session на корутине ≡ @db_session"""

        @self.db.session
        async def add_dept(name):
            self.Dept(name=name)
            return name

        async def scenario():
            self.assertEqual(await add_dept("IT"), "IT")
            async with db_session:
                names = sorted(d.name for d in await select(x for x in self.Dept))
                self.assertEqual(names, ["IT"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
