"""Интроспекция схемы БД (этап 2 миграций pony).

- `db.introspect()` — достраивает объявленные entity-классы атрибутами,
  выводимыми из каталога БД, и генерирует маппинг; после вызова работают
  обычные `db_session` и ORM-запросы.
- `db.introspect('Person', 'Book')` — отладочный режим: возвращает строку
  с описаниями указанных сущностей, выведенными из схемы БД (классы при
  этом не трогает).

Диалекты: PostgreSQL (каталог pg_catalog), MySQL/MariaDB (information_schema),
SQLite (PRAGMA). Sync.
"""

from collections import namedtuple
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from pony.orm import core
from pony.orm.core import Database, Optional, PrimaryKey, Required, Set
from pony.orm.ormtypes import FloatArray, IntArray, Json, StrArray


class IntrospectionError(core.OrmError):
    pass


class _DumpResult(str):
    # в консоли/ноутбуке показывается как код, а не как repr строки
    def __repr__(self):
        return str(self)

    def _repr_pretty_(self, p, cycle):
        p.text(str(self))


TYPE_PY = {
    "int": int,
    "bool": bool,
    "str": str,
    "float": float,
    "Decimal": Decimal,
    "date": date,
    "time": time,
    "datetime": datetime,
    "timedelta": timedelta,
    "UUID": UUID,
    "bytes": bytes,
    "Json": Json,
    "IntArray": IntArray,
    "StrArray": StrArray,
    "FloatArray": FloatArray,
}

PG_TYPE_MAP = {
    "int2": "int",
    "int4": "int",
    "int8": "int",
    "bool": "bool",
    "char": "str",
    "bpchar": "str",
    "varchar": "str",
    "text": "str",
    "name": "str",
    "float4": "float",
    "float8": "float",
    "numeric": "Decimal",
    "date": "date",
    "time": "time",
    "timetz": "time",
    "timestamp": "datetime",
    "timestamptz": "datetime",
    "interval": "timedelta",
    "uuid": "UUID",
    "bytea": "bytes",
    "json": "Json",
    "jsonb": "Json",
    "_int2": "IntArray",
    "_int4": "IntArray",
    "_int8": "IntArray",
    "_text": "StrArray",
    "_varchar": "StrArray",
    "_bpchar": "StrArray",
    "_float4": "FloatArray",
    "_float8": "FloatArray",
}

MYSQL_TYPE_MAP = {
    "tinyint": "int",
    "smallint": "int",
    "mediumint": "int",
    "int": "int",
    "integer": "int",
    "bigint": "int",
    "year": "int",
    "bit": "int",
    "decimal": "Decimal",
    "numeric": "Decimal",
    "float": "float",
    "double": "float",
    "real": "float",
    "char": "str",
    "varchar": "str",
    "text": "str",
    "tinytext": "str",
    "mediumtext": "str",
    "longtext": "str",
    "enum": "str",
    "set": "str",
    "date": "date",
    "time": "time",
    "datetime": "datetime",
    "timestamp": "datetime",
    "json": "Json",
    "binary": "bytes",
    "varbinary": "bytes",
    "blob": "bytes",
    "tinyblob": "bytes",
    "mediumblob": "bytes",
    "longblob": "bytes",
}

SQLITE_TYPE_MAP = {
    "INT": "int",
    "INTEGER": "int",
    "BIGINT": "int",
    "SMALLINT": "int",
    "TINYINT": "int",
    "MEDIUMINT": "int",
    "BOOL": "bool",
    "BOOLEAN": "bool",
    "VARCHAR": "str",
    "CHAR": "str",
    "TEXT": "str",
    "CLOB": "str",
    "NCHAR": "str",
    "NVARCHAR": "str",
    "REAL": "float",
    "FLOAT": "float",
    "DOUBLE": "float",
    "DECIMAL": "Decimal",
    "NUMERIC": "Decimal",
    "DATE": "date",
    "TIME": "time",
    "DATETIME": "datetime",
    "TIMESTAMP": "datetime",
    "BLOB": "bytes",
    "JSON": "Json",
}

FKInfo = namedtuple("FKInfo", "col pschema ptable ptype deltype ncols")


def _q(db, sql, params):
    return db.select(sql, params)


