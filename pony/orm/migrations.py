"""Миграции pony ORM, этап 1: генерация начального SQL из деклараций моделей,
применение миграций (.sql / .py) с трекингом применённых в таблице
`pony_migrations`. Пока — без графа зависимостей: миграции применяются
в лексикографическом порядке имён файлов. Диалект этапа 1 — PostgreSQL.

Python API::

    db.migrations.add()       # 0001_initial.sql из деклараций моделей
    db.migrations.apply()     # применить неприменённые миграции

CLI::

    pony-migrate add --db myapp.models:db
    pony-migrate apply --db myapp.models:db
"""

import argparse
import configparser
import hashlib
import importlib
import os
import sys
import types
from datetime import datetime, timezone

from pony.orm import core
from pony.orm.core import Database, db_session


MIGRATIONS_TABLE = "pony_migrations"


class MigrationError(core.OrmError):
    pass


class MigrationsFacade:
    """Объект db.migrations: программный интерфейс миграций."""

    def __init__(self, db):
        self.db = db

    def add(self, directory="migrations"):
        """Создаёт 0001_initial.sql из деклараций моделей."""
        return add_migration(self.db, directory)

    def apply(self, directory="migrations", dry_run=False, fake=False):
        """Применяет неприменённые миграции в лексикографическом порядке."""
        return apply_migrations(
            self.db, directory, dry_run=dry_run, fake=fake
        )


def _check_supported(db):
    if db.provider is None:
        raise MigrationError("Database object is not bound with a provider yet")
    if db.provider.dialect != "PostgreSQL":
        raise MigrationError(
            "Migrations are supported only with PostgreSQL for now (got %s)"
            % db.provider.dialect
        )


def list_migrations(directory):
    if not os.path.isdir(directory):
        raise MigrationError("Migrations directory %r does not exist" % directory)
    names = [
        name
        for name in os.listdir(directory)
        if not name.startswith("__")
        and not name.endswith(".down.sql")
        and name.endswith((".sql", ".py"))
    ]
    return sorted(names)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def add_migration(db, directory, name="0001_initial.sql"):
    """Вместо generate_mapping(create_tables=True): пишет 0001_initial.sql
    из деклараций моделей."""
    _check_supported(db)
    if db.schema is None:
        db.generate_mapping(check_tables=False, create_tables=False)
    path = os.path.join(directory, name)
    if os.path.exists(path):
        raise MigrationError("Migration %r already exists" % path)
    os.makedirs(directory, exist_ok=True)
    with open(path, "w") as f:
        f.write(db.schema.generate_create_script())
        f.write("\n")
    return path


def ensure_migrations_table(db):
    sql = (
        "CREATE TABLE IF NOT EXISTS %s ("
        "name varchar(255) PRIMARY KEY, "
        "sha256 varchar(64) NOT NULL, "
        "applied_at timestamptz NOT NULL DEFAULT now())" % MIGRATIONS_TABLE
    )
    with db_session(ddl=True):
        db.execute(sql)


def applied_migrations(db):
    with db_session:
        rows = db.select(
            "SELECT name, sha256 FROM %s ORDER BY name" % MIGRATIONS_TABLE
        )
        return {name: sha256 for name, sha256 in rows}


def _record(db, name, sha256):
    db.insert(
        MIGRATIONS_TABLE,
        name=name,
        sha256=sha256,
        applied_at=datetime.now(timezone.utc),
    )


def _load_py_migration(name, path):
    with open(path) as f:
        source = f.read()
    module_name = "pony_migration_" + "".join(
        c if c.isalnum() else "_" for c in os.path.splitext(name)[0]
    )
    module = types.ModuleType(module_name)
    module.__file__ = path
    exec(compile(source, path, "exec"), module.__dict__)
    return module


def _apply_sql(db, name, path, sha256):
    with open(path) as f:
        sql = f.read()
    with db_session(ddl=True):
        db.execute(sql)
        _record(db, name, sha256)


def _apply_py(db, name, path, sha256):
    module = _load_py_migration(name, path)
    up = getattr(module, "up", None)
    if not callable(up):
        raise MigrationError(
            "Python migration %s must define an up(db) function" % name
        )

    def run():
        up(db)
        _record(db, name, sha256)

    with db_session():
        run()


def apply_migrations(db, directory, dry_run=False, fake=False):
    """Применяет неприменённые миграции в лексикографическом порядке.
    Каждая миграция — в своей транзакции; после успеха записывается
    в pony_migrations (name, sha256, applied_at)."""
    _check_supported(db)
    ensure_migrations_table(db)
    applied = applied_migrations(db)
    pending = []
    for name in list_migrations(directory):
        path = os.path.join(directory, name)
        sha256 = file_sha256(path)
        if name in applied:
            if applied[name] != sha256:
                raise MigrationError(
                    "Migration %s was modified after it was applied" % name
                )
            continue
        pending.append((name, path, sha256))
    for name, path, sha256 in pending:
        if dry_run:
            continue
        if fake:
            with db_session:
                _record(db, name, sha256)
        elif name.endswith(".sql"):
            _apply_sql(db, name, path, sha256)
        else:
            _apply_py(db, name, path, sha256)
    return [name for name, _, _ in pending]


def _read_config(path):
    if path is None or not os.path.exists(path):
        return {}
    parser = configparser.ConfigParser()
    parser.read(path)
    if not parser.has_section("pony-migrate"):
        return {}
    return dict(parser.items("pony-migrate"))


def _load_db(spec):
    if ":" not in spec:
        raise MigrationError(
            "Invalid database specification %r: expected <module>:<attr>" % spec
        )
    module_name, attr_name = spec.split(":", 1)
    module = importlib.import_module(module_name)
    db = getattr(module, attr_name)
    if not isinstance(db, Database):
        raise MigrationError("%r is not a pony Database object" % spec)
    return db


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db", help="database object as <module>:<attr> (e.g. myapp.models:db)"
    )
    common.add_argument(
        "--config",
        default="pony_migrate.ini",
        help="config file with [pony-migrate] db=/migrations_dir= (default: pony_migrate.ini)",
    )
    common.add_argument(
        "--dir",
        dest="directory",
        help="migrations directory (default: migrations)",
    )
    parser = argparse.ArgumentParser(prog="pony-migrate")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "add",
        parents=[common],
        help="generate 0001_initial.sql from the model declarations",
    )
    apply_parser = sub.add_parser(
        "apply",
        parents=[common],
        help="apply pending migrations",
    )
    apply_parser.add_argument(
        "--dry-run", action="store_true", help="only print what would be applied"
    )
    apply_parser.add_argument(
        "--fake",
        action="store_true",
        help="record migrations as applied without running them",
    )
    args = parser.parse_args(argv)
    try:
        config = _read_config(args.config)
        spec = args.db or config.get("db")
        if not spec:
            raise MigrationError(
                "Database is not specified: use --db <module>:<attr> "
                "or [pony-migrate] db = ... in the config file"
            )
        db = _load_db(spec)
        directory = args.directory or config.get("migrations_dir") or "migrations"
        if args.command == "add":
            path = add_migration(db, directory)
            print("created %s" % path)
        else:
            pending = apply_migrations(
                db, directory, dry_run=args.dry_run, fake=args.fake
            )
            if not pending:
                print("nothing to migrate")
            else:
                for name in pending:
                    if args.dry_run:
                        print("would apply %s" % name)
                    else:
                        print("applied %s" % name)
    except (MigrationError, core.OrmError) as e:
        print("pony-migrate: error: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
