import unittest

from pony.orm import *
from pony.orm.tests import db_params, only_for, teardown_database
from pony.orm.tests.testutils import *


class TestIndexes(unittest.TestCase):
    def setUp(self):
        self.db = Database(**db_params)

    def tearDown(self):
        teardown_database(self.db)

    def test_1(self):
        db = self.db

        class Person(db.Entity):
            name = Required(str)
            age = Required(int)
            composite_key(name, "age")

        db.generate_mapping(create_tables=True)

        i1, i2 = Person._indexes_
        self.assertEqual(i1.attrs, (Person.id,))
        self.assertEqual(i1.is_pk, True)
        self.assertEqual(i1.is_unique, True)
        self.assertEqual(i2.attrs, (Person.name, Person.age))
        self.assertEqual(i2.is_pk, False)
        self.assertEqual(i2.is_unique, True)

        table_name = (
            "Person"
            if db.provider.dialect == "SQLite" and pony.__version__ < "0.9"
            else "person"
        )
        table = db.schema.tables[table_name]
        name_column = table.column_dict["name"]
        age_column = table.column_dict["age"]
        self.assertEqual(len(table.indexes), 2)
        db_index = table.indexes[name_column, age_column]
        self.assertEqual(db_index.is_pk, False)
        self.assertEqual(db_index.is_unique, True)

    def test_2(self):
        db = self.db

        class Person(db.Entity):
            name = Required(str)
            age = Required(int)
            composite_index(name, "age")

        db.generate_mapping(create_tables=True)

        i1, i2 = Person._indexes_
        self.assertEqual(i1.attrs, (Person.id,))
        self.assertEqual(i1.is_pk, True)
        self.assertEqual(i1.is_unique, True)
        self.assertEqual(i2.attrs, (Person.name, Person.age))
        self.assertEqual(i2.is_pk, False)
        self.assertEqual(i2.is_unique, False)

        table_name = (
            "Person"
            if db.provider.dialect == "SQLite" and pony.__version__ < "0.9"
            else "person"
        )
        table = db.schema.tables[table_name]
        name_column = table.column_dict["name"]
        age_column = table.column_dict["age"]
        self.assertEqual(len(table.indexes), 2)
        db_index = table.indexes[name_column, age_column]
        self.assertEqual(db_index.is_pk, False)
        self.assertEqual(db_index.is_unique, False)

        create_script = db.schema.generate_create_script()

        dialect = self.db.provider.dialect
        if pony.__version__ < "0.9":
            if dialect == "SQLite":
                index_sql = (
                    'CREATE INDEX "idx_person__name_age" ON "Person" ("name", "age")'
                )
            else:
                index_sql = (
                    'CREATE INDEX "idx_person__name_age" ON "person" ("name", "age")'
                )
        elif dialect == "MySQL" or dialect == "SQLite":
            index_sql = (
                "CREATE INDEX `idx_person__name__age` ON `person` (`name`, `age`)"
            )
        elif dialect == "PostgreSQL":
            index_sql = (
                'CREATE INDEX "idx_person__name__age" ON "person" ("name", "age")'
            )
        elif dialect == "Oracle":
            index_sql = (
                'CREATE INDEX "IDX_PERSON__NAME__AGE" ON "PERSON" ("NAME", "AGE")'
            )
        else:
            raise NotImplementedError
        self.assertIn(index_sql, create_script)

    def test_3(self):
        db = self.db

        class User(db.Entity):
            name = Required(str, unique=True)

        db.generate_mapping(create_tables=True)

        with db_session:
            u = User(id=1, name="A")

        with db_session:
            u = User[1]
            u.name = "B"

        with db_session:
            u = User[1]
            self.assertEqual(u.name, "B")

    def test_4(self):  # issue 321
        db = self.db

        class Person(db.Entity):
            name = Required(str)
            age = Required(int)
            composite_key(name, age)

        db.generate_mapping(create_tables=True)
        with db_session:
            p1 = Person(id=1, name="John", age=19)

        with db_session:
            p1 = Person[1]
            p1.set(name="John", age=19)
            p1.delete()

    def test_5(self):  # named composite_index
        db = self.db

        class Person(db.Entity):
            a = Required(str)
            b = Required(int)
            composite_index(a, b, name="idx_ab")

        db.generate_mapping(create_tables=True)

        i1, i2 = Person._indexes_
        self.assertEqual(i2.name, "idx_ab")
        table = db.schema.tables[Person._table_]
        db_index = table.indexes[table.column_dict["a"], table.column_dict["b"]]
        self.assertEqual(db_index.name, "idx_ab")
        self.assertEqual(db_index.is_named, True)
        self.assertEqual(db_index.is_unique, False)

        script = db.schema.generate_create_script()
        dialect = db.provider.dialect
        if dialect == "MySQL" or dialect == "SQLite":
            index_sql = "CREATE INDEX `idx_ab` ON `%s` (`a`, `b`)" % Person._table_
        elif dialect == "PostgreSQL":
            index_sql = 'CREATE INDEX "idx_ab" ON "%s" ("a", "b")' % Person._table_
        elif dialect == "Oracle":
            index_sql = 'CREATE INDEX "IDX_AB" ON "%s" ("A", "B")' % Person._table_.upper()
        else:
            raise NotImplementedError
        self.assertIn(index_sql, script)

    def test_6(self):  # named unique()
        db = self.db

        class Person(db.Entity):
            a = Required(str)
            b = Required(int)
            unique(a, b, name="unq_ab")

        db.generate_mapping(create_tables=True)

        i1, i2 = Person._indexes_
        self.assertEqual(i2.name, "unq_ab")
        self.assertEqual(i2.is_unique, True)
        table = db.schema.tables[Person._table_]
        db_index = table.indexes[table.column_dict["a"], table.column_dict["b"]]
        self.assertEqual(db_index.name, "unq_ab")
        self.assertEqual(db_index.is_named, True)
        self.assertEqual(db_index.is_unique, True)

        script = db.schema.generate_create_script()
        dialect = db.provider.dialect
        if dialect == "MySQL" or dialect == "SQLite":
            constraint_sql = (
                "CONSTRAINT `unq_ab` UNIQUE (`a`, `b`)" if dialect == "MySQL"
                else 'CONSTRAINT "unq_ab" UNIQUE ("a", "b")'
            )
        elif dialect == "PostgreSQL":
            constraint_sql = 'CONSTRAINT "unq_ab" UNIQUE ("a", "b")'
        elif dialect == "Oracle":
            constraint_sql = 'CONSTRAINT "UNQ_AB" UNIQUE ("A", "B")'
        else:
            raise NotImplementedError
        self.assertIn(constraint_sql, script)

    def test_7(self):  # composite_key alias with name
        db = self.db

        class Person(db.Entity):
            a = Required(str)
            b = Required(int)
            composite_key(a, b, name="unq_ab")

        db.generate_mapping(create_tables=True)

        i1, i2 = Person._indexes_
        self.assertEqual(i2.name, "unq_ab")
        self.assertEqual(i2.is_unique, True)
        table = db.schema.tables[Person._table_]
        db_index = table.indexes[table.column_dict["a"], table.column_dict["b"]]
        self.assertEqual(db_index.name, "unq_ab")
        self.assertEqual(db_index.is_unique, True)

    def test_8(self):  # unique="name" for a single column
        db = self.db

        class User(db.Entity):
            email = Required(str, unique="unq_email")

        db.generate_mapping(create_tables=True)

        table = db.schema.tables[User._table_]
        column = table.column_dict["email"]
        db_index = table.indexes[column,]
        self.assertEqual(db_index.name, "unq_email")
        self.assertEqual(db_index.is_named, True)
        self.assertEqual(db_index.is_unique, True)

        script = db.schema.generate_create_script()
        dialect = db.provider.dialect
        if dialect == "MySQL":
            constraint_sql = "CONSTRAINT `unq_email` UNIQUE (`email`)"
        elif dialect == "SQLite" or dialect == "PostgreSQL":
            constraint_sql = 'CONSTRAINT "unq_email" UNIQUE ("email")'
        elif dialect == "Oracle":
            constraint_sql = 'CONSTRAINT "UNQ_EMAIL" UNIQUE ("EMAIL")'
        else:
            raise NotImplementedError
        self.assertIn(constraint_sql, script)

    def test_9(self):  # named composite PrimaryKey
        db = self.db

        class Person(db.Entity):
            a = Required(str)
            b = Required(int)
            PrimaryKey(a, b, name="pk_ab")

        db.generate_mapping(create_tables=True)

        i1 = Person._indexes_[0]
        self.assertEqual(i1.name, "pk_ab")
        self.assertEqual(i1.is_pk, True)
        table = db.schema.tables[Person._table_]
        self.assertEqual(table.pk_index.name, "pk_ab")

        script = db.schema.generate_create_script()
        dialect = db.provider.dialect
        if dialect == "MySQL":
            constraint_sql = "CONSTRAINT `pk_ab` PRIMARY KEY (`a`, `b`)"
        elif dialect == "SQLite" or dialect == "PostgreSQL":
            constraint_sql = 'CONSTRAINT "pk_ab" PRIMARY KEY ("a", "b")'
        elif dialect == "Oracle":
            constraint_sql = 'CONSTRAINT "PK_AB" PRIMARY KEY ("A", "B")'
        else:
            raise NotImplementedError
        self.assertIn(constraint_sql, script)

    def test_10(self):  # error cases
        db = self.db

        with self.assertRaises(TypeError):
            class Bad1(db.Entity):
                p = Required(str)
                PrimaryKey(p, name="pk_single")

        with self.assertRaises(TypeError):
            class Bad2(db.Entity):
                a = Required(str)
                b = Required(int)
                unique(a, b, name=123)

        with self.assertRaises(TypeError):
            class Bad3(db.Entity):
                a = Required(str, unique=123)

    def test_11(self):  # invalid index method is rejected at class definition
        db = self.db

        with self.assertRaises(TypeError):
            class Bad(db.Entity):
                a = Required(str)
                b = Required(int)
                composite_index(a, b, using="bogus")

        with self.assertRaises(TypeError):
            class Bad(db.Entity):
                a = Required(str, index=True, using="bogus")

    def test_12(self):  # nulls_not_distinct is unique-only
        db = self.db

        with self.assertRaises(TypeError):
            class Bad(db.Entity):
                a = Required(str)
                b = Required(int)
                composite_index(a, b, nulls_not_distinct=True)

    def test_13(self):  # RawSQL constraints
        db = self.db

        with self.assertRaises(TypeError):
            class Bad(db.Entity):
                a = Required(str)
                composite_index(raw_sql("lower(a)"))

        with self.assertRaises(TypeError):
            class Bad(db.Entity):
                a = Required(str)
                b = Required(int)
                composite_index(raw_sql("lower(a)"), raw_sql("lower(b)"), name="ix")

    def test_14(self):  # postgres-only options are rejected on other dialects
        db = self.db
        if db.provider.dialect == "PostgreSQL":
            self.skipTest("postgres-only index options are allowed on PostgreSQL")

        db1 = Database(**db_params)
        with self.assertRaises(TypeError):
            class Bad1(db1.Entity):
                a = Required(str)
                b = Required(int)
                composite_index(a, b, using="gin")
            db1.generate_mapping()
        teardown_database(db1)

        db2 = Database(**db_params)
        with self.assertRaises(TypeError):
            class Bad2(db2.Entity):
                a = Required(str)
                b = Required(int)
                composite_index(a, b, where="b > 0")
            db2.generate_mapping()
        teardown_database(db2)

        db3 = Database(**db_params)
        with self.assertRaises(TypeError):
            class Bad3(db3.Entity):
                a = Required(str)
                b = Required(int)
                unique(a, desc(b))
            db3.generate_mapping()
        teardown_database(db3)

    def test_15(self):  # attribute using/where require index or unique
        db = self.db

        with self.assertRaises(TypeError):
            class Bad(db.Entity):
                a = Required(str, using="hash")

        with self.assertRaises(TypeError):
            class Bad(db.Entity):
                a = Required(str, where="a > ''")

        with self.assertRaises(TypeError):
            class Bad(db.Entity):
                a = PrimaryKey(int, using="hash")

    def test_5(self):
        db = self.db

        class Table1(db.Entity):
            name = Required(str)
            table2s = Set("Table2")

        class Table2(db.Entity):
            height = Required(int)
            length = Required(int)
            table1 = Optional("Table1")
            composite_key(height, length, table1)

        db.generate_mapping(create_tables=True)

        with db_session:
            Table2(height=2, length=1)
            Table2.exists(height=2, length=1)