def _qualified_name(table_name):
    if isinstance(table_name, str):
        return table_name
    return "%s.%s" % table_name


def _type_name(db, typname, typtype=None):
    """Имя python-типа для колонки; None — неизвестный тип."""
    dialect = db.provider.dialect
    if dialect == "PostgreSQL":
        mapped = PG_TYPE_MAP.get(typname)
        if mapped is not None:
            return mapped
        if typtype == "e":  # user-defined enum
            return "str"
        return None
    if dialect == "MySQL":
        if typtype == "bool":  # tinyint(1)
            return "bool"
        return MYSQL_TYPE_MAP.get(typname)
    return SQLITE_TYPE_MAP.get(typname)  # SQLite


def _py_type_from_sql(db, typname, typtype=None):
    name = _type_name(db, typname, typtype)
    return TYPE_PY.get(name, int) if name else int


def _table_exists(db, table_name):
    dialect = db.provider.dialect
    if dialect == "PostgreSQL":
        rows = _q(db, "SELECT to_regclass($q)", {"q": _qualified_name(table_name)})
        return bool(rows and rows[0] is not None)
    if dialect == "MySQL":
        schema_name, base_name = db.provider.split_table_name(table_name)
        rows = _q(
            db,
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = $s AND LOWER(table_name) = LOWER($t)",
            {"s": schema_name, "t": base_name},
        )
        return bool(rows and rows[0])
    rows = _q(
        db,
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name = $t COLLATE NOCASE",
        {"t": table_name},
    )
    return bool(rows)


def table_info(db, table_name):
    """Читает каталог для одной таблицы: колонки, PK, unique, индексы, FK.
    Возвращает единый словарь для всех диалектов."""
    dialect = db.provider.dialect
    if dialect == "PostgreSQL":
        return _table_info_postgres(db, table_name)
    if dialect == "MySQL":
        return _table_info_mysql(db, table_name)
    return _table_info_sqlite(db, table_name)


def _table_info_postgres(db, table_name):
    """Каталог PostgreSQL (pg_catalog) для одной таблицы."""
    qname = _qualified_name(table_name)
    rows = _q(
        db,
        "SELECT a.attname AS name, t.typname AS type, t.typtype AS typtype, "
        "a.attnotnull AS notnull, a.attidentity AS identity, "
        "pg_get_expr(d.adbin, d.adrelid) AS default_expr "
        "FROM pg_attribute a "
        "JOIN pg_type t ON t.oid = a.atttypid "
        "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        "WHERE a.attrelid = to_regclass($q) AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum",
        {"q": qname},
    )
    columns = {}
    column_list = []
    for row in rows:
        default_expr = row.default_expr
        if default_expr and "::" in default_expr:
            # pg_get_expr добавляет каст литералов: "'new'::text" → "'new'"
            default_expr = default_expr.rsplit("::", 1)[0]
        info = {
            "type": row.type,
            "typtype": row.typtype,
            "notnull": row.notnull,
            "identity": row.identity,
            "default": default_expr,
        }
        columns[row.name] = info
        column_list.append((row.name, info))

    pk = _q(
        db,
        "SELECT a.attname AS name FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = to_regclass($q) AND i.indisprimary "
        "ORDER BY array_position(i.indkey, a.attnum)",
        {"q": qname},
    )
    pk_name_rows = _q(
        db,
        "SELECT conname AS name FROM pg_constraint "
        "WHERE conrelid = to_regclass($q) AND contype = 'p'",
        {"q": qname},
    )
    pk_name = pk_name_rows[0] if pk_name_rows else None

    uniques = {}
    for row in _q(
        db,
        "SELECT a.attname AS col, COALESCE(c.conname, ic.relname) AS idxname "
        "FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "JOIN pg_class ic ON ic.oid = i.indexrelid "
        "LEFT JOIN pg_constraint c ON c.conindid = i.indexrelid "
        "WHERE i.indrelid = to_regclass($q) AND i.indisunique "
        "AND NOT i.indisprimary AND cardinality(i.indkey) = 1",
        {"q": qname},
    ):
        uniques[row.col] = row.idxname

    indexes = {}
    for row in _q(
        db,
        "SELECT a.attname AS col, ic.relname AS idxname "
        "FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "JOIN pg_class ic ON ic.oid = i.indexrelid "
        "WHERE i.indrelid = to_regclass($q) AND NOT i.indisunique "
        "AND NOT i.indisprimary AND cardinality(i.indkey) = 1",
        {"q": qname},
    ):
        indexes[row.col] = row.idxname

    fks = {}
    for row in _q(
        db,
        "SELECT a.attname AS col, ns2.nspname AS pschema, cl2.relname AS ptable, "
        "pt.typname AS ptype, con.confdeltype AS deltype, "
        "cardinality(con.conkey) AS ncols "
        "FROM pg_constraint con "
        "JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = ANY(con.conkey) "
        "JOIN pg_class cl2 ON cl2.oid = con.confrelid "
        "JOIN pg_namespace ns2 ON ns2.oid = cl2.relnamespace "
        "JOIN pg_attribute pa ON pa.attrelid = con.confrelid "
        "AND pa.attnum = ANY(con.confkey) "
        "JOIN pg_type pt ON pt.oid = pa.atttypid "
        "WHERE con.contype = 'f' AND con.conrelid = to_regclass($q) "
        "ORDER BY a.attname",
        {"q": qname},
    ):
        fks[row.col] = FKInfo(
            row.col, row.pschema, row.ptable, row.ptype, row.deltype, row.ncols
        )

    return {
        "columns": columns,
        "column_list": column_list,
        "pk": list(pk),
        "pk_name": pk_name,
        "uniques": uniques,
        "indexes": indexes,
        "fks": fks,
    }


