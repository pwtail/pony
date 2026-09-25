"""Async-режим без сервера БД: эмуляция на sqlite + SyncOps.

Async-провайдер эмулируется подстановкой SyncOps в качестве async_ops: все
ops-операции завершаются синхронно, поэтому корутины async-сессии можно
крутить через drive() без event loop. Логика async-путей (сессия, Gen-код,
guards) при этом исполняется настоящая. Тесты регрессий, найденных review;
бойовые async-тесты на postgres/mariadb — test_async_api.py и далее.
"""

import gc
import inspect
import sys
import unittest
import warnings
from contextlib import contextmanager

from pony.orm import (
    Database,
    ObjectNotFound,
    Optional,
    PrimaryKey,
    Required,
    Set,
    TransactionError,
    UnrepeatableReadError,
    db_session,
    select,
)
from pony.orm import core
from pony.orm.core_gen import load_collection_gen, load_many_gen
from pony.orm.drive import drive
from pony.orm.ops import AsyncOps, SyncOps
from pony.orm.session_cache import SyncSessionCache


def make_db(**entities):
    """sqlite :memory: + маркеры async-режима: async_ops = SyncOps."""
    db = Database("sqlite", ":memory:")
    namespace = {
        "db": db,
        "Required": Required,
        "Optional": Optional,
        "Set": Set,
        "PrimaryKey": PrimaryKey,
    }
    for name, source in entities.items():
        exec(source, namespace)
    db.generate_mapping(create_tables=True)
    db.provider.async_pool = object()  # маркер async-capable для _get_cache
    db.provider.async_ops = SyncOps(db.provider)
    return db


@contextmanager
def async_session(**kwargs):
    """Вход/выход async-сессии, задрайвленные вручную (без event loop)."""
    ds = db_session(**kwargs)
    drive(ds.__aenter__())
    try:
        yield ds
    except BaseException:
        drive(ds.__aexit__(*sys.exc_info()))
        raise
    else:
        drive(ds.__aexit__(None, None, None))


def aiter_collect(aiterable):
    async def collect():
        return [item async for item in aiterable]

    return drive(collect())


PERSON = """
class Person(db.Entity):
    name = Required(str)
    age = Optional(int)
"""

DEPT_PERSON = """
class Dept(db.Entity):
    name = Required(str)
    persons = Set('Person')

class Person(db.Entity):
    name = Required(str)
    dept = Required(Dept)
"""

M2M = """
class Tag(db.Entity):
    name = Required(str)
    persons = Set('Person')

class Person(db.Entity):
    name = Required(str)
    tags = Set(Tag)
"""

M2M_INHERIT = """
class Tag(db.Entity):
    name = Required(str)
    persons = Set('Person')

class Person(db.Entity):
    name = Required(str)
    tags = Set(Tag)

class Employee(Person):
    salary = Optional(int)
"""

RAW_PK = """
class Pair(db.Entity):
    a = Required(int)
    b = Required(int)
    value = Optional(str)
    PrimaryKey(a, b)
    links = Set('Link')

class Link(db.Entity):
    pair = PrimaryKey(Pair)
    label = Required(str)
"""


class TestAsyncAggregate(unittest.TestCase):
    def test_repeated_await_of_same_aggregate(self):
        # регрессия: повторный await q.count() возвращал значение, а не корутину
        db = make_db(PERSON=PERSON)
        with db_session:
            db.Person(name="a")
        with async_session():
            q = select(x for x in db.Person)
            first = q.count()
            self.assertTrue(inspect.iscoroutine(first))
            self.assertEqual(drive(first), 1)
            second = q.count()  # попадание в query_results
            self.assertTrue(inspect.iscoroutine(second))
            self.assertEqual(drive(second), 1)
            total = select(x.age for x in db.Person).sum()
            self.assertTrue(inspect.iscoroutine(total))
            self.assertEqual(drive(total), 0)


class TestAsyncRollbackIsAwaited(unittest.TestCase):
    def test_allowed_exceptions_crash_rolls_back(self):
        # регрессия: rollback() без await — откат не происходил
        db = make_db(PERSON=PERSON)

        def bad_allowed(exc):
            raise RuntimeError("allowed_exceptions blew up")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                with async_session(allowed_exceptions=bad_allowed):
                    db.Person(name="x")
                    raise ValueError("body failed")
            except RuntimeError as e:
                self.assertEqual(str(e), "allowed_exceptions blew up")
            else:
                self.fail("ожидали RuntimeError из allowed_exceptions")
            gc.collect()
        unawaited = [w for w in caught if "was never awaited" in str(w.message)]
        self.assertEqual(unawaited, [])
        with db_session:
            self.assertEqual(select(x for x in db.Person).count(), 0)


class TestAsyncPkLookup(unittest.TestCase):
    def test_lookup_on_discriminator_seed(self):
        # регрессия: await Person[pk] на seed-объекте с иерархией — NotLoadedError
        db = make_db(M2M_INHERIT=M2M_INHERIT)
        with db_session:
            t = db.Tag(name="t1")
            db.Employee(name="e1", salary=10, tags=[t])
        with async_session():
            t = drive(db.Tag.select().first())
            drive(load_collection_gen(t, db.Tag.persons))  # m2m -> seeds
            seed = next(iter(t.persons))
            self.assertFalse(seed._dbvals_)
            obj = drive(db.Person[seed._pkval_])
            self.assertIs(obj, seed)
            self.assertIs(type(obj), db.Employee)
            self.assertEqual(obj.salary, 10)

    def test_failed_raw_key_lookup_leaves_no_ghost_seed(self):
        # регрессия: seed от неудачного raw-key lookup оставался в identity map
        db = make_db(RAW_PK=RAW_PK)
        with db_session:
            pair = db.Pair(a=1, b=2, value="p")
            db.Link(pair=pair, label="l1")
        with async_session():
            link = drive(db.Link[1, 2])
            self.assertEqual(link.label, "l1")
            cache = db._get_cache()
            with self.assertRaises(ObjectNotFound):
                drive(db.Link[9, 9])
            self.assertFalse(cache.seeds[db.Link._pk_attrs_])
            for pair_seed in cache.seeds[db.Pair._pk_attrs_]:
                if pair_seed._pkval_ == (9, 9):
                    self.assertFalse(pair_seed._vals_.get(db.Pair.links))
            self.assertIs(drive(db.Link[1, 2]), link)


