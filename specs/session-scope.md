---
id: pony-session-scope
type: architecture-design
title: Модель сессии: глобальный db_session и per-database скоупы
status: active
parent: pony-async-goal-and-requirements
references: [pony-async-shared-code]
tags: [session, db_session, per-database, async, sync]
---

# Цель

Зафиксировать модель сессий форка. Глобальный `db_session` остаётся прежним —
одна транзакция, которая может обнимать несколько `Database`. Объект `Database`
дополнительно получает собственный **per-database скоуп**: `with db:` /
`async with db:` и `db.session(...)`, ограниченный одной базой.

# Решения

1. **`db_session` — глобальный, без изменений.** `with db_session:` /
   `async with db_session:` открывает транзакцию на все базы, к которым обращаются
   внутри блока (координация primary/остальные кэши, `PartialCommitException` /
   `CommitException` — как сейчас).
2. **`Database` — per-database скоуп.** `with db:` ≡ `with db.session():`,
   `async with db:` ≡ `async with db.session():`; `db.session(*args, **kw)`
   принимает те же флаги, что `db_session(*args, **kw)`, но скоуп **привязан к
   этой базе**: на входе фиксируется `db`, на выходе коммитится/откатывается
   **только её кэш**.
3. **Чужая база внутри per-db скоупа — ошибка.** Обращение к другому `Database`
   внутри `with db:` / `async with db:` бросает `TransactionError` (guard в
   `Database._get_cache`). Иначе кэш чужой базы некому закоммитить — останется
   зависший незакоммиченный кэш.
4. **Вложенность.**
   - per-db внутри per-db (та же база) — можно;
   - per-db внутри per-db (другая база) — `TransactionError` (следствие решения 3);
   - per-db внутри глобального `db_session` — можно: per-db скоуп сам не коммитит,
     commit/rollback происходит на выходе из внешнего глобального скоупа (одна
     транзакция на весь `db_session`);
   - глобальный `db_session` внутри per-db — **no-op**: внутренний глобальный
     скоуп прозрачен (вложенность учитывается, коммита на выходе нет — счётчик
     не доходит до нуля), граница per-db не расширяется — guard чужой базы
     продолжает действовать.

# Механизм

- В `local` (ContextLocal) появляется `scoped_db` — база активного per-db скоупа
  (плюс счётчик глубины для вложенных скоупов той же базы); `db_session` это поле
  не трогает.
- Per-db скоуп — тот же `DBSessionContextManager` с параметром `database`
  (`None` — глобальный `db_session`):
  - вход — проверки вложенности (решение 4), регистрация `db` в `local.scoped_db`,
    открытие скоупа (инкремент `db_context_counter`, чтобы `_get_cache` разрешал
    создание кэша);
  - выход — только сброс `local.scoped_db` (по счётчику глубины). Сам
    commit/rollback/release — существующий путь `db_session` (`_commit_or_rollback`
    / `_async_commit_or_rollback`), он срабатывает на выходе из внешнего скоупа
    (где `db_context_counter == 0`) и обрабатывает все кэши. Поскольку guard ниже
    гарантирует внутри per-db скоупа максимум один кэш, отдельной per-db логики
    commit не нужно.
- Guard в `Database._get_cache()`: если `local.scoped_db` установлен и
  `self is not local.scoped_db` — `TransactionError` с подсказкой (использовать
  `with <эта база>` или глобальный `db_session`).

# Инварианты

- **I1.** Внутри per-db скоупа новые кэши создаются только для `scoped_db`; кэши
  других баз, созданные внешним глобальным скоупом, не коммитятся и не удаляются
  per-db скоупом.
- **I2.** `db_session` поведенчески не меняется: мультибазовые транзакции,
  `PartialCommitException`, порядок commit — как до изменения.
- **I3.** Флаги (`retry / immediate / serializable / optimistic / ddl / strict /
  allowed_exceptions / retry_exceptions / sql_debug / show_values`) сохраняют смысл
  в обоих скоупах; `retry` по-прежнему нельзя использовать как context manager.
- **I4.** Sync/async-симметрия: `with db:` и `async with db:` проходят одни и те же
  guard'ы; запрет смешения режимов (sync внутри async-сессии) действует и для
  per-db скоупа.

# Риски

- **R1. Глобалы `commit()`/`rollback()`/`flush()`** итерируют `_get_caches()` —
  внутри per-db скоупа это максимум один кэш (I1), поэтому их вызов внутри
  per-db скоупа корректен и затрагивает только эту базу. Явный per-db commit —
  это `db.commit()`.
- **R2. Декораторы и генераторы.** `@db.session` и `@db_session` на функциях/
  генераторах: per-db версия проводит тот же guard и коммитит только свою базу;
  генераторная обёртка манипулирует `local.db2cache` вручную и должна учитывать
  `scoped_db`.
- **R3. Интерактивный режим** (`pony.MODE == "INTERACTIVE"`): guard не должен
  ломать неявный доступ вне скоупа.

# Вне области

- Семантика глобального `db_session` (мультибазовые транзакции) не меняется.
- Отдельного per-db пула/соединения не добавляется — используется тот же
  async-механизм, что у `db_session`.