def _table_info_mysql(db, table_name):
    """Каталог MySQL/MariaDB (information_schema) для одной таблицы."""
    schema_name, base_name = db.provider.split_table_name(table_name)
    normalize_name = db.provider.normalize_name
    rows = _q(
        db,
        "SELECT COLUMN_NAME AS name, DATA_TYPE AS type, COLUMN_TYPE AS coltype, "
        "IS_NULLABLE AS nullable, COLUMN_KEY AS colkey, EXTRA AS extra, "
        "COLUMN_DEFAULT AS default_expr "
        "FROM information_schema.columns "
        "WHERE TABLE_SCHEMA = $s AND TABLE_NAME = $t "
        "ORDER BY ORDINAL_POSITION",
        {"s": schema_name, "t": base_name},
    )
    columns = {}
    column_list = []
    for row in rows:
        is_bool = row.type == "tinyint" and row.coltype == "tinyint(1)"
        auto = "auto_increment" in (row.extra or "")
        default_expr = row.default_expr
        name = normalize_name(row.name)
        info = {
            "type": row.type,
            "typtype": "bool" if is_bool else None,
            "notnull": row.nullable == "NO",
            "identity": None,
            "auto": auto,
            "default": default_expr,
        }
        columns[name] = info
        column_list.append((name, info))

    pk_rows = _q(
        db,
        "SELECT COLUMN_NAME AS name FROM information_schema.columns "
        "WHERE TABLE_SCHEMA = $s AND TABLE_NAME = $t AND COLUMN_KEY = 'PRI' "
        "ORDER BY ORDINAL_POSITION",
        {"s": schema_name, "t": base_name},
    )
    pk = [normalize_name(row.name) for row in pk_rows]
    constraint_rows = _q(
        db,
        "SELECT CONSTRAINT_NAME AS name FROM information_schema.table_constraints "
        "WHERE TABLE_SCHEMA = $s AND TABLE_NAME = $t AND CONSTRAINT_TYPE = 'PRIMARY KEY'",
        {"s": schema_name, "t": base_name},
    )
    pk_name = constraint_rows[0] if constraint_rows else None

    uniques = {}
    indexes = {}
    for row in _q(
        db,
        "SELECT COLUMN_NAME AS col, INDEX_NAME AS idxname, NON_UNIQUE AS nonunique, "
        "COUNT(*) OVER (PARTITION BY INDEX_NAME) AS ncols "
        "FROM information_schema.statistics "
        "WHERE TABLE_SCHEMA = $s AND TABLE_NAME = $t AND INDEX_NAME != 'PRIMARY'",
        {"s": schema_name, "t": base_name},
    ):
        col = normalize_name(row.col)
        if row.ncols != 1:
            continue
        if row.nonunique == 0:
            uniques[col] = row.idxname
        else:
            indexes[col] = row.idxname

    fks = {}
    for row in _q(
        db,
        "SELECT kcu.COLUMN_NAME AS col, kcu.REFERENCED_TABLE_SCHEMA AS pschema, "
        "kcu.REFERENCED_TABLE_NAME AS ptable, pc.DATA_TYPE AS ptype, "
        "rc.DELETE_RULE AS deltype, "
        "COUNT(*) OVER (PARTITION BY kcu.CONSTRAINT_NAME) AS ncols "
        "FROM information_schema.key_column_usage kcu "
        "JOIN information_schema.referential_constraints rc "
        "  ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA "
        " AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
        "JOIN information_schema.columns pc "
        "  ON pc.TABLE_SCHEMA = kcu.REFERENCED_TABLE_SCHEMA "
        " AND pc.TABLE_NAME = kcu.REFERENCED_TABLE_NAME "
        " AND pc.COLUMN_NAME = kcu.REFERENCED_COLUMN_NAME "
        "WHERE kcu.TABLE_SCHEMA = $s AND kcu.TABLE_NAME = $t "
        "AND kcu.REFERENCED_TABLE_NAME IS NOT NULL",
        {"s": schema_name, "t": base_name},
    ):
        col = normalize_name(row.col)
        fks[col] = FKInfo(
            col, row.pschema, row.ptable, row.ptype, row.deltype, row.ncols
        )

    return {
        "columns": columns,
        "column_list": column_list,
        "pk": pk,
        "pk_name": pk_name,
        "uniques": uniques,
        "indexes": indexes,
        "fks": fks,
    }


