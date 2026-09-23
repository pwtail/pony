"""Миграции pony ORM, этапы 1 и 3.

- `db.migrations.add()` — `0001_initial.sql` из деклараций моделей
  (вместо `generate_mapping(create_tables=True)`);
- `db.migrations.apply()` — применить неприменённые миграции;
- `db.migrations.plan()` — вычисленный порядок применения;
- `db.migrations.merge()` — миграция слияния для двух разошедшихся веток.

Файлы миграций:

- `.sql` — миграция схемы (применяется целиком);
- `.py` — миграция данных: исполняется **как скрипт** внутри `db_session`,
  с `__name__ == '__main__'`; текущая база — `from pony.migrate import db`;
- `.txt` — миграция слияния (no-op, только запись в `pony_migrations`).

Зависимости объявляются в первых строках файла: `-- depends: a.sql, b.py`
(или `# depends:` для `.py`). Порядок применения — топологическая сортировка
с лексикографическим tie-break; у графа должна быть ровно одна голова.

CLI::

    pony-migrate add --db myapp.models:db
    pony-migrate apply --db myapp.models:db [--fake]
    pony-migrate plan --db myapp.models:db
    pony-migrate merge --db myapp.models:db [--name NAME]
"""

import argparse
import configparser
import hashlib
import heapq
import importlib
import os
import re
import sys
from collections import namedtuple
from datetime import datetime, timezone

from pony.orm import core
from pony.orm.core import Database, db_session


MIGRATIONS_TABLE = "pony_migrations"

MigrationInfo = namedtuple("MigrationInfo", "name dependencies applied")


class MigrationError(core.OrmError):
    pass


class MigrationGraph:
    """Граф зависимостей миграций из директории.

    Зависимости — только явные: `-- depends:` / `# depends:` в первых строках
    файла; миграция без директивы — корень (ни от чего не зависит).
    """

    def __init__(self, directory):
        self.directory = directory
        self.names = list_migrations(directory)
        self.explicit = {}
        self.dependencies = {}
        for name in self.names:
            deps = _read_dependencies(os.path.join(directory, name), name) or []
            self.explicit[name] = deps
            self.dependencies[name] = deps
        for name, deps in self.dependencies.items():
            for dep in deps:
                if dep not in self.dependencies:
                    raise MigrationError(
                        "Migration %s depends on %s, which does not exist"
                        % (name, dep)
                    )
        self.order = _topological_sort(self.dependencies)
        dependents = {
            dep for deps in self.dependencies.values() for dep in deps
        }
        self.heads = [name for name in self.names if name not in dependents]

    def ancestors(self, head):
        """Все миграции, достижимые из head по зависимостям (включая сам head)."""
        result = set()
        stack = [head]
        while stack:
            name = stack.pop()
            if name in result:
                continue
            result.add(name)
            stack.extend(self.dependencies[name])
        return result


class MigrationsFacade:
    """Объект db.migrations: программный интерфейс миграций."""

    def __init__(self, db):
        self.db = db

    def add(self, directory="migrations", name=None):
        """Без имени — 0001_initial.sql из деклараций моделей;
        `add('some.py')` — следующая по номеру дата-миграция."""
        if name:
            return add_named_migration(self.db, directory, name)
        return add_migration(self.db, directory)

    def apply(self, directory="migrations", fake=False):
        """Применяет неприменённые миграции в порядке графа."""
        return apply_migrations(self.db, directory, fake=fake)

    def plan(self, directory="migrations"):
        """Порядок применения миграций: список MigrationInfo."""
        return plan_migrations(self.db, directory)

    def merge(self, directory="migrations", name="merge"):
        """Создаёт .txt-миграцию слияния для двух голов графа."""
        return merge_migration(self.db, directory, name=name)


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
        and name.endswith((".sql", ".py", ".txt"))
    ]
    return sorted(names)


