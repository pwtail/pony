"""Точка входа миграций pony: `python -m pony.migrations` и команда `pony migrations`.

`python -m pony.migrations` принимает команду сразу (`python -m pony.migrations
apply`); слово `migrations` — подкоманда консольного `pony`, а не модуля.

Реализация — в `pony.orm.migrations`. Миграция данных — это `.py`-скрипт,
который исполняется раннером (`runpy.run_path` с `__name__ == '__main__'`,
внутри внешней `db_session` раннера — одна транзакция на миграцию); свежую
базу к той же БД скрипт создаёт сам::

    from pony.orm import *

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
    applied_migrations,
    apply_migrations,
    list_migrations,
    main,
    make_migration,
    make_named_migration,
    merge_migration,
    plan_migrations,
)

__all__ = [
    "MigrationError",
    "MigrationGraph",
    "MigrationsFacade",
    "applied_migrations",
    "apply_migrations",
    "list_migrations",
    "main",
    "make_migration",
    "make_named_migration",
    "merge_migration",
    "plan_migrations",
]


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "migrations":
        print(
            "pony migrations: error: `migrations` is a subcommand of the "
            "`pony` command; with `python -m pony.migrations` pass the "
            "command directly, e.g. `python -m pony.migrations apply`",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.exit(main(argv))