def _table_info_sqlite(db, table_name):
    """Каталог SQLite (pragma_* table-valued functions) для одной таблицы."""
    rows = _q(db, "SELECT * FROM pragma_table_info($t)", {"t": table_name})
    columns = {}
    column_list = []
    for row in rows:
        raw_type = (row.type or "").split("(", 1)[0].strip().upper()
        info = {
            "type": raw_type,
            "typtype": None,
            "notnull": bool(row.notnull),
            "identity": None,
            "auto": False,
            "default": row.dflt_value,
            "pk": row.pk,
        }
        columns[row.name] = info
        column_list.append((row.name, info))

    pk = [name for name, info in column_list if info["pk"]]
    # INTEGER PRIMARY KEY (одна колонка) — rowid-алиас, значения генерируются
    if len(pk) == 1 and columns[pk[0]]["type"] == "INTEGER":
        columns[pk[0]]["auto"] = True

    uniques = {}
    indexes = {}
    for idx in _q(db, "SELECT * FROM pragma_index_list($t)", {"t": table_name}):
        if idx.origin == "pk":
            continue
        cols = _q(db, "SELECT * FROM pragma_index_info($n)", {"n": idx.name})
        col_names = [c.name for c in cols]
        if len(col_names) != 1:
            continue
        col = col_names[0]
        if idx.unique:
            uniques[col] = idx.name
        else:
            indexes[col] = idx.name

    fks = {}
    fk_rows = _q(db, "SELECT * FROM pragma_foreign_key_list($t)", {"t": table_name})
    fk_counts = {}
    for row in fk_rows:
        fk_counts[row.id] = fk_counts.get(row.id, 0) + 1
    for row in fk_rows:
        ncols = fk_counts[row.id]
        col = getattr(row, "from")
        fks[col] = FKInfo(
            col,
            None,
            row.table,
            columns.get(col, {}).get("type", "INTEGER"),
            row.on_delete,
            ncols,
        )

    return {
        "columns": columns,
        "column_list": column_list,
        "pk": pk,
        "pk_name": None,
        "uniques": uniques,
        "indexes": indexes,
        "fks": fks,
    }


def _entity_table_name(db, entity):
    root = entity._root_
    if root is not entity:
        entity = root
    table_name = entity._table_
    if table_name is None:
        table_name = db.provider.get_default_entity_table_name(entity)
    app = getattr(entity, "_app_", None)
    if (
        app is not None
        and db.provider.dialect == "PostgreSQL"
        and isinstance(table_name, str)
    ):
        table_name = (app.schema_name, table_name)
    return table_name