class TestCollectionOwnership(unittest.TestCase):
    def test_collection_of_another_transaction_rejected(self):
        # регрессия: load_collection_gen потерял проверку владельца транзакции
        db = make_db(DEPT_PERSON=DEPT_PERSON)
        with db_session:
            d = db.Dept(name="d")
            db.Person(name="p", dept=d)
        with async_session():
            cache = db._get_cache()
            d = drive(db.Dept.select().first())
            foreign = SyncSessionCache(db)
            core.local.db2cache[db] = foreign  # чужой кэш == другая задача
            try:
                with self.assertRaises(TransactionError):
                    drive(load_collection_gen(d, db.Dept.persons))
            finally:
                core.local.db2cache[db] = cache


class TestSyncOnlyGuards(unittest.TestCase):
    def test_sync_only_paths_raise_in_async_session(self):
        db = make_db(PERSON=PERSON)
        with async_session():
            p = db.Person(name="x")
            with self.assertRaises(TransactionError):
                p.to_dict()
            with self.assertRaises(TransactionError):
                p.flush()
            with self.assertRaises(TransactionError):
                db.from_json('{"objects": []}')

    def test_async_session_inside_sync_rejected(self):
        db = make_db(PERSON=PERSON)
        with db_session:
            with self.assertRaises(TransactionError):
                drive(db_session.__aenter__())

    def test_nested_async_sessions_allowed(self):
        db = make_db(PERSON=PERSON)
        drive(db_session.__aenter__())
        drive(db_session.__aenter__())
        drive(db_session.__aexit__(None, None, None))
        drive(db_session.__aexit__(None, None, None))


class TestAsyncInsertAndGetConnection(unittest.TestCase):
    def test_insert_and_get_connection(self):
        db = make_db(PERSON=PERSON)
        with async_session():
            new_id = drive(db.insert("Person", name="a", age=1))
            self.assertEqual(new_id, 1)
            rowid = drive(db.insert("Person", name="b", age=2, returning="id"))
            self.assertEqual(rowid, 2)
            con = drive(db.get_connection())
            self.assertIs(con, db._get_cache().connection)
        with async_session():
            self.assertEqual(drive(select(x for x in db.Person).count()), 2)


class TestAsyncIteration(unittest.TestCase):
    def test_async_for_over_query_and_slice(self):
        db = make_db(PERSON=PERSON)
        with db_session:
            for n in "abc":
                db.Person(name=n)
        with async_session():
            persons = aiter_collect(
                select(x for x in db.Person).order_by(db.Person.name)
            )
            self.assertEqual([p.name for p in persons], ["a", "b", "c"])
            persons = aiter_collect(
                select(x for x in db.Person).order_by(db.Person.name)[:2]
            )
            self.assertEqual([p.name for p in persons], ["a", "b"])


class TestOpsShape(unittest.TestCase):
    def test_async_ops_are_plain_functions(self):
        # анти-паттерн async def wrapper: return await coro — лишний фрейм
        for name in (
            "connect", "set_transaction_mode", "execute", "fetchone",
            "fetchmany", "fetchall", "commit", "rollback", "release", "drop",
        ):
            self.assertFalse(
                inspect.iscoroutinefunction(getattr(AsyncOps, name)), name
            )
            self.assertTrue(
                inspect.iscoroutinefunction(getattr(SyncOps, name)), name
            )


class TestDrive(unittest.TestCase):
    def test_drive_closes_suspended_coroutine(self):
        import asyncio

        async def suspending():
            await asyncio.sleep(0)

        coro = suspending()
        with self.assertRaises(RuntimeError):
            drive(coro)
        self.assertEqual(inspect.getcoroutinestate(coro), "CORO_CLOSED")


class TestLoadManyPhantom(unittest.TestCase):
    def test_phantom_seed_detected(self):
        # проверка была перевёрнута (унаследовано от апстрима) и не срабатывала
        db = make_db(M2M=M2M)
        with db_session:
            t = db.Tag(name="t")
            db.Person(name="p1", tags=[t])
            db.Person(name="p2", tags=[t])
        with async_session():
            t = drive(db.Tag.select().first())
            drive(load_collection_gen(t, db.Tag.persons))
            drive(db.execute("delete from Person where name='p2'"))
            with self.assertRaises(UnrepeatableReadError):
                drive(load_many_gen(db.Person, list(t.persons)))


class TestContextLocal(unittest.TestCase):
    def test_task_isolation(self):
        import asyncio

        from pony.utils.utils import ContextLocal

        class L(ContextLocal):
            def _init_context(self):
                self.x = 0

        l = L()
        l.x = 5

        async def worker(n, results):
            results[n] = l.x  # re-init: унаследованное значение не протекает
            l.x = n * 10
            await asyncio.sleep(0)
            results[n * 100] = l.x

        async def main():
            results = {}
            await asyncio.gather(worker(1, results), worker(2, results))
            return results

        res = asyncio.run(main())
        self.assertEqual(res, {1: 0, 2: 0, 100: 10, 200: 20})
        self.assertEqual(l.x, 5)


if __name__ == "__main__":
    unittest.main()
