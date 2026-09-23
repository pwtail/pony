import os
import shutil
import sys
import tempfile
import types
import unittest

from pony.orm import Database, ProgrammingError, Required, db_session
from pony.orm import migrations
from pony.orm.tests import db_params, only_for, teardown_database


@only_for("postgres")
class TestMigrations(unittest.TestCase):
    def setUp(self):
        self.db = Database(**db_params)
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
        path = os.path.join(self.dir, name)
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

        class Person(db.Entity):
            name = Required(str)

        path = migrations.add_migration(db, self.dir)
        self.assertEqual(os.path.basename(path), "0001_initial.sql")
        with open(path) as f:
            content = f.read()
        self.assertIn('CREATE TABLE "person"', content)
        self.assertEqual(content.rstrip(), db.schema.generate_create_script())
        with self.assertRaises(migrations.MigrationError):
            migrations.add_migration(db, self.dir)

    def test_migrate_sql_and_py_in_order(self):
        db = self.db
        self._write("0001_create.sql", 'CREATE TABLE "t1" (id integer PRIMARY KEY)')
        self._write(
            "0002_seed.py",
            "# depends: 0001_create.sql\n"
            "from pony.migrate import db\n\n"
            "db.execute('INSERT INTO t1 (id) VALUES (1)')\n",
        )
        self.assertEqual(
            migrations.apply_migrations(db, self.dir),
            ["0001_create.sql", "0002_seed.py"],
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

        class Person(db.Entity):
            name = Required(str)

        migrations.add_migration(db, self.dir)  # 0001_initial.sql
        self._write(
            "0002_seed.py",
            "# depends: 0001_initial.sql\n"
            "from pony.migrate import db\n\n"
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

    def test_py_migration_runs_as_main_script(self):
        db = self.db
        self._write(
            "0001_name.py",
            "from pony.migrate import db\n\n"
            "assert __name__ == '__main__', __name__\n"
            "db.execute('CREATE TABLE nm (v text)')\n"
            "db.execute(\"INSERT INTO nm VALUES ('%s')\" % __name__)\n",
        )
        migrations.apply_migrations(db, self.dir)
        with db_session:
            self.assertEqual(list(db.select("SELECT v FROM nm")), ["__main__"])

    def test_pony_migrate_db_inside_migration(self):
        from pony import migrate as pony_migrate

        db = self.db
        self._write(
            "0001_x.py",
            "from pony.migrate import db\n\n"
            "def helper():\n"
            "    return db\n\n"
            "db.execute('CREATE TABLE iv (v text)')\n"
            "db.execute(\"INSERT INTO iv VALUES ('%s')\" % (helper() is db))\n",
        )
        self.assertIsNone(pony_migrate.db)
        migrations.apply_migrations(db, self.dir)
        self.assertIsNone(pony_migrate.db)
        with db_session:
            self.assertEqual(list(db.select("SELECT v FROM iv")), ["True"])

    def test_add_named_data_migration(self):
        db = self.db
        path = migrations.add_named_migration(db, self.dir, "some_name.py")
        self.assertEqual(os.path.basename(path), "0001_some_name.py")
        with open(path) as f:
            content = f.read()
        self.assertIn("from pony.migrate import db", content)
        self.assertIn("if __name__ == '__main__':", content)
        self.assertNotIn("depends", content)
        path2 = migrations.add_named_migration(db, self.dir, "second.py")
        self.assertEqual(os.path.basename(path2), "0002_second.py")
        with open(path2) as f:
            self.assertIn("# depends: 0001_some_name.py", f.read())
        self.assertEqual(
            migrations.apply_migrations(db, self.dir),
            ["0001_some_name.py", "0002_second.py"],
        )

    def test_add_named_sql_migration(self):
        db = self.db
        path = migrations.add_named_migration(db, self.dir, "add_orders.sql")
        self.assertEqual(os.path.basename(path), "0001_add_orders.sql")
        with open(path) as f:
            self.assertNotIn("depends", f.read())
        # заготовка (только комментарий) применяется без ошибки
        self.assertEqual(
            migrations.apply_migrations(db, self.dir), ["0001_add_orders.sql"]
        )
        self.assertEqual(self._applied_names(), ["0001_add_orders.sql"])

    def test_migration_gets_fresh_database(self):
        db = self.db

        class AppModel(db.Entity):
            name = Required(str)

        self._write(
            "0001_x.py",
            "from pony.migrate import db\n\n"
            "assert 'AppModel' not in db.entities\n"
            "assert db.schema is None\n"
            "assert db.provider is not None\n",
        )
        migrations.apply_migrations(db, self.dir)
        self.assertEqual(self._applied_names(), ["0001_x.py"])

    def test_module_entry_point(self):
        import subprocess

        root = os.path.dirname(
            os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
        )
        result = subprocess.run(
            [sys.executable, "-m", "pony.migrate", "--help"],
            capture_output=True,
            text=True,
            cwd=root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("merge", result.stdout)

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
                ["apply", "--db", module.__name__ + ":db", "--dir", self.dir]
            )
            self.assertEqual(code, 0)
        finally:
            del sys.modules[module.__name__]
        self.assertEqual(self._applied_names(), ["0001_a.sql"])

    def test_db_migrations_api(self):
        db = self.db

        class Person(db.Entity):
            name = Required(str)

        path = db.migrations.add(self.dir)
        self.assertEqual(os.path.basename(path), "0001_initial.sql")
        self.assertEqual(db.migrations.apply(self.dir), ["0001_initial.sql"])
        self.assertEqual(self._applied_names(), ["0001_initial.sql"])
        with db_session:
            tables = db.select(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = current_schema()"
            )
            self.assertEqual(
                sorted(tables), ["person", "pony_migrations"]
            )

    def test_cli_add_and_config(self):
        db = self.db
        module = types.ModuleType("pony_migrations_test_mod")
        module.db = db
        sys.modules[module.__name__] = module
        config_path = os.path.join(self.dir, "pony_migrate.ini")
        with open(config_path, "w") as f:
            f.write(
                "[pony-migrate]\n"
                "db = %s:db\n"
                "migrations_dir = %s\n" % (module.__name__, self.dir)
            )
        try:
            class Person(db.Entity):
                name = Required(str)

            code = migrations.main(["add", "--config", config_path])
            self.assertEqual(code, 0)
            self.assertTrue(
                os.path.exists(os.path.join(self.dir, "0001_initial.sql"))
            )
            code = migrations.main(["apply", "--config", config_path])
            self.assertEqual(code, 0)
        finally:
            del sys.modules[module.__name__]
        self.assertEqual(self._applied_names(), ["0001_initial.sql"])


class TestMigrationsNotSupported(unittest.TestCase):
    def setUp(self):
        self.db = Database(**db_params)

    def tearDown(self):
        teardown_database(self.db)

    def test_non_postgres_rejected(self):
        if self.db.provider.dialect == "PostgreSQL":
            self.skipTest("migrations are supported on PostgreSQL")
        with self.assertRaises(migrations.MigrationError):
            migrations.apply_migrations(self.db, tempfile.mkdtemp())
        with self.assertRaises(migrations.MigrationError):
            migrations.add_migration(self.db, tempfile.mkdtemp())


if __name__ == "__main__":
    unittest.main()