def _read_dependencies(path, name):
    """Зависимости из директивы-комментария в первых строках файла:
    `-- depends: a.sql, b.py` для `.sql`/`.txt` и `# depends: a.sql` для `.py`.
    None — зависимостей нет; [] — директива есть, но список пуст."""
    comment = "#" if name.endswith(".py") else "--"
    pattern = re.compile(r"^%s\s*depends:\s*(.*)$" % re.escape(comment), re.I)
    with open(path) as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            if not stripped.startswith(comment):
                if comment == "#" and re.match(r"^depends\s*=", stripped):
                    raise MigrationError(
                        "Migration %s: dependencies in .py are declared by the "
                        "comment `# depends: ...`; a `depends` variable is not "
                        "supported" % name
                    )
                break
            match = pattern.match(stripped)
            if match:
                return [
                    dep.strip()
                    for dep in match.group(1).split(",")
                    if dep.strip()
                ]
    return None


def _topological_sort(dependencies):
    indegree = {name: len(deps) for name, deps in dependencies.items()}
    children = {name: [] for name in dependencies}
    for name, deps in dependencies.items():
        for dep in deps:
            children[dep].append(name)
    heap = [name for name in dependencies if indegree[name] == 0]
    heapq.heapify(heap)
    order = []
    while heap:
        name = heapq.heappop(heap)
        order.append(name)
        for child in children[name]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(heap, child)
    if len(order) != len(dependencies):
        remaining = [name for name in dependencies if name not in set(order)]
        cycle = _find_cycle(dependencies, set(remaining))
        raise MigrationError(
            "Circular dependencies between migrations: %s" % " -> ".join(cycle)
        )
    return order


def _find_cycle(dependencies, remaining):
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in remaining}
    stack = []

    def visit(name):
        color[name] = GRAY
        stack.append(name)
        for dep in dependencies[name]:
            if dep not in remaining:
                continue
            if color[dep] == GRAY:
                return stack[stack.index(dep):] + [dep]
            if color[dep] == WHITE:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        color[name] = BLACK
        return None

    for name in sorted(remaining):
        if color[name] == WHITE:
            found = visit(name)
            if found:
                return found
    return sorted(remaining)


def ensure_single_head(graph):
    if len(graph.heads) > 1:
        raise MigrationError(
            "Multiple migration heads: %s. Declare `-- depends: <head>` "
            "(or `# depends:` for .py) on the new migration, or close the "
            "branch with a merge migration: pony-migrate merge"
            % ", ".join(graph.heads)
        )


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


def add_named_migration(db, directory, file_name):
    """Создаёт следующую по номеру миграцию с указанным именем:
    `add some_name.py` → `000N_some_name.py` (дата-миграция-скрипт),
    `add some_name.sql` → `000N_some_name.sql` (заготовка SQL-миграции).
    В шапку подставляется текущая голова графа (`-- depends:` / `# depends:`)."""
    base, ext = os.path.splitext(file_name)
    if ext not in (".py", ".sql"):
        raise MigrationError(
            "Migration name must end with .py or .sql, got %r" % file_name
        )
    if not os.path.isdir(directory):
        os.makedirs(directory)
    graph = MigrationGraph(directory)
    ensure_single_head(graph)
    head = graph.heads[0] if graph.heads else None
    base = re.sub(r"^\d+_", "", base).strip("_") or "migration"
    name = "%04d_%s%s" % (_next_number(graph.names), base, ext)
    path = os.path.join(directory, name)
    if os.path.exists(path):
        raise MigrationError("Migration %r already exists" % path)
    lines = []
    if head is not None:
        comment = "#" if ext == ".py" else "--"
        lines.append("%s depends: %s" % (comment, head))
    if ext == ".py":
        if lines:
            lines.append("")
        lines.append("from pony.migrate import db")
        lines.append("")
        lines.append("if __name__ == '__main__':")
        lines.append("    pass")
    elif not lines:
        lines.append("-- migration has no dependencies")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
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


def _migrations_table_exists(db):
    with db_session:
        rows = db.select(
            "SELECT to_regclass($t) AS oid", {"t": MIGRATIONS_TABLE}
        )
        return rows[0] is not None


def applied_migrations(db):
    if not _migrations_table_exists(db):
        return {}
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


def _apply_sql(db, name, path, sha256):
    with open(path) as f:
        sql = f.read()
    with db_session(ddl=True):
        if _strip_sql_comments(sql).strip():
            db.execute(sql)
        _record(db, name, sha256)


