import unittest

from pony.orm import (
    Database,
    IOForbiddenError,
    Optional,
    Required,
    Set,
    commit,
    db_session,
    flush,
    io,
    rollback,
    select,
)

import pony.options

db = Database("sqlite", ":memory:")


class Department(db.Entity):
    name = Required(str)
    persons = Set("Person")


class Person(db.Entity):
    name = Required(str)
    dept = Optional(Department, lazy=True)


class TestIOScope(unittest.TestCase):
    """The 'with io:' scope: database access allowed only inside the block.

    The legacy suite runs with pony.options.IO_GUARD=False; this module
    re-enables the guard and exercises the scope itself.
    """

    @classmethod
    def setUpClass(cls):
        pony.options.IO_GUARD = True
        with io:
            db.generate_mapping(create_tables=True)

    @classmethod
    def tearDownClass(cls):
        pony.options.IO_GUARD = False
        db.provider.disconnect()

    def test_queries_forbidden_outside_io(self):
        with db_session:
            with io:
                p = Person(name="AnnQueriesForbidden")
                commit()
        with db_session:
            for op in (
                lambda: list(select(x for x in Person)),
                lambda: Person[p.id],  # p не в кэше этой сессии
                lambda: Person.get(name="AnnQueriesForbidden"),
            ):
                with self.assertRaises(IOForbiddenError):
                    op()

    def test_queries_allowed_inside_io(self):
        with db_session:
            with io:
                p = Person(name="AnnQueriesAllowed")
                names = [x.name for x in select(x for x in Person)]
                self.assertIn("AnnQueriesAllowed", names)
                self.assertIs(Person[p.id], p)
                with io:  # вложенные блоки допустимы
                    self.assertIs(Person.get(name="AnnQueriesAllowed"), p)

    def test_lazy_load_forbidden_outside_io(self):
        with db_session:
            with io:
                d = Department(name="IT")
                p = Person(name="AnnLazy", dept=d)
                p.dept  # ленивая загрузка внутри io разрешена
                p2 = Person(name="BobLazy")
                commit()
            self.assertIs(p.dept, d)  # уже загружено: читается вне io
            with self.assertRaises(IOForbiddenError):
                p2.dept  # не загружено: запрещено

    def test_writes_allowed_outside_io(self):
        with db_session:
            with io:
                p = Person(name="AnnWrites")
                commit()
            p.name = "AnnaWrites"  # только память, без I/O
        # выход из сессии отправляет запись в БД через внутренний bypass
        with db_session:
            with io:
                self.assertEqual(Person[p.id].name, "AnnaWrites")

    def test_transaction_machinery_outside_io(self):
        with db_session:
            with io:
                p = Person(name="CyrilTx")
            flush()  # транзакционная механика освобождена от guard
            p.name = "CyrilTx2"
            commit()
        with db_session:
            with io:
                self.assertEqual(Person[p.id].name, "CyrilTx2")
        try:
            with db_session:
                with io:
                    Person(name="DinaTx")
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass
        rollback()
        with db_session:
            with io:
                names = {x.name for x in select(x for x in Person)}
                self.assertNotIn("DinaTx", names)

    def test_io_without_db_session(self):
        from pony.orm.core import TransactionError

        with io:
            with self.assertRaises(TransactionError):
                list(select(x for x in Person))

    def test_io_as_decorator(self):
        # `io` работает и как декоратор функции: доступ к БД разрешён в теле
        with db_session:
            with io:
                Person(name="AnnDecorator")
                commit()

        @io
        def fetch_names():
            with db_session:
                return [x.name for x in select(x for x in Person)]

        self.assertIn("AnnDecorator", fetch_names())

    def test_io_bypass_as_decorator(self):
        # внутренний io_bypass тоже работает как декоратор:
        # снимает guard без блока `with io:`
        from pony.orm.core import io_bypass

        with db_session:
            with io:
                Person(name="AnnBypassDecorator")
                commit()

        @io_bypass
        def raw_read():
            with db_session:
                return [x.name for x in select(x for x in Person)]

        self.assertIn("AnnBypassDecorator", raw_read())


if __name__ == "__main__":
    unittest.main()
