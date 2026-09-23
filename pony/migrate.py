"""Точка входа миграций pony: `python -m pony.migrate` и команда `pony-migrate`.

Реализация — в `pony.orm.migrations`. Миграция данных — это `.py`-скрипт,
который исполняется раннером (как `__main__`, внутри `db_session`); текущая
база доступна так::

    from pony.migrate import db

    if __name__ == '__main__':
        ...

Атрибут `db` выставляет раннер на время применения миграции; вне применения
он равен ``None``.
"""

import sys

from pony.orm.migrations import (
    MigrationError,
    MigrationGraph,
    MigrationsFacade,
    add_migration,
    add_named_migration,
    applied_migrations,
    apply_migrations,
    list_migrations,
    main,
    merge_migration,
    plan_migrations,
)

__all__ = [
    "MigrationError",
    "MigrationGraph",
    "MigrationsFacade",
    "add_migration",
    "add_named_migration",
    "applied_migrations",
    "apply_migrations",
    "db",
    "list_migrations",
    "main",
    "merge_migration",
    "plan_migrations",
]

db = None


if __name__ == "__main__":
    sys.exit(main())