@only_for("postgres")
class TestIndexOptionsPostgreSQL(unittest.TestCase):
    def setUp(self):
        self.db = Database(**db_params)

    def tearDown(self):
        teardown_database(self.db)

    def test_using(self):
        db = self.db

        class Person(db.Entity):
            a = Required(str)
            b = Required(int)
            composite_index(a, b, name="ix_ab", using="btree")

        db.generate_mapping(create_tables=True)
        script = db.schema.generate_create_script()
        self.assertIn(
            'CREATE INDEX "ix_ab" ON "person" USING BTREE ("a", "b")', script
        )

    def test_using_gin(self):
        db = self.db

        class Person(db.Entity):
            tags = Required(IntArray, index="ix_tags", using="gin")

        db.generate_mapping(create_tables=True)
        script = db.schema.generate_create_script()
        self.assertIn(
            'CREATE INDEX "ix_tags" ON "person" USING GIN ("tags")', script
        )

    def test_where(self):
        db = self.db

        class Person(db.Entity):
            a = Required(str)
            b = Required(int)
            composite_index(a, b, name="ix_ab", where="b > 0")

        db.generate_mapping(create_tables=True)
        script = db.schema.generate_create_script()
        self.assertIn(
            'CREATE INDEX "ix_ab" ON "person" ("a", "b") WHERE b > 0', script
        )

    def test_partial_unique(self):
        db = self.db

        class Person(db.Entity):
            a = Required(str)
            b = Required(int)
            unique(a, b, name="unq_ab", where="a IS NOT NULL")

        db.generate_mapping(create_tables=True)
        script = db.schema.generate_create_script()
        self.assertIn(
            'CREATE UNIQUE INDEX "unq_ab" ON "person" ("a", "b") WHERE a IS NOT NULL',
            script,
        )
        self.assertNotIn('CONSTRAINT "unq_ab"', script)

    def test_desc(self):
        db = self.db

        class Person(db.Entity):
            a = Required(str)
            b = Required(int)
            composite_index(a, desc(b), name="ix_ab")

        db.generate_mapping(create_tables=True)
        script = db.schema.generate_create_script()
        self.assertIn(
            'CREATE INDEX "ix_ab" ON "person" ("a", "b" DESC)', script
        )

    def test_include(self):
        db = self.db

        class Person(db.Entity):
            a = Required(str)
            b = Required(int)
            c = Required(int)
            composite_index(a, b, name="ix_ab", include=(c,))

        db.generate_mapping(create_tables=True)
        script = db.schema.generate_create_script()
        self.assertIn(
            'CREATE INDEX "ix_ab" ON "person" ("a", "b") INCLUDE ("c")', script
        )

    def test_expression(self):
        db = self.db

        class Person(db.Entity):
            a = Required(str)
            composite_index(raw_sql("lower(a)"), name="ix_lower")

        db.generate_mapping(create_tables=True)
        script = db.schema.generate_create_script()
        self.assertIn('CREATE INDEX "ix_lower" ON "person" ((lower(a)))', script)

    def test_nulls_not_distinct(self):
        db = self.db

        class Person(db.Entity):
            a = Required(str)
            b = Required(int)
            unique(a, b, name="unq_ab", nulls_not_distinct=True)

        db.generate_mapping(create_tables=True)
        script = db.schema.generate_create_script()
        self.assertIn(
            'CONSTRAINT "unq_ab" UNIQUE NULLS NOT DISTINCT ("a", "b")', script
        )

    def test_attribute_level_options(self):
        db = self.db

        class Person(db.Entity):
            email = Required(str, unique=True, where="email IS NOT NULL")
            name = Required(str, index="ix_name", using="hash")

        db.generate_mapping(create_tables=True)
        script = db.schema.generate_create_script()
        self.assertIn(
            'CREATE UNIQUE INDEX "unq_person__email" ON "person" ("email") '
            "WHERE email IS NOT NULL",
            script,
        )
        self.assertIn(
            'CREATE INDEX "ix_name" ON "person" USING HASH ("name")', script
        )


if __name__ == "__main__":
    unittest.main()
