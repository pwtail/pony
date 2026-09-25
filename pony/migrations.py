"""Точка входа миграций pony: `python -m pony.migrations` и команда `pony migrations`.

Реализация — в `pony.orm.migrations`. Миграция данных — это `.py`-скрипт,
который исполняется раннером (`runpy.run_path` с `__name__ == '__main__'`,
внутри внешней `db_session` раннера — одна транзакция на миграцию); свежую
базу к той же БД скрипт создаёт сам::

    from pony.orm import Database, db_session

    db = Database.instance().new()

    if __name__ == '__main__':
        with db_session:
            ...
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
    "list_migrations",
    "main",
    "merge_migration",
    "plan_migrations",
]


if __name__ == "__main__":
    sys.exit(main())