def _strip_sql_comments(sql):
    return re.sub(r"--[^\n]*", "", sql)


def _apply_py(db, name, path, sha256):
    """Исполняет .py-миграцию как скрипт (`__main__`) внутри db_session.

    Миграция получает **свежую Database**, привязанную к той же БД, но без
    моделей приложения: модели в миграции определяются интроспекцией
    (`db.introspect()`); текущая база доступна как
    `from pony.migrate import db`.
    """
    from pony import migrate as pony_migrate

    with open(path) as f:
        source = f.read()
    module_globals = {
        "__name__": "__main__",
        "__file__": path,
    }
    migration_db = make_migration_database(db)

    def run():
        pony_migrate.db = migration_db
        try:
            exec(compile(source, path, "exec"), module_globals)
        finally:
            pony_migrate.db = None
        _record(migration_db, name, sha256)

    try:
        with db_session(ddl=True):
            run()
    finally:
        migration_db.disconnect()


def make_migration_database(db):
    """Свежая Database к той же БД, что и db, но без моделей приложения:
    в миграции модели определяются интроспекцией."""
    provider = db.provider
    if provider is None:
        raise MigrationError("Database object is not bound with a provider yet")
    pool = getattr(provider, "pool", None)
    args = getattr(pool, "args", ())
    kwargs = dict(getattr(pool, "kwargs", {}))
    migration_db = Database()
    migration_db.bind(type(provider), *args, **kwargs)
    return migration_db


def _apply_txt(db, name, sha256):
    with db_session:
        _record(db, name, sha256)


def plan_migrations(db, directory):
    """Порядок применения миграций с отметками применённых."""
    _check_supported(db)
    graph = MigrationGraph(directory)
    ensure_single_head(graph)
    applied = applied_migrations(db)
    return [
        MigrationInfo(name, tuple(graph.dependencies[name]), name in applied)
        for name in graph.order
    ]


def apply_migrations(db, directory, fake=False):
    """Применяет неприменённые миграции в порядке графа.
    Каждая миграция — в своей транзакции; после успеха записывается
    в pony_migrations (name, sha256, applied_at)."""
    _check_supported(db)
    graph = MigrationGraph(directory)
    ensure_single_head(graph)
    ensure_migrations_table(db)
    applied = applied_migrations(db)
    pending = []
    for name in graph.order:
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
        if fake:
            _apply_txt(db, name, sha256)
        elif name.endswith(".sql"):
            _apply_sql(db, name, path, sha256)
        elif name.endswith(".py"):
            _apply_py(db, name, path, sha256)
        else:
            _apply_txt(db, name, sha256)
    return [name for name, _, _ in pending]


def _next_number(names):
    max_number = 0
    for name in names:
        match = re.match(r"^(\d+)", name)
        if match:
            max_number = max(max_number, int(match.group(1)))
    return max_number + 1