def _create_referenced_entities(db):
    """Создаёт entity-классы для упомянутых в декларациях, но не объявленных
    сущностей, если в схеме есть таблица по конвенции имени; в том же
    application, что и ссылающаяся сущность."""
    missing = []
    seen = set()
    for entity in list(db.entities.values()):
        if entity._root_ is not entity:
            continue
        for attr in entity._new_attrs_:
            py_type = attr.py_type
            if isinstance(py_type, str) and py_type not in db.entities:
                app = getattr(entity, "_app_", None)
                key = (py_type, app)
                if key not in seen:
                    seen.add(key)
                    missing.append(key)
    for name, app in missing:
        table_name = db.provider.normalize_name(name)
        if app is not None and db.provider.dialect == "PostgreSQL":
            table_name = (app.schema_name, table_name)
        if not _table_exists(db, table_name):
            raise IntrospectionError(
                "Entity %s is referenced by the model but not declared, "
                "and table %r does not exist" % (name, table_name)
            )
        base = app.Entity if app is not None else db.Entity
        core.EntityMeta(name, (base,), {})


def _add_attr(entity, name, attr):
    existing = entity._adict_.get(name)
    if existing is not None and existing.is_implicit:
        # неявный атрибут (например, авто-id) замещаем выведенным из схемы
        entity._attrs_.remove(existing)
        entity._new_attrs_.remove(existing)
        del entity._adict_[name]
        for index in entity._indexes_:
            if existing in index.attrs:
                index.attrs = tuple(
                    attr if a is not existing else attr
                    for a in index.attrs
                )
        if len(entity._pk_attrs_) == 1 and entity._pk_attrs_[0] is existing:
            entity._pk_attrs_ = (attr,)
            entity._pk_ = attr
    elif name in entity._adict_ or hasattr(entity, name):
        raise IntrospectionError(
            "Cannot create attribute %s.%s: the name is already in use"
            % (entity.__name__, name)
        )
    attr._init_(entity, name)
    entity._attrs_.append(attr)
    entity._new_attrs_.append(attr)
    entity._adict_[name] = attr
    setattr(entity, name, attr)
    return attr


def _attr_name_for_fk(column):
    if column.endswith("_id"):
        return column[:-3]
    return column


def introspect(db, *entities):
    """db.introspect(*entities): без имён заполняет классы из схемы БД и
    строит маппинг; с именами — возвращает описания указанных сущностей."""
    if db.provider is None:
        raise IntrospectionError("Database object is not bound with a provider yet")
    if db.provider.dialect not in ("PostgreSQL", "MySQL", "SQLite"):
        raise IntrospectionError(
            "Introspection supports PostgreSQL, MySQL/MariaDB and SQLite "
            "(got %s)" % db.provider.dialect
        )
    if db.schema is not None:
        raise IntrospectionError("Mapping was already generated")
    if entities:
        with core.db_session:
            return _dump_declarations(db, entities)
    if not db._pending_entities and not any(
        entity._root_ is entity for entity in db.entities.values()
    ):
        raise IntrospectionError(
            "No entities are declared: declare the entity classes you need "
            "(even empty ones) before db.introspect()"
        )
    # интроспекция выполняется: флаг снимается, ленивый триггер больше
    # не срабатывает (в т.ч. на внутренних запросах)
    db._is_empty = False
    with core.db_session:
        _create_pending_entities(db)
        _create_referenced_entities(db)
        entities = [
            entity
            for entity in sorted(db.entities.values(), key=lambda e: e._id_)
            if entity._root_ is entity
        ]
        table_map = {}
        for entity in entities:
            table_name = _entity_table_name(db, entity)
            qname = _qualified_name(table_name)
            table_map[qname] = entity
            # case-insensitive fallback: имена таблиц в SQLite/MySQL регистронезависимы
            table_map.setdefault(qname.lower(), entity)
        for entity in entities:
            table_name = _entity_table_name(db, entity)
            info = table_info(db, table_name)
            _fill_entity(db, entity, table_name, table_map, info)
    db.generate_mapping(check_tables=False)


