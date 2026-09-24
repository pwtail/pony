import os
import shutil
import sys
import tempfile
import types
import unittest

from pony.orm import Database, Required, Set, db_session
from pony.orm import migrations
from pony.orm.core import MappingError
from pony.orm.tests import db_params, only_for, teardown_database


class TestAppAPI(unittest.TestCase):
    def setUp(self):
        self.db = Database(**db_params)

    def tearDown(self):
        teardown_database(self.db)

    def test_app_accessible_via_db_attr(self):
        db = self.db
        myapp = db.app("myapp")
        self.assertIs(db.myapp, myapp)
        self.assertEqual(myapp.schema_name, "myapp")

    def test_schema_override_and_idempotent(self):
        db = self.db
        finances = db.app("accounts", schema="finances")
        self.assertEqual(finances.schema_name, "finances")
        self.assertIs(db.app("accounts"), finances)

    def test_name_collision_guard(self):
        db = self.db
        for bad in ("schema", "Entity", "bind", "my-app", "class"):
            with self.assertRaises((MappingError, TypeError)):
                db.app(bad)

    def test_entity_gets_schema_qualified_table(self):
        db = self.db
        myapp = db.app("myapp")

        class Account(myapp.Entity):
            name = Required(str)

        db.generate_mapping(check_tables=False, create_tables=False)
        default = db.provider.get_default_entity_table_name(Account)
        if db.provider.dialect == "PostgreSQL":
            self.assertEqual(Account._table_, (myapp.schema_name, default))
        else:
            # MySQL/SQLite: app — логическая группировка, имя таблицы обычное
            self.assertEqual(Account._table_, default)

    def test_entity_string_table_gets_qualified(self):
        db = self.db
        myapp = db.app("myapp")

        class Account(myapp.Entity):
            _table_ = "acc"
            name = Required(str)

        db.generate_mapping(check_tables=False, create_tables=False)
        if db.provider.dialect == "PostgreSQL":
            self.assertEqual(Account._table_, ("myapp", "acc"))
        else:
            self.assertEqual(Account._table_, "acc")

    @only_for("postgres")
    def test_entity_tuple_table_overrides_app(self):
        db = self.db
        myapp = db.app("myapp")

        class Account(myapp.Entity):
            _table_ = ("other", "acc")
            name = Required(str)

        db.generate_mapping(check_tables=False, create_tables=False)
        self.assertEqual(Account._table_, ("other", "acc"))

    def test_entity_without_app_keeps_plain_table(self):
        db = self.db

        class Person(db.Entity):
            name = Required(str)

        db.generate_mapping(check_tables=False, create_tables=False)
        default = db.provider.get_default_entity_table_name(Person)
        self.assertEqual(Person._table_, default)


@only_for("postgres")
class TestAppMigrations(unittest.TestCase):
    def setUp(self):
        self.db = Database(**db_params)
        self.dir = tempfile.mkdtemp()
        with db_session(ddl=True):
            rows = self.db.select(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = current_schema()"
            )
            for tablename in rows:
                self.db.execute('DROP TABLE IF EXISTS "%s" CASCADE' % tablename)
            for schema in ("accounts", "orders", "finances"):
                self.db.execute('DROP SCHEMA IF EXISTS %s CASCADE' % schema)

    def tearDown(self):
        teardown_database(self.db)
        shutil.rmtree(self.dir)

    def _make_db(self):
        db = self.db
        accounts = db.app("accounts")
        orders = db.app("orders")

        class Customer(accounts.Entity):
            name = Required(str)

        class Order(orders.Entity):
            customer = Required(Customer)
            items = Set("Item")

        class Item(orders.Entity):
            order = Required(Order)

        db.generate_mapping(check_tables=False, create_tables=False)
        return db, accounts, orders, Customer, Order, Item

    def _applied(self):
        with db_session:
            rows = self.db.select(
                "SELECT app, name FROM pony_migrations ORDER BY app, name"
            )
            return [(app, name) for app, name in rows]

    def test_add_per_app_initial_sql(self):
        db, accounts, orders, Customer, Order, Item = self._make_db()
        p1 = accounts.migrations.add(self.dir)
        p2 = orders.migrations.add(self.dir)
        self.assertTrue(p1.endswith(os.path.join("accounts", "0001_initial.sql")))
        self.assertTrue(p2.endswith(os.path.join("orders", "0001_initial.sql")))
        with open(p1) as f:
            accounts_sql = f.read()
        with open(p2) as f:
            orders_sql = f.read()
        self.assertNotIn("depends", accounts_sql)
        self.assertIn("-- depends: accounts/0001_initial.sql", orders_sql)
        self.assertIn('CREATE TABLE "accounts"."customer"', accounts_sql)
        self.assertNotIn('"orders"', accounts_sql)
        self.assertIn('REFERENCES "accounts"."customer"', orders_sql)

    def test_apply_aggregate_in_order(self):
        db, accounts, orders, Customer, Order, Item = self._make_db()
        accounts.migrations.add(self.dir)
        orders.migrations.add(self.dir)
        self.assertEqual(
            db.migrations.apply(self.dir),
            ["accounts/0001_initial.sql", "orders/0001_initial.sql"],
        )
        self.assertEqual(
            self._applied(),
            [("accounts", "0001_initial.sql"), ("orders", "0001_initial.sql")],
        )
        with db_session:
            schemas = db.select(
                "SELECT schemaname FROM pg_tables WHERE tablename = 'customer'"
            )
            self.assertEqual(schemas, ["accounts"])
        with db_session:
            c = Customer(name="a")
            Order(customer=c)
            self.assertEqual(Order.select().first().customer.name, "a")

    def test_per_app_apply(self):
        db, accounts, orders, Customer, Order, Item = self._make_db()
        accounts.migrations.add(self.dir)
        orders.migrations.add(self.dir)
        self.assertEqual(accounts.migrations.apply(self.dir), ["0001_initial.sql"])
        self.assertEqual(orders.migrations.apply(self.dir), ["0001_initial.sql"])
        self.assertEqual(
            self._applied(),
            [("accounts", "0001_initial.sql"), ("orders", "0001_initial.sql")],
        )

    def test_per_app_apply_requires_cross_app_dep(self):
        db, accounts, orders, Customer, Order, Item = self._make_db()
        accounts.migrations.add(self.dir)
        orders.migrations.add(self.dir)
        with self.assertRaises(migrations.MigrationError) as ctx:
            orders.migrations.apply(self.dir)
        self.assertIn("accounts/0001_initial.sql", str(ctx.exception))

    def test_plan_aggregate_and_per_app(self):
        db, accounts, orders, Customer, Order, Item = self._make_db()
        accounts.migrations.add(self.dir)
        orders.migrations.add(self.dir)
        infos = db.migrations.plan(self.dir)
        self.assertEqual(
            [(i.app, i.name) for i in infos],
            [("accounts", "0001_initial.sql"), ("orders", "0001_initial.sql")],
        )
        self.assertEqual(infos[1].dependencies, ("accounts/0001_initial.sql",))
        infos = orders.migrations.plan(self.dir)
        self.assertEqual([(i.app, i.name) for i in infos], [("orders", "0001_initial.sql")])

    def test_cli_positional_app(self):
        db, accounts, orders, Customer, Order, Item = self._make_db()
        module = types.ModuleType("pony_apps_test_mod")
        module.db = db
        sys.modules[module.__name__] = module
        try:
            self.assertEqual(
                migrations.main(
                    ["accounts", "add", "--db", module.__name__ + ":db", "--dir", self.dir]
                ),
                0,
            )
            self.assertEqual(
                migrations.main(
                    ["orders", "add", "--db", module.__name__ + ":db", "--dir", self.dir]
                ),
                0,
            )
            self.assertEqual(
                migrations.main(["apply", "--db", module.__name__ + ":db", "--dir", self.dir]),
                0,
            )
        finally:
            del sys.modules[module.__name__]
        self.assertEqual(
            self._applied(),
            [("accounts", "0001_initial.sql"), ("orders", "0001_initial.sql")],
        )


