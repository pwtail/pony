import os
import shutil
import sys
import tempfile
import types
import unittest

from pony.orm import Database, db_session
from pony.orm import migrations
from pony.orm.tests import db_params, only_for, teardown_database


@only_for("postgres")
class TestMigrationGraph(unittest.TestCase):
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
                name
                for name, _ in self.db.select(
                    "SELECT name, sha256 FROM pony_migrations ORDER BY name"
                )
            ]

    def test_topological_order_and_tie_break(self):
        db = self.db
        self._write("0001_initial.sql", 'CREATE TABLE "t1" (id integer)')
        self._write(
            "0002_a.sql",
            '-- depends: 0001_initial.sql\nCREATE TABLE "t2" (id integer)',
        )
        self._write(
            "0003_b.sql",
            '-- depends: 0001_initial.sql\nCREATE TABLE "t3" (id integer)',
        )
        self._write(
            "0004_merge.txt",
            "-- depends: 0002_a.sql, 0003_b.sql\n--\n-- merge\n",
        )
        graph = migrations.MigrationGraph(self.dir)
        self.assertEqual(
            graph.order,
            [
                "0001_initial.sql",
                "0002_a.sql",
                "0003_b.sql",
                "0004_merge.txt",
            ],
        )
        self.assertEqual(graph.heads, ["0004_merge.txt"])
        infos = migrations.plan_migrations(db, self.dir)
        self.assertEqual(
            [info.name for info in infos],
            [
                "0001_initial.sql",
                "0002_a.sql",
                "0003_b.sql",
                "0004_merge.txt",
            ],
        )
        self.assertEqual(infos[1].dependencies, ("0001_initial.sql",))
        self.assertEqual(
            infos[3].dependencies, ("0002_a.sql", "0003_b.sql")
        )

    def test_explicit_dependencies(self):
        self._write("0001_initial.sql", 'CREATE TABLE "t1" (id integer)')
        self._write(
            "0002_a.sql",
            '-- depends: 0001_initial.sql\nCREATE TABLE "t2" (id integer)',
        )
        self._write(
            "0003_b.sql",
            '-- depends: 0001_initial.sql\nCREATE TABLE "t3" (id integer)',
        )
        graph = migrations.MigrationGraph(self.dir)
        self.assertEqual(graph.dependencies["0001_initial.sql"], [])
        self.assertEqual(
            graph.dependencies["0002_a.sql"], ["0001_initial.sql"]
        )
        self.assertEqual(
            graph.dependencies["0003_b.sql"], ["0001_initial.sql"]
        )
        self.assertEqual(graph.heads, ["0002_a.sql", "0003_b.sql"])

    def test_py_depends_variable_is_an_error(self):
        db = self.db
        self._write("0001_initial.sql", 'CREATE TABLE "t1" (id integer)')
        self._write(
            "0002_data.py",
            "depends = ['0001_initial.sql']\n\ndef up(db):\n    pass\n",
        )
        with self.assertRaises(migrations.MigrationError) as ctx:
            migrations.plan_migrations(db, self.dir)
        self.assertIn("# depends", str(ctx.exception))

    def test_missing_dependency_is_an_error(self):
        db = self.db
        self._write("0001_a.sql", "-- depends: 0099_x.sql\nSELECT 1")
        with self.assertRaises(migrations.MigrationError) as ctx:
            migrations.plan_migrations(db, self.dir)
        self.assertIn("0099_x.sql", str(ctx.exception))

    def test_cycle_is_an_error(self):
        db = self.db
        self._write("0001_a.sql", "-- depends: 0002_b.sql\nSELECT 1")
        self._write("0002_b.sql", "-- depends: 0001_a.sql\nSELECT 1")
        with self.assertRaises(migrations.MigrationError) as ctx:
            migrations.plan_migrations(db, self.dir)
        self.assertIn("->", str(ctx.exception))

    def test_multiple_heads_are_an_error(self):
        db = self.db
        self._write("0001_initial.sql", 'CREATE TABLE "t1" (id integer)')
        self._write(
            "0002_a.sql",
            '-- depends: 0001_initial.sql\nCREATE TABLE "t2" (id integer)',
        )
        self._write(
            "0003_b.sql",
            '-- depends: 0001_initial.sql\nCREATE TABLE "t3" (id integer)',
        )
        with self.assertRaises(migrations.MigrationError) as ctx:
            migrations.plan_migrations(db, self.dir)
        message = str(ctx.exception)
        self.assertIn("0002_a.sql", message)
        self.assertIn("0003_b.sql", message)
        self.assertIn("merge", message)
        with self.assertRaises(migrations.MigrationError):
            migrations.apply_migrations(db, self.dir)

    def test_txt_merge_migration_is_noop(self):
        db = self.db
        self._write("0001_initial.sql", 'CREATE TABLE "t1" (id integer)')
        self._write(
            "0002_a.sql",
            '-- depends: 0001_initial.sql\nCREATE TABLE "t2" (id integer)',
        )
        self._write(
            "0003_b.sql",
            '-- depends: 0001_initial.sql\nCREATE TABLE "t3" (id integer)',
        )
        self._write(
            "0004_merge.txt",
            "-- depends: 0002_a.sql, 0003_b.sql\n--\n-- merge\n",
        )
        self.assertEqual(
            migrations.apply_migrations(db, self.dir),
            [
                "0001_initial.sql",
                "0002_a.sql",
                "0003_b.sql",
                "0004_merge.txt",
            ],
        )
        self.assertEqual(len(self._applied_names()), 4)

    def test_merge_command_creates_file(self):
        db = self.db
        self._write("0001_initial.sql", 'CREATE TABLE "t1" (id integer)')
        self._write(
            "0002_a.sql",
            '-- depends: 0001_initial.sql\nCREATE TABLE "t2" (id integer)',
        )
        self._write(
            "0003_b.sql",
            '-- depends: 0001_initial.sql\nCREATE TABLE "t3" (id integer)',
        )
        path = migrations.merge_migration(db, self.dir)
        self.assertEqual(os.path.basename(path), "0004_merge.txt")
        with open(path) as f:
            content = f.read()
        self.assertTrue(
            content.startswith(
                "-- depends: 0002_a.sql, 0003_b.sql\n"
            )
        )
        self.assertIn("┴", content)
        self.assertIn("┬", content)
        lines = content.splitlines()
        merge_line = next(
            i for i, line in enumerate(lines) if "0004_merge.txt" in line
        )
        head_line = next(
            i
            for i, line in enumerate(lines)
            if "0002_a.sql" in line and i > 0
        )
        tail_line = next(
            i for i, line in enumerate(lines) if "0001_initial.sql" in line
        )
        self.assertLess(merge_line, head_line)
        self.assertLess(head_line, tail_line)
        # граф закрыт: одна голова, миграции применяются
        self.assertEqual(
            migrations.apply_migrations(db, self.dir),
            [
                "0001_initial.sql",
                "0002_a.sql",
                "0003_b.sql",
                "0004_merge.txt",
            ],
        )

    def test_merge_with_single_head_does_nothing(self):
        db = self.db
        self._write("0001_initial.sql", 'CREATE TABLE "t1" (id integer)')
        self.assertIsNone(migrations.merge_migration(db, self.dir))

    def test_merge_with_three_heads_is_an_error(self):
        db = self.db
        self._write("0001_initial.sql", 'CREATE TABLE "t1" (id integer)')
        for suffix in ("a", "b", "c"):
            self._write(
                "0002_%s.sql" % suffix,
                '-- depends: 0001_initial.sql\nSELECT 1',
            )
        with self.assertRaises(migrations.MigrationError):
            migrations.merge_migration(db, self.dir)

    def test_cli_plan_merge_and_no_dry_run(self):
        db = self.db
        module = types.ModuleType("pony_migrations_graph_mod")
        module.db = db
        sys.modules[module.__name__] = module
        try:
            self._write("0001_initial.sql", 'CREATE TABLE "t1" (id integer)')
            self._write(
                "0002_a.sql",
                '-- depends: 0001_initial.sql\nCREATE TABLE "t2" (id integer)',
            )
            self._write(
                "0003_b.sql",
                '-- depends: 0001_initial.sql\nCREATE TABLE "t3" (id integer)',
            )
            code = migrations.main(
                ["plan", "--db", module.__name__ + ":db", "--dir", self.dir]
            )
            self.assertEqual(code, 1)  # две головы — ошибка
            code = migrations.main(
                ["merge", "--db", module.__name__ + ":db", "--dir", self.dir]
            )
            self.assertEqual(code, 0)
            code = migrations.main(
                ["plan", "--db", module.__name__ + ":db", "--dir", self.dir]
            )
            self.assertEqual(code, 0)
            code = migrations.main(
                ["apply", "--db", module.__name__ + ":db", "--dir", self.dir]
            )
            self.assertEqual(code, 0)
            with self.assertRaises(SystemExit):
                migrations.main(
                    [
                        "apply",
                        "--db",
                        module.__name__ + ":db",
                        "--dir",
                        self.dir,
                        "--dry-run",
                    ]
                )
        finally:
            del sys.modules[module.__name__]
        self.assertEqual(len(self._applied_names()), 4)


if __name__ == "__main__":
    unittest.main()
