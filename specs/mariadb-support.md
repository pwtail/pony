---
id: pony-mariadb-support
type: task-spec
title: Поддержка MariaDB (коннектор mariadb 2.0RC)
status: done
parent: pony-async-architecture
depends-on: [pony-async-shared-code]
tags: [mariadb, async, sync, provider]
---

# Цель

Добавить в форк поддержку MariaDB через Python-коннектор `mariadb` версии
**2.0RC** (`pip install --pre mariadb`, проверено: 2.0.0rc2): sync-режим через
PEP-249 (`mariadb.dbapi20`), async-режим через встроенный asyncio-модуль
(`mariadb.asyncio`). Gen-слой (пункт 1-а) при этом **не меняется** — новая СУБД
подключается только провайдером и ProviderOps.

# Факты о коннекторе (проверено на 2.0.0rc2+native)

- `mariadb.dbapi20` — полноценный dbapi2-модуль: `connect(*args, **kwargs)`,
  `paramstyle`, типы данных; sync.
- `mariadb.asyncio` — парный async-модуль в стиле psycopg3:
  `await connect(...)` → `AsyncConnection`; `connection.cursor()` (sync-вызов)
  → `AsyncCursor`; `cursor.execute/executemany/fetchone/fetchmany/fetchall` —
  await-методы; `commit/rollback/close` — await-методы; атрибуты
  `description/lastrowid/rowcount`; `autocommit` + `set_autocommit()`.
- Исключения: `mariadb.exceptions` (dbapi-классы `Error`, `IntegrityError`,
  `OperationalError`, ...) — общие для sync и async.
- **Пул** (extras `mariadb[pool]`, отдельный дистрибутив `mariadb-pool`,
  импорт `mariadb_pool`): `ConnectionPool` (sync) и `AsyncConnectionPool`
  (`await open()`, `await acquire()`, `release()`, `close()`, контекстный
  менеджер `connection`); `PoolConfig(min_size=10, max_size=10,
  max_idle_time, max_lifetime, acquire_timeout, enable_health_check,
  reset_connection=False, ...)`.
- RC-статус коннектора: возможны изменения API до релиза.

# Решения

1. **Sync-провайдер** `dbproviders/mariadb.py`: диалект MySQL-совместимый —
   наследует/повторяет `MySQLProvider` (dbproviders/mysql.py), но
   `dbapi_module = mariadb.dbapi20`; имя провайдера в реестре — `"mariadb"`
   (`known_providers`). Специфика MariaDB (server_version и пр.) — по месту.
2. **Async-провайдер** `dbproviders/mariadb_async.py` по образцу
   `postgres_async.py`: `MariadbAsyncProvider` с `async_*` методами
   (`async_connect/async_execute/async_commit/async_rollback/async_release/
   async_drop/async_set_transaction_mode`) поверх `mariadb.asyncio`;
   `provider_cls` для реестра, имя `"mariadb_async"`.
3. **Пул async-соединений**: официальный `mariadb_pool.AsyncConnectionPool`
   (loop-локальный, как `_AsyncPools` в postgres_async: пул создаётся лениво
   при первом async-использовании, `open()` — await-точка);
   `PoolConfig(min_size=1, max_size=10, reset_connection=True)` — сброс
   соединения при возврате вместо DISCARD ALL.
4. **ProviderOps**: `SyncOps` из gen_core подходит как есть (делегирует
   dbapi2-методы); для async — `MariadbAsyncOps` (аналог `AsyncOps`, вызовы
   `mariadb.asyncio`). Gen-код (session_cache/async_core/core-делегаты) не
   трогается — это и есть проверка универсальности пункта 1-а.
5. **Конфигурация**: зависимости в extras (pyproject):
   `mariadb>=2.0.0rc2` и `mariadb-pool>=2.0.0rc2` (вместе — extras
   `mariadb[pool]`), не в обязательные.

# Инварианты

- **I1.** Существующие сьюты (sync 3972 + async 10) зелёные без правок —
   новая СУБД ничего не ломает в Postgres/sqlite-путях.
- **I2.** Sync- и async-сессии MariaDB проходят одни и те же тесты-сценарии
   (создание/выборка/обновление/удаление, коллекции, fetch/load).
- **I3.** Sync- и async-операции MariaDB идут через тот же Gen-слой и ProviderOps,
   что и PostgreSQL (общий код, различия только в коннекторе).

# Риски и открытые вопросы

- **R1. RC-коннектор**: 2.0.0rc2 может менять API; при переходе на релиз —
   перепроверка. В спеке фиксируется точная версия, с которой проверено.
- **R2. Свой async-пул (закрыт).** Пул — `mariadb_pool.AsyncConnectionPool`;
  возврат соединения при отмене корутины покрыт тестом
  `test_cancellation_rolls_back_and_returns_connection` в `test_mariadb_async.py`.
- **R3. Тестовая среда (закрыт).** Локальный контейнер `pony-mariadb` (MariaDB 11.8,
  `--lower-case-table-names=1 --sql-mode=...ANSI_QUOTES` — как в CI) поднимается одной
  командой, тесты идут против него без skip; есть и MySQL-контейнер для совместимости.
