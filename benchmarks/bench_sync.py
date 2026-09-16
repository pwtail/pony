"""Замер sync-пути: апстрим vs Gen-драйвер (пункт 1-а, риск R1 спеки).

Выбор СУБД опцией --db, выбор движка опцией --engine:

    python benchmarks/bench_sync.py --db sqlite --engine gen
    python benchmarks/bench_sync.py --db sqlite --engine native --native-path <апстрим>

--engine auto (по умолчанию) — мерит то дерево, которое импортировано;
--engine native в форке перезапускает замер в дереве апстрима через subprocess
(PYTHONPATH, путь из --native-path или PONY_NATIVE_PATH); --engine gen в дереве
апстрима — ошибка (там нет Gen-движка).

Сценарий: 1000 объектов — update по pk, select всей таблицы (200 раз),
вставка 1000 объектов. Метрики — секунды на операцию.
"""

import argparse
import os
import sys
import time

import pony.options

# в старом дереве опции IO_GUARD нет — игнорируем
try:
    pony.options.IO_GUARD = False
except AttributeError:
    pass

from pony.orm import (  # noqa: E402
    Database,
    Optional,
    Required,
    db_session,
    select,
)

import pony  # noqa: E402

try:
    from pony.orm.session_cache import SessionCacheGen  # noqa: F401

except ImportError:
    ENGINE = "native"  # апстрим: отдельный sync-движок
else:
    ENGINE = "gen"  # форк: sync через Gen-драйвер


parser = argparse.ArgumentParser()
parser.add_argument("--db", choices=("sqlite", "postgres"), default="sqlite")
parser.add_argument(
    "--engine",
    choices=("auto", "gen", "native"),
    default="auto",
    help="auto — то дерево, которое импортировано; native — перезапуск замера "
    "в нативном дереве апстрима через PYTHONPATH (нужен --native-path)",
)
parser.add_argument(
    "--native-path",
    default=os.environ.get("PONY_NATIVE_PATH"),
    help="путь к дереву с нативным sync-движком (апстрим) для --engine native",
)
args = parser.parse_args()

if args.engine == "native" and ENGINE == "gen":
    # в форке нативного sync-движка нет — перезапускаемся в дереве апстрима
    if not args.native_path:
        sys.exit("для --engine native нужен --native-path (или PONY_NATIVE_PATH)")
    import subprocess

    env = dict(os.environ)
    env["PYTHONPATH"] = args.native_path + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [
        sys.executable,
        os.path.abspath(__file__),
        "--db",
        args.db,
        "--engine",
        "auto",
    ]
    sys.exit(subprocess.run(cmd, env=env).returncode)

if args.engine == "gen" and ENGINE == "native":
    sys.exit("engine=gen недоступен в этом дереве (нет SessionCacheGen)")

if args.db == "sqlite":
    db = Database("sqlite", ":memory:")
else:
    import psycopg  # noqa: E402

    DSN = os.environ.get(
        "PONY_BENCH_DSN", "dbname=pony_async_test user=postgres host=localhost"
    )
    # сброс схемы перед замером
    conn = psycopg.connect(DSN)
    conn.autocommit = True
    conn.cursor().execute("DROP TABLE IF EXISTS e CASCADE")
    conn.close()
    db = Database("postgres", DSN)  # conninfo позиционно: psycopg3 не принимает dsn=


class E(db.Entity):
    name = Required(str)
    x = Optional(int)


db.generate_mapping(create_tables=True)

with db_session:
    for i in range(1000):
        E(name=str(i), x=i)

t0 = time.perf_counter()
with db_session:
    for i in range(1000):
        e = E[i + 1]
        e.x = i * 2
t_update = time.perf_counter() - t0

t0 = time.perf_counter()
with db_session:
    for _ in range(200):
        select(o for o in E)[:]
t_select = time.perf_counter() - t0

t0 = time.perf_counter()
with db_session:
    for i in range(1000):
        E(name=str(1000 + i), x=i)
t_insert = time.perf_counter() - t0

print(
    "pony=%s engine=%s db=%s update1000 %.3f  select200 %.3f  insert1000 %.3f"
    % (pony.__file__, ENGINE, args.db, t_update, t_select, t_insert)
)