def _fill_entity(db, entity, table_name, table_map, info):
    columns = info["columns"]
    normalize_name = db.provider.normalize_name
    covered = set()
    for attr in entity._new_attrs_:
        if attr.is_collection:
            continue
        if attr.is_implicit:
            continue  # неявные атрибуты (авто-id) не считаются контрактом
        attr_columns = attr.columns if attr.columns else [attr.name]
        for column in attr_columns:
            if normalize_name(column) not in columns:
                raise IntrospectionError(
                    "Declared attribute %s column %r does not exist in table %s "
                    "(declare the actual column via column=...)"
                    % (attr, column, table_name)
                )
            covered.add(normalize_name(column))

    pk = info["pk"]
    if len(pk) > 1 and not any(
        attr.is_pk for attr in entity._new_attrs_ if not attr.is_collection
    ):
        raise IntrospectionError(
            "Composite primary key of table %s must be declared explicitly: "
            "PrimaryKey(%s, name=...)" % (table_name, ", ".join(pk))
        )

    for name, cinfo in info["column_list"]:
        if name in covered:
            continue
        type_name = _type_name(db, cinfo["type"], cinfo.get("typtype"))
        py_type = TYPE_PY.get(type_name) if type_name else None
        notnull = bool(cinfo["notnull"])
        is_pk = name in pk
        fk = info["fks"].get(name)

        if fk is not None and fk.ncols == 1 and not is_pk:
            parent_key = "%s.%s" % (fk.pschema, fk.ptable)
            parent_entity = (
                table_map.get(parent_key)
                or table_map.get(fk.ptable)
                or table_map.get((parent_key or "").lower())
                or table_map.get((fk.ptable or "").lower())
            )
            attr_name = _attr_name_for_fk(name)
            kwargs = {}
            if normalize_name(attr_name) != normalize_name(name):
                kwargs["column"] = name
            if parent_entity is not None:
                attr = (Required if notnull else Optional)(
                    parent_entity, **kwargs
                )
                if not notnull:
                    attr.nullable = True
                _add_attr(entity, attr_name, attr)
                continue
            if py_type is None:
                py_type = _py_type_from_sql(db, fk.ptype)
            attr = (Required if notnull else Optional)(py_type, **kwargs)
            if not notnull:
                attr.nullable = True
            _add_attr(entity, attr_name, attr)
            continue

        if py_type is None:
            py_type = str  # неизвестный тип: базовый str (best effort)

        if is_pk and len(pk) == 1:
            kwargs = {"name": info["pk_name"]}
            if cinfo.get("auto"):
                kwargs["auto"] = True
            elif cinfo["identity"] in ("a", "d"):
                kwargs["auto"] = "identity"
            elif cinfo["default"] and cinfo["default"].startswith("nextval("):
                kwargs["auto"] = True
            attr = PrimaryKey(py_type, **kwargs)
        else:
            if notnull:
                attr = Required(py_type)
            else:
                attr = Optional(py_type, nullable=True)

        if name in info["uniques"]:
            attr.is_unique = info["uniques"][name] or True
        if name in info["indexes"]:
            attr.index = info["indexes"][name] or True
        if cinfo["default"] and not is_pk:
            attr.sql_default = cinfo["default"]
        _add_attr(entity, name, attr)


def _entity_name(table):
    return "".join(part.capitalize() for part in table.split("_"))


def _list_tables(db):
    """Список таблиц для debug-дампа: [(schema_or_None, table), ...]."""
    dialect = db.provider.dialect
    if dialect == "PostgreSQL":
        schemas = sorted({app.schema_name for app in db._apps.values()})
        rows = _q(
            db,
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE schemaname = current_schema() "
            "OR schemaname = ANY($schemas) "
            "ORDER BY schemaname, tablename",
            {"schemas": schemas},
        )
        return [(row.schemaname, row.tablename) for row in rows]
    if dialect == "MySQL":
        rows = _q(
            db,
            "SELECT TABLE_NAME AS name FROM information_schema.tables "
            "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME",
            {},
        )
        return [(None, row.name) for row in rows]
    rows = _q(
        db,
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
        {},
    )
    return [(None, row) for row in rows]