- **Q1. Диалектные отличия (закрыт).** Проверено на живых MariaDB 11.8 и MySQL 8.4:
  JSON (`JSON_EQUALS`, сравнение с текстом, bool-значения, `JSON_PARAM`),
  `CLIENT.FOUND_ROWS`, коды ошибок CHECK (3819 → `IntegrityError`), EXISTS-обёртка для
  DELETE/UPDATE (1093 — только настоящий MySQL), LIMIT в IN-подзапросе, слайсы,
  `LIKE BINARY`, `TIMESTAMPDIFF`/INTERVAL. Провайдер определяет сервер по версии
  (`is_mariadb` в `inspect_connection`) и выбирает нужный билдер.

# Этапы

1. Sync-провайдер `mariadb` (dbapi20) + генерация маппинга; sync-сьют-сценарии
   на MariaDB.
2. Async-провайдер `mariadb_async` + `mariadb_pool.AsyncConnectionPool` +
   `MariadbAsyncOps`.
3. Тесты: `test_mariadb_sync.py` / `test_mariadb_async.py` (skipUnless docker
   MariaDB), сценарии как в test_async_api; тест отмены для пула (R2).
4. pyproject-extras, CI-сервис mariadb (опционально), документация в
   docs/async.md.

# Совместимость с MySQL

Проверено с обоими коннекторами: `mariadb` 2.0.0rc2 и `mysqlclient`
(MySQLdb) 2.3.0 (собран против libmariadb 3.4.10) / pymysql 1.2.0.
Провайдер `mysql` (легаси-путь) тоже сделан server-aware: в
`inspect_connection` он определяет MariaDB по строке версии, ставит
`is_mariadb` и переключает SQL-билдер на `MariaDBBuilder` — поэтому один и
тот же провайдер корректно работает и с MySQL, и с MariaDB (проверено:
полный сьют зелёный с обоими серверами на обоих провайдерах).

Коннектор `mariadb` работает и с MySQL-сервером (поле `server_mariadb`
различает их). Полный тест-сьют pony на **MySQL 8.4.11** — зелёный
(3871 тест, skipped=29; те же настройки сервера, что и для MariaDB, но без
`NO_AUTO_CREATE_USER` в `sql_mode`). Различия, которые провайдер учитывает
по факту сервера (`provider.is_mariadb`, определяется в `inspect_connection`):

- **JSON**: MariaDB не умеет `CAST(... AS JSON)` — сравнение через
  `JSON_EQUALS`, JSON-null текстовый, bool через `= 'true'`, `JSON_PARAM` —
  no-op. На MySQL используется исходный синтаксис MySQL-диалекта.
- **DELETE/UPDATE по читаемой таблице** (ошибка 1093): обёртка `EXISTS`
  в производную таблицу — только для настоящего MySQL (MariaDB справляется
  сама, а коррелированные производные таблицы ей недоступны без LATERAL).
- **Нарушение CHECK-констрейнта**: MySQL 8 отдаёт его как `DatabaseError`
  (3819), MariaDB — как `IntegrityError` (4025); провайдер нормализует
  3819 в `IntegrityError`, как ожидает pony.

# Тестовая среда (обязательные настройки сервера)

Полный тест-сьют pony на MariaDB требует:

- **`lower_case_table_names=1`** — pony нормализует имена таблиц в нижний
  регистр, а тесты пишут сырой SQL в CamelCase (`from Male`); на Linux
  по умолчанию регистрозависимо.
- **`sql_mode` с `ANSI_QUOTES`** — тесты используют `"` как кавычки
  идентификаторов (как в Postgres/sqlite).
- Оба параметра задаются при старте сервера (см. CI: docker run mariadb:11
  `--lower-case-table-names=1 --sql-mode=...ANSI_QUOTES`); менять их из
  провайдера нельзя — это сломало бы пользовательские строковые литералы.

# Подтверждённые ограничения

- **Ключи на BLOB/TEXT** (например `PrimaryKey(buffer)` без `max_len`) —
  MySQL/MariaDB требуют длину ключа; pony не генерирует префиксные ключи,
  так как это меняет семантику уникальности. Соответствующие тесты
  пропускаются для mysql/mariadb с пояснением.
- **Коррелированные derived-таблицы** — SQL вида
  `EXISTS (SELECT 1 FROM (SELECT ... WHERE outer.col = inner.col))`:
  MariaDB не поддерживает LATERAL, поэтому такие запросы невыразимы без
  де-корреляции в трансляторе; тесты пропускаются с пояснением.

# Готовность

Оба новых тест-модуля зелёные против живого MariaDB; старые сьюты зелёные
без правок; версия коннектора зафиксирована в спеке.

# Миграции, app и интроспекция

Поверх провайдера `mysql`/`mariadb` (dialect `MySQL`) работают миграции
(`pony-migrations`), app как логическая группировка (`pony-apps`, решение 8) и
интроспекция через `information_schema` (`pony-introspection`). В MariaDB нет
отдельного понятия схемы — app не создаёт базу данных, таблицы остаются в
текущей БД с обычными именами.