def _render_history(merge_name, left_names, right_names, common_names):
    """ASCII-история двух веток: новые — вверху, 0001 — внизу."""
    width = max(
        [len(name) for name in left_names + right_names + [merge_name]] or [0]
    )
    left_x = 0
    right_x = width + 4
    center_x = (left_x + right_x) // 2

    def centered(text):
        return " " * max(center_x - len(text) // 2, 0) + text

    def axis_row():
        return " " * left_x + "│" + " " * (right_x - left_x - 1) + "│"

    lines = [centered(merge_name)]
    lines.append(" " * center_x + "│")
    lines.append(
        "┌" + "─" * (center_x - 1) + "┴" + "─" * (right_x - center_x - 1) + "┐"
    )
    lines.append(axis_row())
    rows = max(len(left_names), len(right_names))
    for i in range(rows):
        left = left_names[i] if i < len(left_names) else "│"
        right = right_names[i] if i < len(right_names) else "│"
        lines.append((left + " " * (right_x - len(left)) + right).rstrip())
        if i != rows - 1:
            lines.append(axis_row())
    lines.append(axis_row())
    lines.append(
        "└" + "─" * (center_x - 1) + "┬" + "─" * (right_x - center_x - 1) + "┘"
    )
    if common_names:
        lines.append(" " * center_x + "│")
        tail_width = max(len(name) for name in common_names)
        tail_x = max(center_x - tail_width // 2, 0)
        for name in common_names:
            lines.append(" " * tail_x + name)
    return [line.rstrip() for line in lines]


def merge_migration(db, directory, name="merge"):
    """Создаёт .txt-миграцию слияния для двух голов графа.
    Возвращает путь нового файла; None, если сливать нечего."""
    graph = MigrationGraph(directory)
    heads = graph.heads
    if not heads:
        raise MigrationError("No migrations found in %r" % directory)
    if len(heads) == 1:
        return None
    if len(heads) > 2:
        raise MigrationError(
            "Cannot merge %d heads (%s): exactly two branches are supported, "
            "merge two of them first" % (len(heads), ", ".join(heads))
        )
    head_a, head_b = heads
    reach_a = graph.ancestors(head_a)
    reach_b = graph.ancestors(head_b)
    common = reach_a & reach_b
    left_names = [n for n in reversed(graph.order) if n in reach_a - common]
    right_names = [n for n in reversed(graph.order) if n in reach_b - common]
    common_names = [n for n in reversed(graph.order) if n in common]
    number = _next_number(graph.names)
    file_name = "%04d_%s.txt" % (number, name)
    path = os.path.join(directory, file_name)
    if os.path.exists(path):
        raise MigrationError("Migration %r already exists" % path)
    art = _render_history(file_name, left_names, right_names, common_names)
    lines = ["-- depends: %s, %s" % (head_a, head_b), "--"]
    lines.extend(("-- " + line).rstrip() for line in art)
    with open(path, "w") as f:
        f.write("\n".join(lines))
        f.write("\n")
    return path


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


def _print_plan(db, directory):
    infos = plan_migrations(db, directory)
    if not infos:
        print("no migrations found")
        return
    for info in infos:
        mark = "applied" if info.applied else "pending"
        deps = (
            " (depends on: %s)" % ", ".join(info.dependencies)
            if info.dependencies
            else ""
        )
        print("  [%s] %s%s" % (mark, info.name, deps))
    applied = sum(1 for info in infos if info.applied)
    print("head: %s" % infos[-1].name)
    print("applied %d of %d" % (applied, len(infos)))
    known = {info.name for info in infos}
    stale = sorted(set(applied_migrations(db)) - known)
    if stale:
        print(
            "applied migrations missing from the directory: %s"
            % ", ".join(stale)
        )


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
    add_parser = sub.add_parser(
        "add",
        parents=[common],
        help="generate 0001_initial.sql from the model declarations, "
        "or create the next numbered migration from a name",
    )
    add_parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help="migration file name, e.g. seed_users.py (data migration) "
        "or add_orders.sql (empty schema migration)",
    )
    apply_parser = sub.add_parser(
        "apply",
        parents=[common],
        help="apply pending migrations",
    )
    apply_parser.add_argument(
        "--fake",
        action="store_true",
        help="record migrations as applied without running them",
    )
    sub.add_parser(
        "plan",
        parents=[common],
        help="show the order in which migrations would be applied",
    )
    merge_parser = sub.add_parser(
        "merge",
        parents=[common],
        help="create a merge migration for two diverged branches",
    )
    merge_parser.add_argument(
        "--name", default="merge", help="merge migration name (default: merge)"
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
            if args.name:
                path = add_named_migration(db, directory, args.name)
            else:
                path = add_migration(db, directory)
            print("created %s" % path)
        elif args.command == "plan":
            _print_plan(db, directory)
        elif args.command == "merge":
            path = merge_migration(db, directory, name=args.name)
            if path is None:
                print("nothing to merge: the graph has a single head")
            else:
                print("created %s" % path)
        else:
            pending = apply_migrations(db, directory, fake=args.fake)
            if not pending:
                print("nothing to migrate")
            else:
                for name in pending:
                    print("applied %s" % name)
    except (MigrationError, core.OrmError) as e:
        print("pony-migrate: error: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