@only_for("sqlite")
class TestAppMigrationsSQLite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        dbfile = os.path.join(self.tmp, "test.db")
        self.db = Database("sqlite", dbfile, create_db=True)
        self.dir = os.path.join(self.tmp, "migrations")
        os.makedirs(self.dir)

    def tearDown(self):
        teardown_database(self.db)
        shutil.rmtree(self.tmp)

    def _make_db(self):
        db = self.db
        accounts = db.app("accounts")
        orders = db.app("orders")

        class Customer(accounts.Entity):
            name = Required(str)

        class Order(orders.Entity):
            customer = Required(Customer)

        db.generate_mapping(check_tables=False, create_tables=False)
        return db, accounts, orders, Customer, Order

    def _applied(self):
        with db_session:
            rows = self.db.select(
                "SELECT app, name FROM pony_migrations ORDER BY app, name"
            )
            return [(app, name) for app, name in rows]

    def test_add_per_app_initial_sql(self):
        db, accounts, orders, Customer, Order = self._make_db()
        p1 = accounts.migrations.add(self.dir)
        p2 = orders.migrations.add(self.dir)
        self.assertTrue(p1.endswith(os.path.join("accounts", "0001_initial.sql")))
        self.assertTrue(p2.endswith(os.path.join("orders", "0001_initial.sql")))
        with open(p1) as f:
            accounts_sql = f.read()
        with open(p2) as f:
            orders_sql = f.read()
        self.assertNotIn("depends", accounts_sql)
        self.assertIn("-- depends: accounts/0001_initial.sql", orders_sql)
        self.assertIn('CREATE TABLE "Customer"', accounts_sql)
        self.assertNotIn('"Order"', accounts_sql)
        self.assertIn('REFERENCES "Customer"', orders_sql)

    def test_apply_aggregate_in_order(self):
        db, accounts, orders, Customer, Order = self._make_db()
        accounts.migrations.add(self.dir)
        orders.migrations.add(self.dir)
        self.assertEqual(
            db.migrations.apply(self.dir),
            ["accounts/0001_initial.sql", "orders/0001_initial.sql"],
        )
        self.assertEqual(
            self._applied(),
            [("accounts", "0001_initial.sql"), ("orders", "0001_initial.sql")],
        )
        with db_session:
            c = Customer(name="a")
            Order(customer=c)
            self.assertEqual(Order.select().first().customer.name, "a")

    def test_per_app_apply_requires_cross_app_dep(self):
        db, accounts, orders, Customer, Order = self._make_db()
        accounts.migrations.add(self.dir)
        orders.migrations.add(self.dir)
        with self.assertRaises(migrations.MigrationError) as ctx:
            orders.migrations.apply(self.dir)
        self.assertIn("accounts/0001_initial.sql", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
