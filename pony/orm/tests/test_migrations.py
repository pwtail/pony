import io
import os
import shutil
import sys
import tempfile
import types
import unittest

from pony.orm import (
    Database,
    ERDiagramError,
    IntegrityError,
    ProgrammingError,
    Required,
    db_session,
)

from pony.orm import migrations
from pony.orm.tests import db_params, only_for, teardown_database


@only_for("postgres")
class TestMigrations(unittest.TestCase):
    def setUp(self):
        self.db = Database(**db_params)
        self.app = self.db.application("main", schema="public")
        self.dir = tempfile.mkdtemp()
        # миграции создают таблицы вне mapping (в т.ч. pony_migrations),
        # поэтому чистка — по каталогу, а не только по db.schema
        with db_session(ddl=True):
            rows = self.db.select(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = current_schema()"
            )
            for tablename in rows:
                self.db.execute('DROP TABLE IF EXISTS "%s" CASCADE' % tablename)

    def tearDown(self):
        teardown_database(self.db)
        shutil.rmtree(self.dir)

    def _write(self, name, content):
        app_dir = os.path.join(self.dir, "main")
        os.makedirs(app_dir, exist_ok=True)
        path = os.path.join(app_dir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def _applied_names(self):
        with db_session:
            return [
                name for name, _ in self.db.select(
                    "SELECT name, sha256 FROM pony_migrations ORDER BY name"
                )
            ]

    def _table_count(self, table_name):
        with db_session:
            rows = self.db.select(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_name = '%s'" % table_name
            )
            return rows[0]

    def test_add_generates_initial_sql(self):
        db = self.db

        class Person(self.app.Entity):
            name = Required(str)

        path = migrations.make_migration(db, self.dir, app="main")
        self.assertEqual(os.path.basename(path), "0001_initial.sql")
        with open(path) as f:
            content = f.read()
        self.assertIn('CREATE TABLE "public"."person"', content)
        self.assertEqual(content.rstrip(), db.schema.generate_create_script())
        with self.assertRaises(migrations.MigrationError):
            migrations.make_migration(db, self.dir, app="main")

    def test_migrate_sql_and_py_in_order(self):
        db = self.db
        self._write("0001_create.sql", 'CREATE TABLE "t1" (id integer PRIMARY KEY)')
        self._write(
            "0002_seed.py",
            "# depends: 0001_create.sql\n"
            "from pony.orm import Database\n\ndb = Database.instance().new()\n\n"
            "db.execute('INSERT INTO t1 (id) VALUES (1)')\n",
        )
        self.assertEqual(
            migrations.apply_migrations(db, self.dir),
            ["main/0001_create.sql", "main/0002_seed.py"],
        )
        with db_session:
            self.assertEqual(list(db.select("SELECT id FROM t1")), [1])
        self.assertEqual(
            self._applied_names(), ["0001_create.sql", "0002_seed.py"]
        )
        # повторный запуск — no-op
        self.assertEqual(migrations.apply_migrations(db, self.dir), [])

    def test_py_migration_uses_orm(self):
        db = self.db

        class Person(self.app.Entity):
            name = Required(str)

        migrations.make_migration(db, self.dir, app="main")  # 0001_initial.sql
        self._write(
            "0002_seed.py",
            "# depends: 0001_initial.sql\n"
            "from pony.orm import Database\n\ndb = Database.instance().new()\n\n"
            "class Person(db.Entity):\n"
            "    pass\n\n"
            "if __name__ == '__main__':\n"
            "    db.introspect()\n"
            "    db.Person(name='migrated')\n",
        )
        migrations.apply_migrations(db, self.dir)
        with db_session:
            self.assertEqual(
                [p.name for p in db.Person.select()], ["migrated"]
            )

    def test_py_migration_lazy_introspection_on_with_db(self):
        db = self.db

        class Person(self.app.Entity):
            name = Required(str)

        migrations.make_migration(db, self.dir, app="main")  # 0001_initial.sql
        self._write(
            "0002_seed.py",
            "# depends: 0001_initial.sql\n"
            "from pony.orm import Database\n\ndb = Database.instance().new()\n\n"
            "class Person(db.Entity):\n"
            "    pass\n\n"
            "if __name__ == '__main__':\n"
            "    with db:\n"
            "        assert db.schema is not None, 'with db: introspects'\n"
            "        db.Person(name='lazy-with-db')\n",
        )
        migrations.apply_migrations(db, self.dir)
        with db_session:
            self.assertEqual(
                [p.name for p in db.Person.select()], ["lazy-with-db"]
            )

    def test_py_migration_without_own_database_is_an_error(self):
        db = self.db
        self._write(
            "0001_bad.py",
            "from pony.orm import Database\n\n"
            "db = Database.instance()\n",
        )
        with self.assertRaises(migrations.MigrationError):
            migrations.apply_migrations(db, self.dir)
        self.assertEqual(self._applied_names(), [])

    def test_py_migration_without_with_db_does_not_introspect(self):
        db = self.db

        class Person(self.app.Entity):
            name = Required(str)

        migrations.make_migration(db, self.dir, app="main")  # 0001_initial.sql
        self._write(
            "0002_seed.py",
            "# depends: 0001_initial.sql\n"
            "from pony.orm import Database\n\ndb = Database.instance().new()\n\n"
            "class Person(db.Entity):\n"
            "    pass\n\n"
            "if __name__ == '__main__':\n"
            "    db.execute(\"INSERT INTO person (name) VALUES ('no-lazy')\")\n"
            "    assert db.schema is None\n",
        )
        migrations.apply_migrations(db, self.dir)
        with db_session:
            self.assertEqual(
                [p.name for p in db.Person.select()], ["no-lazy"]
            )

    def test_py_migration_entity_use_without_with_db_is_an_error(self):
        db = self.db

        class Person(self.app.Entity):
            name = Required(str)

        migrations.make_migration(db, self.dir, app="main")  # 0001_initial.sql
        self._write(
            "0002_seed.py",
            "# depends: 0001_initial.sql\n"
            "from pony.orm import Database\n\ndb = Database.instance().new()\n\n"
            "class Person(db.Entity):\n"
            "    pass\n\n"
            "if __name__ == '__main__':\n"
            "    db.Person(name='no-lazy')\n",
        )
        with self.assertRaises(ERDiagramError):
            migrations.apply_migrations(db, self.dir)
        self.assertEqual(self._applied_names(), ["0001_initial.sql"])

    def test_py_migration_is_not_ddl(self):
        db = self.db
        self._write(
            "0001_check.py",
            "from pony.orm import Database\n\ndb = Database.instance().new()\n\n"
            "from pony.orm import core\n\n"
            "if __name__ == '__main__':\n"
            "    assert core.local.db_session.ddl is False\n",
        )
        migrations.apply_migrations(db, self.dir)
        self.assertEqual(self._applied_names(), ["0001_check.py"])

    def test_py_migration_without_entities_skips_introspection(self):
        db = self.db
        self._write(
            "0001_seed.py",
            "from pony.orm import Database\n\ndb = Database.instance().new()\n\n"
            "if __name__ == '__main__':\n"
            "    with db:\n"
            "        assert db.schema is None\n"
            "        db.execute('CREATE TABLE lazy_raw (v text)')\n"
            "        db.execute(\"INSERT INTO lazy_raw VALUES ('ok')\")\n"
            "    assert db.schema is None\n",
        )
        migrations.apply_migrations(db, self.dir)
        with db_session:
            self.assertEqual(list(db.select("SELECT v FROM lazy_raw")), ["ok"])

    def test_py_migration_runs_as_main_script(self):
        db = self.db
        self._write(
            "0001_name.py",
            "from pony.orm import Database\n\ndb = Database.instance().new()\n\n"
            "assert __name__ == '__main__', __name__\n"
            "db.execute('CREATE TABLE nm (v text)')\n"
            "db.execute(\"INSERT INTO nm VALUES ('%s')\" % __name__)\n",
        )
        migrations.apply_migrations(db, self.dir)
        with db_session:
            self.assertEqual(list(db.select("SELECT v FROM nm")), ["__main__"])

    def test_database_instance_inside_migration(self):
        db = self.db
        self._write(
            "0001_x.py",
            "from pony.orm import Database\n\ndb = Database.instance().new()\n\n"
            "def helper():\n"
            "    return db\n\n"
            "db.execute('CREATE TABLE iv (v text)')\n"
            "db.execute(\"INSERT INTO iv VALUES ('%s')\" % (helper() is db))\n",
        )
        migrations.apply_migrations(db, self.dir)
        self.assertIs(Database.instance(), db)
        with db_session:
            self.assertEqual(list(db.select("SELECT v FROM iv")), ["True"])

    def test_add_named_data_migration(self):
        db = self.db
        path = migrations.make_named_migration(db, self.dir, "some_name.py", app="main")
        self.assertEqual(os.path.basename(path), "0001_some_name.py")
        with open(path) as f:
            content = f.read()
        self.assertIn("from pony.orm import *", content)
        self.assertIn("db = Database.instance().new()", content)
        self.assertIn("if __name__ == '__main__':", content)
        self.assertIn("with db_session:", content)
        self.assertNotIn("depends", content)
        path2 = migrations.make_named_migration(db, self.dir, "second.py", app="main")
        self.assertEqual(os.path.basename(path2), "0002_second.py")
        with open(path2) as f:
            self.assertIn("# depends: 0001_some_name.py", f.read())
        self.assertEqual(
            migrations.apply_migrations(db, self.dir),
            ["main/0001_some_name.py", "main/0002_second.py"],
        )

    def test_add_named_sql_migration(self):
        db = self.db
        path = migrations.make_named_migration(db, self.dir, "add_orders.sql", app="main")
        self.assertEqual(os.path.basename(path), "0001_add_orders.sql")
        with open(path) as f:
            self.assertNotIn("depends", f.read())
        # заготовка (только комментарий) применяется без ошибки
        self.assertEqual(
            migrations.apply_migrations(db, self.dir), ["main/0001_add_orders.sql"]
        )
        self.assertEqual(self._applied_names(), ["0001_add_orders.sql"])

    def test_migration_gets_fresh_database(self):
        db = self.db

        class AppModel(self.app.Entity):
            name = Required(str)

        self._write(
            "0001_x.py",
            "from pony.orm import Database\n\ndb = Database.instance().new()\n\n"
            "assert 'AppModel' not in db.entities\n"
            "assert db.schema is None\n"
            "assert db.provider is not None\n",
        )
        migrations.apply_migrations(db, self.dir)
        self.assertEqual(self._applied_names(), ["0001_x.py"])

    def test_modified_after_apply_is_an_error(self):
        db = self.db
        self._write("0001_a.sql", 'CREATE TABLE "ta" (id integer)')
        migrations.apply_migrations(db, self.dir)
        self._write("0001_a.sql", 'CREATE TABLE "ta" (id integer, x integer)')
        with self.assertRaises(migrations.MigrationError):
            migrations.apply_migrations(db, self.dir)

    def test_failed_sql_rolls_back(self):
        db = self.db
        self._write(
            "0001_bad.sql",
            'CREATE TABLE "ok_t" (id integer);\n'
            "INSERT INTO nonexistent_table VALUES (1);\n",
        )
        with self.assertRaises(ProgrammingError):
            migrations.apply_migrations(db, self.dir)
        self.assertEqual(self._table_count("ok_t"), 0)
        self.assertEqual(self._applied_names(), [])

    def test_failed_py_rolls_back_its_transaction(self):
        db = self.db
        self._write("0001_ok.sql", 'CREATE TABLE "px" (id integer)')
        self._write(
            "0002_bad.py",
            "# depends: 0001_ok.sql\nraise RuntimeError('boom')\n",
        )
        with self.assertRaises(RuntimeError):
            migrations.apply_migrations(db, self.dir)
        # 0001 — в своей транзакции, применена; 0002 — откатилась
        self.assertEqual(self._applied_names(), ["0001_ok.sql"])

    def test_fake(self):
        db = self.db
        self._write("0001_a.sql", 'CREATE TABLE "fa" (id integer)')
        migrations.apply_migrations(db, self.dir, fake=True)
        self.assertEqual(self._applied_names(), ["0001_a.sql"])
        self.assertEqual(self._table_count("fa"), 0)

    def test_plan_does_not_apply(self):
        db = self.db
        self._write("0001_a.sql", 'CREATE TABLE "da" (id integer)')
        infos = migrations.plan_migrations(db, self.dir)
        self.assertEqual([info.name for info in infos], ["0001_a.sql"])
        self.assertFalse(infos[0].applied)
        self.assertEqual(self._table_count("da"), 0)
        self.assertEqual(self._table_count("pony_migrations"), 0)

    def test_cli_migrate(self):
        db = self.db
        module = types.ModuleType("pony_migrations_test_mod")
        module.db = db
        sys.modules[module.__name__] = module
        try:
            self._write("0001_a.sql", 'CREATE TABLE "ca" (id integer)')
            code = migrations.main(
                ["migrations", "apply", "--db", module.__name__ + ":db", "--dir", self.dir]
            )
            self.assertEqual(code, 0)
        finally:
            del sys.modules[module.__name__]
        self.assertEqual(self._applied_names(), ["0001_a.sql"])

    def test_db_migrations_api(self):
        db = self.db

        class Person(self.app.Entity):
            name = Required(str)

        paths = db.migrations.make(self.dir)
        self.assertEqual(
            [os.path.basename(path) for path in paths], ["0001_initial.sql"]
        )
        self.assertEqual(
            db.migrations.apply(self.dir), ["main/0001_initial.sql"]
        )
        self.assertEqual(self._applied_names(), ["0001_initial.sql"])
        with db_session:
            tables = db.select(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = current_schema()"
            )
            self.assertEqual(
                sorted(tables), ["person", "pony_migrations"]
            )

    def test_cli_make_and_dir(self):
        db = self.db
        module = types.ModuleType("pony_migrations_test_mod")
        module.db = db
        sys.modules[module.__name__] = module
        try:
            class Person(self.app.Entity):
                name = Required(str)

            code = migrations.main(
                [
                    "migrations",
                    "make",
                    "--db",
                    module.__name__ + ":db",
                    "--dir",
                    self.dir,
                ]
            )
            self.assertEqual(code, 0)
            self.assertTrue(
                os.path.exists(os.path.join(self.dir, "main", "0001_initial.sql"))
            )
            code = migrations.main(
                [
                    "migrations",
                    "apply",
                    "--db",
                    module.__name__ + ":db",
                    "--dir",
                    self.dir,
                ]
            )
            self.assertEqual(code, 0)
        finally:
            del sys.modules[module.__name__]
        self.assertEqual(self._applied_names(), ["0001_initial.sql"])


class TestMigrationsDialectGuard(unittest.TestCase):
    def test_unsupported_dialect_rejected(self):
        class FakeProvider:
            dialect = "Oracle"

        db = Database()
        db.provider = FakeProvider()
        with self.assertRaises(migrations.MigrationError):
            migrations._check_supported(db)


@only_for("sqlite")
class TestMigrationsSQLite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        dbfile = os.path.join(self.tmp, "test.db")
        self.db = Database("sqlite", dbfile, create_db=True)
        self.app = self.db.application("main", schema="public")
        self.dir = os.path.join(self.tmp, "migrations")
        os.makedirs(self.dir)

    def tearDown(self):
        teardown_database(self.db)
        shutil.rmtree(self.tmp)

    def _write(self, name, content):
        app_dir = os.path.join(self.dir, "main")
        os.makedirs(app_dir, exist_ok=True)
        path = os.path.join(app_dir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def _applied_names(self):
        with db_session:
            return [
                name
                for name, _ in self.db.select(
                    "SELECT name, sha256 FROM pony_migrations ORDER BY name"
                )
            ]

    def _table_count(self, table_name):
        with db_session:
            rows = self.db.select(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type='table' AND name=$t",
                {"t": table_name},
            )
            return rows[0]

    def test_add_generates_initial_sql(self):
        db = self.db

        class Person(self.app.Entity):
            name = Required(str)

        path = migrations.make_migration(db, self.dir, app="main")
        self.assertEqual(os.path.basename(path), "0001_initial.sql")
        with open(path) as f:
            content = f.read()
        self.assertIn('CREATE TABLE "Person"', content)
        with self.assertRaises(migrations.MigrationError):
            migrations.make_migration(db, self.dir, app="main")

    def test_migrate_sql_and_py_in_order(self):
        db = self.db
        self._write("0001_create.sql", 'CREATE TABLE "t1" (id integer PRIMARY KEY)')
        self._write(
            "0002_seed.py",
            "# depends: 0001_create.sql\n"
            "from pony.orm import Database\n\ndb = Database.instance().new()\n\n"
            "db.execute('INSERT INTO t1 (id) VALUES (1)')\n",
        )
        self.assertEqual(
            migrations.apply_migrations(db, self.dir),
            ["main/0001_create.sql", "main/0002_seed.py"],
        )
        with db_session:
            self.assertEqual(list(db.select("SELECT id FROM t1")), [1])
        self.assertEqual(
            self._applied_names(), ["0001_create.sql", "0002_seed.py"]
        )
        self.assertEqual(migrations.apply_migrations(db, self.dir), [])

    def test_py_migration_uses_introspection(self):
        db = self.db

        class Person(self.app.Entity):
            name = Required(str)

        migrations.make_migration(db, self.dir, app="main")  # 0001_initial.sql
        self._write(
            "0002_seed.py",
            "# depends: 0001_initial.sql\n"
            "from pony.orm import Database\n\ndb = Database.instance().new()\n\n"
            "class Person(db.Entity):\n"
            "    pass\n\n"
            "db.introspect()\n"
            "db.Person(name='migrated')\n",
        )
        migrations.apply_migrations(db, self.dir)
        with db_session:
            self.assertEqual(
                [p.name for p in db.Person.select()], ["migrated"]
            )

    def test_py_migration_lazy_introspection_on_with_db(self):
        db = self.db

        class Person(self.app.Entity):
            name = Required(str)

        migrations.make_migration(db, self.dir, app="main")  # 0001_initial.sql
        self._write(
            "0002_seed.py",
            "# depends: 0001_initial.sql\n"
            "from pony.orm import Database\n\ndb = Database.instance().new()\n\n"
            "class Person(db.Entity):\n"
            "    pass\n\n"
            "if __name__ == '__main__':\n"
            "    with db:\n"
            "        assert db.schema is not None, 'with db: introspects'\n"
            "        db.Person(name='lazy-with-db')\n",
        )
        migrations.apply_migrations(db, self.dir)
        with db_session:
            self.assertEqual(
                [p.name for p in db.Person.select()], ["lazy-with-db"]
            )

    def test_py_migration_has_fk_checks(self):
        # дата-миграция идёт в обычной (не ddl) сессии, FK-проверки включены
        db = self.db
        self._write(
            "0001_fk.sql",
            "CREATE TABLE parent (id integer PRIMARY KEY);\n"
            "CREATE TABLE child ("
            "id integer PRIMARY KEY, "
            "parent_id integer REFERENCES parent(id));",
        )
        self._write(
            "0002_bad.py",
            "# depends: 0001_fk.sql\n"
            "from pony.orm import Database\n\ndb = Database.instance().new()\n\n"
            "if __name__ == '__main__':\n"
            "    db.execute(\n"
            "        'INSERT INTO child (id, parent_id) VALUES (1, 999)'\n"
            "    )\n",
        )
        with self.assertRaises(IntegrityError):
            migrations.apply_migrations(db, self.dir)
        self.assertEqual(self._applied_names(), ["0001_fk.sql"])
        with db_session:
            self.assertEqual(list(db.select("SELECT id FROM child")), [])

    def test_modified_after_apply_is_an_error(self):
        db = self.db
        self._write("0001_a.sql", 'CREATE TABLE "ta" (id integer)')
        migrations.apply_migrations(db, self.dir)
        self._write("0001_a.sql", 'CREATE TABLE "ta" (id integer, x integer)')
        with self.assertRaises(migrations.MigrationError):
            migrations.apply_migrations(db, self.dir)

    def test_fake(self):
        db = self.db
        self._write("0001_a.sql", 'CREATE TABLE "fa" (id integer)')
        migrations.apply_migrations(db, self.dir, fake=True)
        self.assertEqual(self._applied_names(), ["0001_a.sql"])
        self.assertEqual(self._table_count("fa"), 0)

    def test_plan_does_not_apply(self):
        db = self.db
        self._write("0001_a.sql", 'CREATE TABLE "da" (id integer)')
        infos = migrations.plan_migrations(db, self.dir)
        self.assertEqual([info.name for info in infos], ["0001_a.sql"])
        self.assertFalse(infos[0].applied)
        self.assertEqual(self._table_count("da"), 0)
        self.assertEqual(self._table_count("pony_migrations"), 0)

    def test_db_migrations_api(self):
        db = self.db

        class Person(self.app.Entity):
            name = Required(str)

        paths = db.migrations.make(self.dir)
        self.assertEqual(
            [os.path.basename(path) for path in paths], ["0001_initial.sql"]
        )
        self.assertEqual(
            db.migrations.apply(self.dir), ["main/0001_initial.sql"]
        )
        self.assertEqual(self._applied_names(), ["0001_initial.sql"])
        with db_session:
            tables = set(
                db.select(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            )
            self.assertTrue({"Person", "pony_migrations"} <= tables)

    def test_apply_fails_on_deleted_applied_migration(self):
        db = self.db
        path = self._write("0001_a.sql", 'CREATE TABLE "t1" (id integer)')
        migrations.apply_migrations(db, self.dir)
        os.remove(path)
        with self.assertRaises(migrations.MigrationError) as cm:
            migrations.apply_migrations(db, self.dir)
        self.assertIn("missing from the directory", str(cm.exception))

    def test_inner_db_session_does_not_commit_early(self):
        # сессию владеет раннер: вложенная with db_session: не коммитит сама,
        # падение в конце миграции откатывает и данные, и запись в pony_migrations
        db = self.db
        self._write(
            "0001_x.sql", "CREATE TABLE t (id integer PRIMARY KEY, v text)"
        )
        self._write(
            "0002_bad.py",
            "# depends: 0001_x.sql\n"
            "from pony.orm import Database, db_session\n\n"
            "db = Database.instance().new()\n\n"
            "if __name__ == '__main__':\n"
            "    with db_session:\n"
            "        db.execute(\"INSERT INTO t VALUES (1, 'x')\")\n"
            "    raise RuntimeError('boom')\n",
        )
        with self.assertRaises(RuntimeError):
            migrations.apply_migrations(db, self.dir)
        with db_session:
            self.assertEqual(list(db.select("SELECT id FROM t")), [])
        self.assertEqual(self._applied_names(), ["0001_x.sql"])

    def test_py_migration_imports_sibling_module(self):
        db = self.db
        # соседние модули — подпакетом: свободный .py в каталоге миграций
        # сам был бы миграцией
        helper_dir = os.path.join(self.dir, "main", "mighelpers")
        os.makedirs(helper_dir)
        with open(os.path.join(helper_dir, "__init__.py"), "w") as f:
            f.write("VALUE = 'from-helper'\n")
        self._write(
            "0001_x.py",
            "from pony.orm import Database\n\n"
            "db = Database.instance().new()\n\n"
            "import mighelpers\n"
            "db.execute('CREATE TABLE sh (v text)')\n"
            "db.execute(\"INSERT INTO sh VALUES ('%s')\" % mighelpers.VALUE)\n",
        )
        migrations.apply_migrations(db, self.dir)
        with db_session:
            self.assertEqual(list(db.select("SELECT v FROM sh")), ["from-helper"])

    def test_cli_accepts_db_before_subcommand(self):
        db = self.db
        module = types.ModuleType("pony_fake_app_cli")
        module.db = db
        sys.modules[module.__name__] = module
        try:
            self._write("0001_a.sql", 'CREATE TABLE "t1" (id integer)')
            rc = migrations.main(
                ["--db", "%s:db" % module.__name__, "--dir", self.dir, "plan"]
            )
            self.assertEqual(rc, 0)
        finally:
            del sys.modules[module.__name__]

    def test_sql_with_literals_and_trigger(self):
        # ';' и '--' внутри строковых литералов, CREATE TRIGGER ... BEGIN..END
        db = self.db
        self._write(
            "0001_a.sql",
            "-- комментарий с ; разделителем\n"
            "CREATE TABLE t1 (id integer PRIMARY KEY, v text);\n"
            "INSERT INTO t1 VALUES (1, 'a--b');\n"
            "INSERT INTO t1 VALUES (2, 'x;y');\n"
            'INSERT INTO t1 VALUES (3, "dq;z");\n'
            "CREATE TRIGGER trg AFTER INSERT ON t1 BEGIN\n"
            "    UPDATE t1 SET v = v || ';' WHERE id = new.id;\n"
            "END;\n"
            "INSERT INTO t1 VALUES (4, 'trigger;test');\n",
        )
        self.assertEqual(
            migrations.apply_migrations(db, self.dir), ["main/0001_a.sql"]
        )
        with db_session:
            rows = list(db.select("SELECT id, v FROM t1 ORDER BY id"))
        self.assertEqual(
            rows,
            [(1, "a--b"), (2, "x;y"), (3, "dq;z"), (4, "trigger;test;")],
        )


class TestSqlSplitter(unittest.TestCase):
    def test_mysql_backslash_escapes(self):
        sql = "INSERT INTO t VALUES ('a\\';b');\nINSERT INTO t VALUES ('c');\n"
        statements = migrations._split_sql_statements(sql, "MySQL")
        self.assertEqual(len(statements), 2)
        self.assertIn("'a\\';b'", statements[0])

    def test_comment_only_statements_are_skipped(self):
        sql = (
            "-- depends: nothing\n"
            "/* block ; comment */\n"
            "CREATE TABLE a (id int);\n"
            "-- just a comment ;\n"
            "CREATE TABLE b (id int);\n"
        )
        statements = migrations._split_sql_statements(sql, "MySQL")
        # лидирующие комментарии прилипают к оператору (безвредны),
        # кусок из одних комментариев оператором не становится
        self.assertEqual(len(statements), 2)
        self.assertTrue(statements[0].endswith("CREATE TABLE a (id int)"))
        self.assertTrue(statements[1].endswith("CREATE TABLE b (id int)"))
        self.assertIn("-- just a comment", statements[1])

    def test_mysql_backticks_and_hash_comments(self):
        sql = "CREATE TABLE `a;b` (id int); # comment ;\nINSERT INTO `a;b` VALUES (1);"
        statements = migrations._split_sql_statements(sql, "MySQL")
        self.assertEqual(len(statements), 2)
        self.assertTrue(statements[0].startswith("CREATE TABLE `a;b`"))


class TestReadDependencies(unittest.TestCase):
    def _deps(self, name, content):
        path = os.path.join(self.dir, name)
        with open(path, "w") as f:
            f.write(content)
        return migrations._read_dependencies(path, name)

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_py_docstring_before_depends(self):
        self.assertEqual(
            self._deps(
                "0002_x.py",
                '"""Migration docstring."""\n# depends: 0001_a.sql\npass\n',
            ),
            ["0001_a.sql"],
        )

    def test_py_multiline_docstring_before_depends(self):
        self.assertEqual(
            self._deps(
                "0002_x.py",
                '"""\nlong\ndocstring\n"""\n\n# depends: 0001_a.sql\npass\n',
            ),
            ["0001_a.sql"],
        )

    def test_py_depends_after_code_is_an_error(self):
        with self.assertRaises(migrations.MigrationError):
            self._deps(
                "0002_x.py", "x = 1\n# depends: 0001_a.sql\n"
            )

    def test_sql_block_comment_before_depends(self):
        self.assertEqual(
            self._deps(
                "0002_x.sql",
                "/* header ; comment */\n-- depends: 0001_a.sql\n"
                "CREATE TABLE t (id int);\n",
            ),
            ["0001_a.sql"],
        )

    def test_sql_depends_after_statements_is_an_error(self):
        with self.assertRaises(migrations.MigrationError):
            self._deps(
                "0002_x.sql",
                "CREATE TABLE t (id int);\n-- depends: 0001_a.sql\n",
            )


class TestNoApplications(unittest.TestCase):
    def test_aggregate_commands_without_apps_are_an_error(self):
        db = Database("sqlite", ":memory:")
        try:
            for call in (
                lambda: migrations.make_migration(db, "/tmp/unused-migs"),
                lambda: migrations.make_named_migration(
                    db, "/tmp/unused-migs", "x.py"
                ),
                lambda: migrations.apply_migrations(db, "/tmp/unused-migs"),
                lambda: migrations.plan_migrations(db, "/tmp/unused-migs"),
                lambda: migrations.merge_migration(db, "/tmp/unused-migs"),
            ):
                with self.assertRaises(migrations.MigrationError) as cm:
                    call()
                self.assertIn("No applications are registered", str(cm.exception))
        finally:
            db.disconnect()

    def test_py_migration_against_memory_sqlite_is_an_error(self):
        db = Database("sqlite", ":memory:")
        db.application("main", schema="public")
        import tempfile

        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, "main"))
        with open(os.path.join(tmp, "main", "0001_x.py"), "w") as f:
            f.write(
                "from pony.orm import Database\n\n"
                "db = Database.instance().new()\n\n"
                "db.execute('CREATE TABLE t (id integer)')\n"
            )
        try:
            with self.assertRaises(migrations.MigrationError) as cm:
                migrations.apply_migrations(db, tmp)
            self.assertIn("in-memory", str(cm.exception))
        finally:
            db.disconnect()

    def test_database_instance_not_clobbered_by_new(self):
        db = Database("sqlite", ":memory:")
        try:
            Database._instance = db
            clone = db.new()
            try:
                self.assertIs(Database.instance(), db)
            finally:
                clone.disconnect()
        finally:
            db.disconnect()
            Database._instance = None

def _mariadb_available():
    try:
        import mariadb

        conn = mariadb.connect(
            user=os.environ.get("PONY_MARIADB_USER", "ponytest"),
            password=os.environ.get("PONY_MARIADB_PASSWORD", "ponytest"),
            host=os.environ.get("PONY_MARIADB_HOST", "127.0.0.1"),
            port=int(os.environ.get("PONY_MARIADB_PORT", "3306")),
            database=os.environ.get("PONY_MARIADB_DB", "pony_mariadb_test"),
        )
    except Exception:
        return False
    else:
        conn.close()
        return True


@unittest.skipUnless(_mariadb_available(), "MariaDB is not available")
class TestMigrationsMariaDB(unittest.TestCase):
    def setUp(self):
        import mariadb

        self.mdb = dict(
            user=os.environ.get("PONY_MARIADB_USER", "ponytest"),
            password=os.environ.get("PONY_MARIADB_PASSWORD", "ponytest"),
            host=os.environ.get("PONY_MARIADB_HOST", "127.0.0.1"),
            port=int(os.environ.get("PONY_MARIADB_PORT", "3306")),
            database=os.environ.get("PONY_MARIADB_DB", "pony_mariadb_test"),
        )
        self.db = Database("mariadb", **self.mdb)
        self.app = self.db.application("main", schema="public")
        self.dir = tempfile.mkdtemp()
        with db_session(ddl=True):
            rows = self.db.select(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE()"
            )
            for table_name in rows:
                self.db.execute("DROP TABLE IF EXISTS `%s`" % table_name)

    def tearDown(self):
        teardown_database(self.db)
        shutil.rmtree(self.dir)

    def test_add_and_apply(self):
        db = self.db

        class Person(self.app.Entity):
            name = Required(str)

        path = migrations.make_migration(db, self.dir, app="main")
        self.assertEqual(os.path.basename(path), "0001_initial.sql")
        with open(path) as f:
            self.assertIn("CREATE TABLE `person`", f.read())
        self.assertEqual(
            migrations.apply_migrations(db, self.dir), ["main/0001_initial.sql"]
        )

    def test_introspection_fill_and_dump(self):
        db = self.db
        with db_session(ddl=True):
            db.execute(
                "CREATE TABLE person (person_id int AUTO_INCREMENT PRIMARY KEY, "
                "name varchar(50) NOT NULL)"
            )
            db.execute(
                "CREATE TABLE tag (tag_id int AUTO_INCREMENT PRIMARY KEY, "
                "label varchar(50))"
            )
            db.execute(
                "CREATE TABLE person_tag (person_id int NOT NULL, tag_id int NOT NULL, "
                "PRIMARY KEY (person_id, tag_id), "
                "CONSTRAINT fk_pt_person FOREIGN KEY (person_id) REFERENCES person(person_id), "
                "CONSTRAINT fk_pt_tag FOREIGN KEY (tag_id) REFERENCES tag(tag_id))"
            )
            db.execute(
                "CREATE TABLE car (id int AUTO_INCREMENT PRIMARY KEY, make varchar(50), "
                "status varchar(10) DEFAULT 'new', "
                "owner_id int NOT NULL, "
                "CONSTRAINT fk_car_owner FOREIGN KEY (owner_id) REFERENCES person(person_id))"
            )
            db.execute("CREATE UNIQUE INDEX uq_person_name ON person (name)")
            db.execute("CREATE INDEX ix_car_owner ON car (owner_id)")
            # префиксный индекс — не полный индекс по колонке, не выводится
            db.execute("CREATE INDEX ix_car_make_prefix ON car (make(5))")
            db.execute(
                "CREATE UNIQUE INDEX uq_car_make_owner ON car (make, owner_id)"
            )

        new = db.new()

        class Person(new.main.Entity):
            pass

        class Tag(new.main.Entity):
            pass

        class Car(new.main.Entity):
            pass

        new.introspect()
        P = new.entities["Person"]
        C = new.entities["Car"]
        # PK не id + AUTO_INCREMENT
        self.assertEqual([a.name for a in P._pk_attrs_], ["person_id"])
        self.assertTrue(P._pk_.auto)
        # unique доходит и до атрибута, и до схемы
        self.assertEqual(P.name.is_unique, "uq_person_name")
        self.assertTrue(P.name.is_part_of_unique_index)
        schema_indexes = new.schema.tables[P._table_].indexes
        self.assertIn(
            "uq_person_name",
            {getattr(i, "name", None) for i in schema_indexes.values()},
        )
        # индекс на FK-колонке
        self.assertEqual(C.owner.index, "ix_car_owner")
        # строковый дефолт закавычен
        self.assertEqual(C.status.sql_default, "'new'")
        # префиксный индекс не выводится
        self.assertIsNone(C.make.index)
        self.assertNotIn("ix_car_make_prefix", {i.name for i in C._indexes_})
        # составной UNIQUE
        self.assertIn(
            ("uq_car_make_owner", ("make", "owner"), True),
            {
                (i.name, tuple(a.name for a in i.attrs), i.is_unique)
                for i in C._indexes_
                if not i.is_pk
            },
        )
        # связочная таблица → пара Set, без entity
        self.assertNotIn("PersonTag", new.entities)
        self.assertIs(P.tag_set.py_type, Tag)
        with db_session:
            p = P(name="ann")
            t = Tag(label="x")
            t.person_set.add(p)
        with db_session:
            self.assertEqual([x.label for x in P[p.person_id].tag_set], ["x"])

        # дамп: без служебного имени PK, без sql_default='NULL',
        # без pony_migrations
        path = os.path.join(self.dir, "models.py")
        dump_db = db.new()

        class Person(dump_db.main.Entity):
            pass

        class Tag(dump_db.main.Entity):
            pass

        class Car(dump_db.main.Entity):
            pass

        dump_db.introspect(dump=path)
        with open(path) as f:
            content = f.read()
        self.assertNotIn("name='PRIMARY'", content)
        self.assertNotIn("sql_default='NULL'", content)
        self.assertNotIn("PonyMigrations", content)
        self.assertIn("person_id = PrimaryKey(int, auto=True)", content)
        self.assertIn("name = Required(str, unique='uq_person_name')", content)
        self.assertIn(
            "unique(make, owner, name='uq_car_make_owner')", content
        )
        self.assertIn(
            "tag_set = Set('Tag', table='person_tag', column='tag_id', "
            "reverse='person_set')",
            content,
        )
        # дамп пастится и маппится
        paste_db = Database("sqlite", os.path.join(self.dir, "paste.db"), create_db=True)
        try:
            exec(content, {"db": paste_db})
            paste_db.generate_mapping(check_tables=False)
            self.assertEqual(
                paste_db.entities["Person"].tag_set.table, "person_tag"
            )
        finally:
            paste_db.disconnect()


class TestMigrationsConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, "migrations")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _config(self):
        return os.path.join(self.dir, "config.ini")

    def test_save_creates_dir_and_reads_back(self):
        path = migrations.save_config_db(self.dir, "myapp.models:db")
        self.assertEqual(path, self._config())
        self.assertTrue(os.path.isdir(self.dir))
        self.assertEqual(migrations.read_config_db(self.dir), "myapp.models:db")
        with open(path) as f:
            content = f.read()
        self.assertIn("[database]", content)
        self.assertIn("path = myapp.models:db", content)

    def test_save_preserves_other_sections(self):
        os.makedirs(self.dir)
        with open(self._config(), "w") as f:
            f.write("[other]\nkey = value\n\n[database]\npath = old:db\n")
        migrations.save_config_db(self.dir, "new:db")
        self.assertEqual(migrations.read_config_db(self.dir), "new:db")
        with open(self._config()) as f:
            content = f.read()
        self.assertIn("key = value", content)
        self.assertIn("path = new:db", content)

    def test_read_missing_or_empty_is_none(self):
        self.assertIsNone(migrations.read_config_db(self.dir))
        os.makedirs(self.dir)
        with open(self._config(), "w") as f:
            f.write("[other]\nkey = value\n")
        self.assertIsNone(migrations.read_config_db(self.dir))
        with open(self._config(), "w") as f:
            f.write("[database]\npath =   \n")
        self.assertIsNone(migrations.read_config_db(self.dir))

    def test_malformed_config_is_an_error(self):
        os.makedirs(self.dir)
        with open(self._config(), "w") as f:
            f.write("not an ini file\n")
        with self.assertRaises(migrations.MigrationError):
            migrations.read_config_db(self.dir)
        with self.assertRaises(migrations.MigrationError):
            migrations.save_config_db(self.dir, "x:y")

    def test_config_is_not_a_migration(self):
        migrations.save_config_db(self.dir, "x:y")
        self.assertEqual(migrations.list_migrations(self.dir), [])


class TestMigrationsConfigCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, "migrations")
        self.db = Database("sqlite", os.path.join(self.tmp, "t.db"), create_db=True)
        self.db.application("main", schema="public")
        self.module = types.ModuleType("pony_config_test_mod")
        self.module.db = self.db
        sys.modules[self.module.__name__] = self.module
        self.spec = self.module.__name__ + ":db"

    def tearDown(self):
        sys.modules.pop(self.module.__name__, None)
        self.db.disconnect()
        shutil.rmtree(self.tmp)

    def _run(self, argv):
        stderr = io.StringIO()
        original = sys.stderr
        sys.stderr = stderr
        try:
            code = migrations.main(argv)
        finally:
            sys.stderr = original
        return code, stderr.getvalue()

    def _config(self):
        return os.path.join(self.dir, "config.ini")

    def test_db_is_saved_and_reused(self):
        code, _ = self._run(
            ["migrations", "plan", "--db", self.spec, "--dir", self.dir]
        )
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self._config()))
        self.assertEqual(migrations.read_config_db(self.dir), self.spec)
        code, _ = self._run(["migrations", "plan", "--dir", self.dir])
        self.assertEqual(code, 0)

    def test_db_overrides_saved_config(self):
        migrations.save_config_db(self.dir, "old.module:db")
        code, _ = self._run(
            ["migrations", "plan", "--db", self.spec, "--dir", self.dir]
        )
        self.assertEqual(code, 0)
        self.assertEqual(migrations.read_config_db(self.dir), self.spec)

    def test_missing_db_and_config_mentions_config_path(self):
        code, err = self._run(["migrations", "plan", "--dir", self.dir])
        self.assertEqual(code, 1)
        self.assertIn(self._config(), err)

    def test_invalid_db_is_not_saved(self):
        code, _ = self._run(
            ["migrations", "plan", "--db", "not_a_spec", "--dir", self.dir]
        )
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self._config()))


class TestMigrationsModuleEntry(unittest.TestCase):
    """`python -m pony.migrations` принимает команду сразу, без `migrations`."""

    def _run(self, *args):
        import subprocess

        root = os.path.dirname(
            os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
        )
        return subprocess.run(
            [sys.executable, "-m", "pony.migrations", *args],
            capture_output=True,
            text=True,
            cwd=root,
        )

    def test_help(self):
        result = self._run("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("merge", result.stdout)
        self.assertIn("pony migrations", result.stdout)

    def test_rejects_migrations_prefix(self):
        result = self._run("migrations", "apply")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("python -m pony.migrations apply", result.stderr)


if __name__ == "__main__":
    unittest.main()
