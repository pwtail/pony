---
id: pony-dbproviders
type: module-design
title: Провайдеры баз данных Pony ORM
status: draft
tags:
  - postgres
  - psycopg
---

# Провайдеры баз данных Pony ORM (pony/orm/dbproviders)

Бэкенды, адаптирующие ядро ORM Pony к конкретным DB-API драйверам. По одному модулю на диалект,
каждый отдаёт `provider_cls`; `Database.bind(provider='<имя>', ...)` импортирует его по имени.

## Провайдер PostgreSQL (`postgres.py`)

- Работает на **psycopg 3** (`psycopg`, >= 3.1). Поддержка psycopg2 / psycopg2cffi удалена
  (2026-09); установка `psycopg` — обязательное требование провайдера `postgres`.
- Адаптация json на уровне драйвера работает с **сырым текстом**
  (`psycopg.types.json.set_json_loads(lambda x: x)`); парсинг и сериализация — за
  `JsonConverter` pony: json-значения пересекают границу драйвера строками.
- Классы ошибок драйвера берутся из `psycopg.errors` (те же имена, что в DBAPI-соглашении) в
  `wrap_dbapi_exceptions`; SQLSTATE читается через `exc.sqlstate` — ретрай сериализации на
  `40001`, переподключение на `57P01`.
- Остальное универсально и сохранилось с времён psycopg2 с точечными адаптациями:
  pyformat-параметры `%(name)s` словарём, переключение autocommit ↔ транзакция;
  `database` → `dbname` и `client_encoding` через conninfo при подключении;
  `server_version` из `connection.info`; `DISCARD ALL` при возврате соединения в пул
  выполняется с `prepare=False` и явной очисткой кеша prepared statements драйвера
  (`conn.prepared.clear()` / `conn._prepared.clear()` — psycopg3 кеширует prepared
  statements, а DISCARD сбрасывает их на сервере).

## Провайдер CockroachDB (`cockroach.py`)

- Наследуется от postgres-провайдера и разделяет привязку к psycopg3; локально переопределены
  только диалект-специфичные части (SQL-билдеры, конвертеры).