def _declared_entities(db, tables):
    declared = {}
    for entity in db.entities.values():
        if entity._root_ is not entity:
            continue
        table_name = entity._table_
        if isinstance(table_name, tuple):
            key = (table_name[0], table_name[1])
            if key in tables:
                declared[key] = entity
            continue
        if not isinstance(table_name, str):
            table_name = db.provider.get_default_entity_table_name(entity)
        for schema, table in tables:
            if table.lower() == table_name.lower():
                declared[(schema, table)] = entity
                break
    return declared


def _declared_sets(children, declared):
    result = {}
    for parent_key, entity in declared.items():
        for attr in entity._new_attrs_:
            if not attr.is_collection:
                continue
            target = attr.py_type
            target_name = target.__name__ if isinstance(target, type) else str(target)
            for child_key, fk_col in children.get(parent_key, []):
                child = declared.get(child_key)
                if (
                    _entity_name(child_key[1]) == target_name
                    or (child is not None and child.__name__ == target_name)
                ):
                    result[(child_key, fk_col)] = attr.name
    return result


def _declared_attr_name(entity, column):
    if entity is None:
        return None
    for attr in entity._new_attrs_:
        if attr.is_collection:
            continue
        columns = attr.columns if attr.columns else [attr.name]
        if any(c.lower() == column.lower() for c in columns):
            return attr.name
    return None


def _resolve_names(db, tables, names):
    by_name = {}
    for schema, table in tables:
        by_name.setdefault(_entity_name(table).lower(), []).append((schema, table))

    def catalog_key(table_name):
        if isinstance(table_name, tuple):
            schema, table = table_name
            for key in tables:
                if key == (schema, table):
                    return key
        else:
            for key in tables:
                if key[1].lower() == table_name.lower():
                    return key
        return None

    def entity_key(entity):
        key = catalog_key(_entity_table_name(db, entity))
        if key is None:
            raise IntrospectionError(
                "Entity %s is not mapped to a table" % entity.__name__
            )
        return key

    resolved = []
    for ref in names:
        if isinstance(ref, core.EntityMeta):
            resolved.append((ref.__name__, entity_key(ref)))
            continue
        if not isinstance(ref, str):
            raise IntrospectionError(
                "Entity reference must be a string or an entity class, got %r"
                % (ref,)
            )
        if "." in ref:
            app_name, entity_name = ref.split(".", 1)
            app = db._apps.get(app_name)
            if app is None:
                raise IntrospectionError(
                    "Unknown application %r in entity reference %r"
                    % (app_name, ref)
                )
            entity = db.entities.get(entity_name)
            if entity is not None and getattr(entity, "_app_", None) is app:
                resolved.append((entity_name, entity_key(entity)))
                continue
            table_name = db.provider.normalize_name(entity_name)
            if db.provider.dialect == "PostgreSQL":
                table_name = (app.schema_name, table_name)
            key = catalog_key(table_name)
            if key is None:
                raise IntrospectionError(
                    "Unknown entity %r: table %r does not exist"
                    % (ref, _qualified_name(table_name))
                )
            resolved.append((entity_name, key))
            continue
        entity = db.entities.get(ref)
        if entity is not None:
            resolved.append((ref, entity_key(entity)))
            continue
        matched = by_name.get(ref.lower())
        if not matched:
            available = sorted(
                set(db.entities) | {_entity_name(table) for _, table in tables}
            )
            raise IntrospectionError(
                "Unknown entity %r; expected one of: %s"
                % (ref, ", ".join(available))
            )
        for key in matched:
            item = (ref, key)
            if item not in resolved:
                resolved.append(item)
    return resolved


def _create_pending_entities(db):
    for name, key in db._pending_entities:
        if name in db.entities:
            continue
        schema, table = key
        attrs = {}
        default = db.provider.normalize_name(name)
        if schema and schema != "public":
            attrs["_table_"] = (schema, table)
        elif default != table:
            attrs["_table_"] = table
        type(name, (db.Entity,), attrs)


