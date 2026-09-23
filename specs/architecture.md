---
id: pony-async-architecture
type: architecture-design
title: Архитектура async-режима pony ORM
status: stale
parent: pony-async-goal-and-requirements
tags: [async, postgresql, psycopg3]
---

> **Статус: stale.** Это исходный план слоёв; актуальный механизм (состояние в
> `AbstractSessionCache`, логика в `SessionCacheGen`, драйверы `drive`/`Delegate`,
> ProviderOps) описан в `sync-async-shared-code.md`. Имена и номера строк ниже —
> историческое описание апстрима.

# Контекст (текущий код)

- Весь SQL сходится в `Database._exec_sql` (core.py:1394) и `_exec_raw_sql` (core.py:1298),
  которые вызывают `provider.execute` (dbapiprovider.py:367). Других путей к БД нет.
- `SessionCache` (core.py:2525) владеет соединением, identity map (`indexes`, `seeds`),
  очередью на сохранение и циклом flush/commit/rollback.
- Контекстное состояние живёт в `threading.local`: `Local` (core.py:532), `DbLocal`
  (core.py:2468), `Pool` (dbapiprovider.py:442). В asyncio один поток исполняет много задач —
  необходим перенос на contextvars.
- Провайдеры: базовый `DBAPIProvider` (dbapiprovider.py) + `dbproviders/postgres.py`
  (psycopg3, sync). Диалектный код — `PGColumn`, `PGSchema`, `PGTranslator`, `SQLBuilder`,
  конвертеры типов — от соединения не зависит.

# Слои

1. **Контекст задачи (contextvars).** `Local` / `DbLocal` / `Pool` переезжают с
   `threading.local` на ContextVar-обёртку с прежним интерфейсом. Поведение sync не меняется;
   соединение привязывается к задаче, а не к потоку. Обязательное условие для всего async.
2. **Async-провайдер PostgreSQL.** Зеркало `PostgreSQLProvider` на
   `psycopg.AsyncConnection` / `AsyncCursor`: await-варианты
   connect / set_transaction_mode / commit / rollback / release / execute.
   Диалектный код, `should_reconnect`, конвертеры — общие с sync.
   Пул: аналог текущего `Pool` с ленивым соединением на задачу либо
   `psycopg_pool.AsyncConnectionPool` (решено именно так — реализовано и проверено).
3. **Async-ядро сессии.** Async-варианты `_exec_sql` / `_exec_raw_sql`,
   `SessionCache.connect` / `commit` / `rollback` / `flush` (цепочки
   `_before_save_` / `_save_` / m2m-синхронизации становятся await-цепочками).
   Инвариант N3: сессия принадлежит одной задаче.
4. **Async API и явная загрузка.** `db_session.__aenter__` / `__aexit__` (решение 4);
   `Query` / `QueryResult`: `__await__`, `__aiter__` / `__anext__`; `Entity.load (async-ветка)`;
   `SetInstance.__await__` / `__aiter__`. Guards `NotLoadedError` в точках неявного I/O:
   `Attribute.get` (core.py:3378), seed-догрузка (core.py:3384),
   `__set__` reverse-атрибутов (core.py:3410), методы `SetInstance`
   (len / iter / getitem / contains / bool / copy / eq / count / is_empty).
5. **Запрет sync-драйвера в async-режиме** — структурный: async-провайдер не имеет
   sync-методов, а sync-соединение в async-сессии не создаётся. Дополнительно входной guard
   (решение 5): sync-`db_session.__enter__` при активном event loop бросает ошибку —
   это единственная точка, где sync-режим «видит» asyncio.

# Инварианты

- **I1.** В async-сессии невозможен ни один блокирующий вызов: все пути к БД — только await.
- **I2.** Машинерия identity map / undo / prefetch / flush — общая для sync и async;
  async-отличия только в точках I/O (слой 3).
- **I3.** Загруженное читается без await; незагруженное — только `NotLoadedError`.
- **I4.** Sync-режим после всех изменений неотличим от текущего поведения
  (единственное видимое отличие — ошибка при входе внутри корутины, решение 5).

# Этапы реализации (порядок)

1. contextvars (прозрачно для sync; sync-сьют зелёный).
2. Async-провайдер psycopg3.
3. Async-ядро сессии (exec / commit / flush).
4. Async API + guards явной загрузки (`NotLoadedError`) + входной guard sync-сессии.
5. Async-тесты (интерлив задач, отмена корутины, одна сессия = одна задача), документация,
   CI на Postgres.
