import datetime
import unittest

from pony.orm import *
from pony.orm.tests import db_params, only_for, teardown_database
from pony.orm.tests.testutils import *


class TestConstraintCheck(unittest.TestCase):
    def setUp(self):
        self.db = Database(**db_params)

    def tearDown(self):
        teardown_database(self.db)

    def test_1(self):  # rendering and enforcement
        db = self.db

        class Person(db.Entity):
            age = Required(int)
            name = Required(str)

            @constraint.check
            def adult(self):
                return self.age > 10

            @constraint.check(name="chk_len")
            def name_len(self):
                return len(self.name) <= 200

        db.generate_mapping(create_tables=True)

        script = db.schema.generate_create_script()
        table_name = Person._table_
        dialect = db.provider.dialect
        if dialect == "MySQL":
            constraint_sql = (
                "CONSTRAINT `chk_%s__adult` CHECK (`age` > 10)" % table_name
            )
        elif dialect == "SQLite" or dialect == "PostgreSQL":
            constraint_sql = 'CONSTRAINT "chk_%s__adult" CHECK ("age" > 10)' % table_name
        elif dialect == "Oracle":
            constraint_sql = (
                'CONSTRAINT "CHK_%s__ADULT" CHECK ("AGE" > 10)' % table_name.upper()
            )
        else:
            raise NotImplementedError
        self.assertIn(constraint_sql, script)
        self.assertIn("chk_len", script)

        with db_session:
            Person(name="Bob", age=30)
        with self.assertRaises(TransactionIntegrityError):
            with db_session:
                Person(name="Kid", age=5)
        with db_session:
            self.assertEqual(Person.get(age=30).name, "Bob")

    def test_2(self):  # class-level constants and inheritance merge
        db = self.db

        class Animal(db.Entity):
            MIN_AGE = 0
            age = Required(int)

            @constraint.check
            def positive_age(self):
                return self.age > MIN_AGE

        class Dog(Animal):
            bark = Required(str)

            @constraint.check
            def bark_len(self):
                return len(self.bark) > 1

        db.generate_mapping(create_tables=True)

        script = db.schema.generate_create_script()
        table_name = Animal._table_
        self.assertIn("chk_%s__positive_age" % table_name, script)
        self.assertIn("chk_%s__bark_len" % table_name, script)
        self.assertIn("CHECK", script)

        with db_session:
            Dog(age=3, bark="woof")
        with self.assertRaises(TransactionIntegrityError):
            with db_session:
                Dog(age=-1, bark="woof")

    def test_3(self):  # error cases
        db = self.db

        with self.assertRaises(TypeError):
            constraint.check(5)

        db1 = Database(**db_params)
        with self.assertRaises(TypeError):
            class Bad1(db1.Entity):
                age = Required(int)

                @constraint.check
                def multi_statement(self):
                    if self.age > 10:
                        return True
                    return False
            db1.generate_mapping()
        teardown_database(db1)

        db2 = Database(**db_params)
        with self.assertRaises(TypeError):
            class Bad2(db2.Entity):
                age = Required(int)

                @constraint.check
                def unknown_name(self):
                    return self.age > UNKNOWN
            db2.generate_mapping()
        teardown_database(db2)

        db3 = Database(**db_params)
        with self.assertRaises(TypeError):
            class Bad3(db3.Entity):
                age = Required(int)

                @constraint.check
                def two_params(self, x):
                    return self.age > x
            db3.generate_mapping()
        teardown_database(db3)

        db4 = Database(**db_params)
        with self.assertRaises(TypeError):
            class Bad4(db4.Entity):
                age = Required(int)

                @constraint.check(name="same")
                def first(self):
                    return self.age > 0

                @constraint.check(name="same")
                def second(self):
                    return self.age > 1
            db4.generate_mapping()
        teardown_database(db4)

        db5 = Database(**db_params)
        with self.assertRaises(TypeError):
            class Bad5(db5.Entity):
                @constraint.check
                def decorated_twice(self):
                    return True
                decorated_twice = constraint.check(decorated_twice)
        teardown_database(db5)


@only_for("postgres")
class TestConstraintCheckPostgreSQL(unittest.TestCase):
    def setUp(self):
        self.db = Database(**db_params)

    def tearDown(self):
        teardown_database(self.db)

    def test_literals(self):
        db = self.db

        class Event(db.Entity):
            created = Required(datetime.datetime)
            status = Required(str)

            @constraint.check
            def recent(self):
                return self.created < datetime.datetime(2024, 1, 1)

            @constraint.check
            def known_status(self):
                return self.status in ("new", "old")

        db.generate_mapping(create_tables=True)

        script = db.schema.generate_create_script()
        self.assertIn(
            'CONSTRAINT "chk_event__recent" CHECK '
            "(\"created\" < TIMESTAMP '2024-01-01 00:00:00.000000')",
            script,
        )
        self.assertIn(
            'CONSTRAINT "chk_event__known_status" CHECK '
            "(\"status\" IN ('new', 'old'))",
            script,
        )


if __name__ == "__main__":
    unittest.main()