def _dump_declarations(db, names=()):
    tables = _list_tables(db)
    resolved = _resolve_names(db, tables, names) if names else []
    if db._is_empty:
        db._pending_entities = list(resolved)
    infos = {}
    children = {}
    for schema, table in tables:
        key = (schema, table)
        table_name = key if schema else table
        infos[key] = table_info(db, table_name)
    for key, info in infos.items():
        for col, fk in info["fks"].items():
            if fk.ncols != 1:
                continue
            parent_key = (fk.pschema, fk.ptable)
            children.setdefault(parent_key, []).append((key, col))
    declared = _declared_entities(db, tables)
    declared_sets = _declared_sets(children, declared)
    if names:
        selected = list(dict.fromkeys(key for _name, key in resolved))
    else:
        selected = tables
    lines = ["from pony.orm import *", "", ""]
    for schema, table in selected:
        key = (schema, table)
        lines.extend(
            _dump_entity(
                db, key, infos[key], children.get(key, []), declared, declared_sets
            )
        )
    return _DumpResult("\n".join(lines))


def _dump_entity(db, key, info, child_list, declared=None, declared_sets=None):
    schema, table = key
    declared = declared or {}
    declared_sets = declared_sets or {}
    entity = declared.get(key)
    lines = []
    cls = entity.__name__ if entity is not None else _entity_name(table)
    lines.append("class %s(db.Entity):" % cls)
    if schema and schema != "public":
        lines.append("    _table_ = (%r, %r)" % (schema, table))
    elif table != table.lower():
        lines.append("    _table_ = %r" % table)
    pk = info["pk"]
    fk_cols = {col: row for col, row in info["fks"].items() if row.ncols == 1}
    for name, cinfo in info["column_list"]:
        fk = fk_cols.get(name)
        if fk is not None and name not in pk:
            attr_name = _declared_attr_name(
                declared.get(key), name
            ) or _attr_name_for_fk(name)
            parent = _entity_name(fk.ptable)
            kind = "Optional" if not cinfo["notnull"] else "Required"
            args = ["%r" % parent]
            if attr_name != name:
                args.append("column=%r" % name)
            args.append(
                "reverse=%r" % declared_sets.get((key, name), table + "_set")
            )
            lines.append(
                "    %s = %s(%s)" % (attr_name, kind, ", ".join(args))
            )
            continue
        type_name = _type_name(db, cinfo["type"], cinfo.get("typtype"))
        if type_name is None:
            type_name = "str"
            comment = "  # unknown type: %s" % cinfo["type"]
        elif cinfo["typtype"] == "e":
            comment = "  # enum: %s" % cinfo["type"]
        else:
            comment = ""
        if name in pk and len(pk) == 1:
            args = ["%s" % type_name]
            if cinfo.get("auto"):
                args.append("auto=True")
            elif cinfo["identity"] in ("a", "d"):
                args.append("auto='identity'")
            elif cinfo["default"] and cinfo["default"].startswith("nextval("):
                args.append("auto=True")
            if info["pk_name"]:
                args.append("name=%r" % info["pk_name"])
            lines.append(
                "    %s = PrimaryKey(%s)%s" % (name, ", ".join(args), comment)
            )
        elif name in pk:
            lines.append("    %s = Required(%s)%s" % (name, type_name, comment))
        else:
            kind = "Required" if cinfo["notnull"] else "Optional"
            args = ["%s" % type_name]
            if not cinfo["notnull"]:
                args.append("nullable=True")
            if cinfo["default"]:
                args.append("sql_default=%r" % cinfo["default"])
            if name in info["uniques"]:
                args.append("unique=%r" % info["uniques"][name])
            if name in info["indexes"]:
                args.append("index=%r" % info["indexes"][name])
            lines.append(
                "    %s = %s(%s)%s" % (name, kind, ", ".join(args), comment)
            )
    if len(pk) > 1:
        lines.append(
            "    PrimaryKey(%s, name=%r)" % (", ".join(pk), info["pk_name"])
        )
    for (child_key, fk_col) in sorted(child_list):
        child_table = child_key[1]
        set_name = declared_sets.get((child_key, fk_col), child_table + "_set")
        fk_name = _declared_attr_name(
            declared.get(child_key), fk_col
        ) or _attr_name_for_fk(fk_col)
        lines.append(
            "    %s = Set(%r, reverse=%r)"
            % (set_name, _entity_name(child_table), fk_name)
        )
    lines.append("")
    return lines
