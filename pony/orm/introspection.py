"""Интроспекция схемы БД (этап 2 миграций pony).

- `db.introspect()` — достраивает объявленные entity-классы атрибутами,
  выводимыми из каталога БД, и генерирует маппинг; после вызова работают
  обычные `db_session` и ORM-запросы.
- `db.introspect('app1', app2)` — то же, но только для сущностей
  перечисленных приложений (имя строкой или объект Application);
  объявленная сущность вне списка — ошибка.
- `db.introspect(..., dump='models.py')` — дополнительно пишет декларации
  всех таблиц этих приложений в файл; без объявленных классов — пассивно
  (только файл, маппинг не строится).

Диалекты: PostgreSQL (каталог pg_catalog), MySQL/MariaDB (information_schema),
SQLite (PRAGMA). Sync.
"""

import re
from collections import namedtuple
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from pony.orm import core
from pony.orm.core import (
    Database,
    Index,
    Optional,
    PrimaryKey,
    Required,
    Set,
)
from pony.orm.ormtypes import FloatArray, IntArray, Json, StrArray


class IntrospectionError(core.OrmError):
    pass


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
        rows = _q(
            db, "SELECT to_regclass($q)", {"q": _pg_regclass_name(table_name)}
        )
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


def _pg_regclass_name(table_name):
    """Имя для to_regclass: каждый компонент квотирован — mixed-case и
    quoted идентификаторы резолвятся точно, а не case-fold'ом."""
    if isinstance(table_name, str):
        parts = (table_name,)
    else:
        parts = table_name
    return ".".join('"%s"' % part.replace('"', '""') for part in parts)


def _strip_outer_cast(expr):
    """Снимает trailing ::type каст верхнего уровня ('new'::text → 'new').
    Касты внутри скобок и строковых литералов не трогаем:
    nextval('seq'::regclass) и ('x'::text || 'y'::text) остаются как есть."""
    depth = 0
    in_str = False
    last = -1
    i = 0
    while i < len(expr):
        ch = expr[i]
        if in_str:
            if ch == "'":
                if expr[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                in_str = False
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ":" and depth == 0 and expr[i + 1 : i + 2] == ":":
            last = i
            i += 1
        i += 1
    if last == -1:
        return expr
    tail = expr[last + 2 :]
    if re.match(r'^[\w."\[\] ]+$', tail):
        return expr[:last]
    return expr


def _table_info_postgres(db, table_name):
    """Каталог PostgreSQL (pg_catalog) для одной таблицы."""
    qname = _pg_regclass_name(table_name)
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
            # (только внешний top-level каст, см. _strip_outer_cast)
            default_expr = _strip_outer_cast(default_expr)
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

    # все не-PK индексы одним запросом; частичные (indpred) и
    # выраженческие (indexprs) не выводим — пони не восстанавливает
    # их предикаты (спека «Что нельзя вычислить из схемы»)
    uniques = {}
    indexes = {}
    composite_uniques = []
    composite_indexes = []
    by_index = {}
    for row in _q(
        db,
        "SELECT COALESCE(c.conname, ic.relname) AS idxname, "
        "i.indisunique AS isunique, a.attname AS col, "
        "array_position(i.indkey, a.attnum) AS pos "
        "FROM pg_index i "
        "JOIN pg_class ic ON ic.oid = i.indexrelid "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "LEFT JOIN pg_constraint c ON c.conindid = i.indexrelid "
        "WHERE i.indrelid = to_regclass($q) AND NOT i.indisprimary "
        "AND i.indpred IS NULL AND i.indexprs IS NULL",
        {"q": qname},
    ):
        by_index.setdefault((row.idxname, row.isunique), []).append(
            (row.pos, row.col)
        )
    for (idxname, isunique), cols in sorted(by_index.items()):
        cols = [col for _pos, col in sorted(cols)]
        if len(cols) == 1:
            (uniques if isunique else indexes)[cols[0]] = idxname
        elif isunique:
            composite_uniques.append((idxname, cols))
        else:
            composite_indexes.append((idxname, cols))

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
        "composite_uniques": composite_uniques,
        "composite_indexes": composite_indexes,
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
    # одноколоночный select: db.select возвращает плоский список скаляров
    pk = [normalize_name(row) for row in pk_rows]
    constraint_rows = _q(
        db,
        "SELECT CONSTRAINT_NAME AS name FROM information_schema.table_constraints "
        "WHERE TABLE_SCHEMA = $s AND TABLE_NAME = $t AND CONSTRAINT_TYPE = 'PRIMARY KEY'",
        {"s": schema_name, "t": base_name},
    )
    pk_name = constraint_rows[0] if constraint_rows else None

    # SUB_PART IS NULL: префиксные индексы (idx (col(16))) не выводим —
    # это не полный индекс по колонке
    uniques = {}
    indexes = {}
    composite_uniques = []
    composite_indexes = []
    by_index = {}
    for row in _q(
        db,
        "SELECT COLUMN_NAME AS col, INDEX_NAME AS idxname, NON_UNIQUE AS nonunique, "
        "SEQ_IN_INDEX AS seq "
        "FROM information_schema.statistics "
        "WHERE TABLE_SCHEMA = $s AND TABLE_NAME = $t AND INDEX_NAME != 'PRIMARY' "
        "AND SUB_PART IS NULL",
        {"s": schema_name, "t": base_name},
    ):
        by_index.setdefault((row.idxname, row.nonunique == 0), []).append(
            (row.seq, normalize_name(row.col))
        )
    for (idxname, isunique), cols in sorted(by_index.items()):
        cols = [col for _seq, col in sorted(cols)]
        if len(cols) == 1:
            (uniques if isunique else indexes)[cols[0]] = idxname
        elif isunique:
            composite_uniques.append((idxname, cols))
        else:
            composite_indexes.append((idxname, cols))

    fks = {}
    fk_rows = _q(
        db,
        "SELECT kcu.COLUMN_NAME AS col, kcu.REFERENCED_TABLE_SCHEMA AS pschema, "
        "kcu.REFERENCED_TABLE_NAME AS ptable, pc.DATA_TYPE AS ptype, "
        "rc.DELETE_RULE AS deltype, kcu.CONSTRAINT_NAME AS conname "
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
    )
    fk_counts = {}
    for row in fk_rows:
        fk_counts[row.conname] = fk_counts.get(row.conname, 0) + 1
    for row in fk_rows:
        col = normalize_name(row.col)
        fks[col] = FKInfo(
            col, row.pschema, row.ptable, row.ptype, row.deltype,
            fk_counts[row.conname],
        )

    return {
        "columns": columns,
        "column_list": column_list,
        "pk": pk,
        "pk_name": pk_name,
        "uniques": uniques,
        "indexes": indexes,
        "composite_uniques": composite_uniques,
        "composite_indexes": composite_indexes,
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
    composite_uniques = []
    composite_indexes = []
    for idx in _q(db, "SELECT * FROM pragma_index_list($t)", {"t": table_name}):
        if idx.origin == "pk" or idx.partial:
            continue  # частичные индексы не выводим (предикат не восстанавливается)
        cols = _q(db, "SELECT * FROM pragma_index_info($n)", {"n": idx.name})
        col_names = [c.name for c in cols]
        if any(name is None for name in col_names):
            continue  # выраженческий индекс
        if len(col_names) == 1:
            if idx.unique:
                uniques[col_names[0]] = idx.name
            else:
                indexes[col_names[0]] = idx.name
        elif idx.unique:
            composite_uniques.append((idx.name, col_names))
        else:
            composite_indexes.append((idx.name, col_names))

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
        "composite_uniques": composite_uniques,
        "composite_indexes": composite_indexes,
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


def _remove_implicit(entity, attr):
    """Убирает неявный атрибут (авто-id) из класса, pk-учёта и индексов."""
    entity._attrs_.remove(attr)
    entity._new_attrs_.remove(attr)
    del entity._adict_[attr.name]
    if getattr(entity, attr.name, None) is attr:
        delattr(entity, attr.name)
    for index in entity._indexes_:
        if attr in index.attrs:
            index.attrs = tuple(a for a in index.attrs if a is not attr)
    entity._pk_attrs_ = tuple(a for a in entity._pk_attrs_ if a is not attr)
    entity._pk_is_composite_ = len(entity._pk_attrs_) > 1
    if entity._pk_is_composite_:
        entity._pk_ = entity._pk_attrs_
    else:
        entity._pk_ = entity._pk_attrs_[0] if entity._pk_attrs_ else None


def _add_attr(entity, name, attr):
    existing = entity._adict_.get(name)
    if existing is not None and existing.is_implicit:
        # неявный атрибут (авто-id) замещаем выведенным из схемы
        _remove_implicit(entity, existing)
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
    if attr.is_pk and not attr.is_collection:
        # выведенный из схемы PK замещает неявный авто-id независимо от имени
        # колонки: регистрируем его в pk-учёте и pk-индексе сущности
        for implicit in [a for a in entity._pk_attrs_ if a.is_implicit]:
            _remove_implicit(entity, implicit)
        if attr not in entity._pk_attrs_:
            entity._pk_attrs_ += (attr,)
        entity._pk_is_composite_ = len(entity._pk_attrs_) > 1
        entity._pk_ = (
            entity._pk_attrs_
            if entity._pk_is_composite_
            else entity._pk_attrs_[0]
        )
        attr.pk_offset = entity._pk_attrs_.index(attr)
        for index in entity._indexes_:
            if index.is_pk and not index.attrs:
                index.attrs = (attr,)
    return attr


def _attr_name_for_fk(column):
    if column.endswith("_id"):
        return column[:-3]
    return column


def _lookup_table_entity(table_map, pschema, ptable):
    """Сущность по (schema, table) родителя FK в карте объявленных таблиц."""
    parent_key = "%s.%s" % (pschema, ptable)
    return (
        table_map.get(parent_key)
        or table_map.get(ptable)
        or table_map.get(parent_key.lower())
        or table_map.get((ptable or "").lower())
    )


def _link_table_parents(info):
    """Связочная таблица m2m: ровно две колонки, обе — одиночные FK на разные
    таблицы, и PK состоит ровно из них. Возвращает
    ((col_a, fk_a), (col_b, fk_b)) или None."""
    columns = info["column_list"]
    if len(columns) != 2:
        return None
    names = [name for name, _info in columns]
    if len(info["pk"]) != 2 or set(info["pk"]) != set(names):
        return None
    fks = []
    for name in names:
        fk = info["fks"].get(name)
        if fk is None or fk.ncols != 1:
            return None
        fks.append((name, fk))
    (_col_a, fk_a), (_col_b, fk_b) = fks
    if fk_a.pschema == fk_b.pschema and fk_a.ptable == fk_b.ptable:
        return None  # симметричный m2m — только явным объявлением
    return fks[0], fks[1]


def _m2m_table_arg(db, schema, table):
    if schema and schema != db.provider.default_schema_name:
        return (schema, table)
    return table


def _declared_m2m(entity_a, entity_b):
    """Уже объявленная пользователем пара Set+Set между сущностями
    (решение 9: объявленное интроспекция не дополняет)."""
    for attr in entity_a._new_attrs_:
        if attr.is_collection and attr.py_type is entity_b:
            return True
    for attr in entity_b._new_attrs_:
        if attr.is_collection and attr.py_type is entity_a:
            return True
    return False


def _add_m2m_pair(db, entity_a, entity_b, table_name, col_a, col_b):
    """Пара Set на родителях связочной таблицы; col_a/col_b — FK-колонки,
    ссылающиеся на таблицы entity_a/entity_b. В pony column= — это колонка,
    ссылающаяся на таблицу ЦЕЛИ атрибута (reverse_column — только для
    симметричных связей), поэтому сторона entity_a получает col_b и наоборот.
    Конвенция pony об именах колонок связи может не совпадать со схемой —
    задаём явно."""
    name_a = entity_b.__name__.lower() + "_set"  # на стороне entity_a
    name_b = entity_a.__name__.lower() + "_set"  # на стороне entity_b
    _add_attr(
        entity_a,
        name_a,
        Set(entity_b, table=table_name, column=col_b, reverse=name_b),
    )
    _add_attr(
        entity_b,
        name_b,
        Set(entity_a, column=col_a, reverse=name_a),
    )


def _link_m2m_tables(db, table_map):
    """Находит в схеме связочные таблицы между объявленными сущностями и
    добавляет пару Set на обоих родителях (сами таблицы entity не становятся)."""
    for schema, table in _list_tables(db):
        qname = "%s.%s" % (schema, table) if schema else table
        if qname in table_map or qname.lower() in table_map:
            continue  # таблица сопоставлена объявленной сущности
        table_name = (schema, table) if schema else table
        link = _link_table_parents(table_info(db, table_name))
        if link is None:
            continue
        (col_a, fk_a), (col_b, fk_b) = link
        entity_a = _lookup_table_entity(table_map, fk_a.pschema, fk_a.ptable)
        entity_b = _lookup_table_entity(table_map, fk_b.pschema, fk_b.ptable)
        if entity_a is None or entity_b is None or entity_a is entity_b:
            continue
        if _declared_m2m(entity_a, entity_b):
            continue
        _add_m2m_pair(
            db,
            entity_a,
            entity_b,
            _m2m_table_arg(db, schema, table),
            col_a,
            col_b,
        )


def _resolve_apps(db, apps):
    """Позиционные аргументы introspect — приложения (имя строкой или объект
    Application). None — без ограничений (все приложения + сущности без
    приложения)."""
    if not apps:
        return None
    scope = set()
    for ref in apps:
        if isinstance(ref, str):
            app = db._apps.get(ref)
            if app is None:
                raise IntrospectionError(
                    "Unknown application %r. Registered applications: %s"
                    % (ref, ", ".join(sorted(db._apps)) or "none")
                )
        elif isinstance(ref, core.Application):
            app = db._apps.get(ref.name)
            if app is None:
                raise IntrospectionError(
                    "Application %r is not registered in this database"
                    % ref.name
                )
        else:
            raise IntrospectionError(
                "introspect() takes applications (a name or an Application "
                "object), got %r" % (ref,)
            )
        scope.add(app)
    return scope


def _snapshot_entities(db):
    """Состояние entity-классов до интроспекции: набор сущностей и всё,
    что интроспекция в них мутирует (атрибуты, pk-учёт, индексы)."""
    snapshot = {}
    for entity in db.entities.values():
        if entity._root_ is not entity:
            continue
        snapshot[entity] = (
            list(entity._attrs_),
            list(entity._new_attrs_),
            dict(entity._adict_),
            entity._pk_attrs_,
            entity._pk_,
            entity._pk_is_composite_,
            list(entity._indexes_),
            [tuple(index.attrs) for index in entity._indexes_],
        )
    return snapshot


def _restore_entities(db, snapshot):
    """Откат после упавшей интроспекции: созданные сущности убираются,
    изменённые — возвращаются к снапшоту, флаг _is_empty восстанавливается
    (ленивый триггер сработает снова)."""
    for entity in list(db.entities.values()):
        if entity._root_ is not entity:
            continue
        state = snapshot.get(entity)
        if state is None:
            # созданная интроспекцией сущность
            del db.entities[entity.__name__]
            if getattr(db, entity.__name__, None) is entity:
                delattr(db, entity.__name__)
            continue
        (
            attrs,
            new_attrs,
            adict,
            pk_attrs,
            pk,
            pk_is_composite,
            indexes,
            index_attrs,
        ) = state
        for name in set(entity._adict_) - set(adict):
            attr = entity._adict_[name]
            if getattr(entity, name, None) is attr:
                delattr(entity, name)
        for name, attr in adict.items():
            if getattr(entity, name, None) is not attr:
                setattr(entity, name, attr)
        entity._attrs_[:] = attrs
        entity._new_attrs_[:] = new_attrs
        entity._adict_.clear()
        entity._adict_.update(adict)
        entity._pk_attrs_ = pk_attrs
        entity._pk_ = pk
        entity._pk_is_composite_ = pk_is_composite
        entity._indexes_[:] = indexes
        for index, attrs_tuple in zip(indexes, index_attrs):
            index.attrs = attrs_tuple
    db.schema = None
    db._is_empty = True


def introspect(db, *apps, dump=None):
    """db.introspect(*apps, dump=None): достраивает объявленные entity-классы
    атрибутами из схемы БД и строит маппинг. Позиционные аргументы —
    приложения: интроспекция только их сущностей (объявленная сущность
    другого приложения — ошибка); без аргументов — все приложения и
    сущности без приложения. `dump=path` дополнительно пишет декларации
    всех таблиц этих приложений в файл."""
    if db.provider is None:
        raise IntrospectionError("Database object is not bound with a provider yet")
    if db.provider.dialect not in ("PostgreSQL", "MySQL", "SQLite"):
        raise IntrospectionError(
            "Introspection supports PostgreSQL, MySQL/MariaDB and SQLite "
            "(got %s)" % db.provider.dialect
        )
    if db.schema is not None:
        raise IntrospectionError("Mapping was already generated")
    scope = _resolve_apps(db, apps)
    declared = [
        entity
        for entity in sorted(db.entities.values(), key=lambda e: e._id_)
        if entity._root_ is entity
    ]
    if scope is not None:
        for entity in declared:
            if getattr(entity, "_app_", None) not in scope:
                entity_app = getattr(entity, "_app_", None)
                raise IntrospectionError(
                    "Entity %s belongs to %s, which is not part of this "
                    "introspection (scope: %s)"
                    % (
                        entity.__name__,
                        "application %r" % entity_app.name
                        if entity_app is not None
                        else "no application",
                        ", ".join(sorted(app.name for app in scope)),
                    )
                )
    if not declared:
        if dump is None:
            raise IntrospectionError(
                "No entities are declared: declare the entity classes you need "
                "(even empty ones) before db.introspect()"
            )
        # dump без объявленных классов: пассивно пишем файл, маппинг не
        # строим — ленивый триггер (with db:) остаётся вооружённым
        with core.db_session:
            dump_text = _dump_declarations(db, scope)
        with open(dump, "w") as f:
            f.write(dump_text)
        return
    # интроспекция выполняется: флаг снимается, ленивый триггер больше
    # не срабатывает (в т.ч. на внутренних запросах)
    db._is_empty = False
    snapshot = _snapshot_entities(db)
    dump_text = None
    try:
        with core.db_session:
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
            _link_m2m_tables(db, table_map)
            if dump is not None:
                dump_text = _dump_declarations(db, scope)
        db.generate_mapping(check_tables=False)
    except BaseException:
        _restore_entities(db, snapshot)
        raise
    if dump is not None:
        with open(dump, "w") as f:
            f.write(dump_text)


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

    if not info["column_list"]:
        raise IntrospectionError(
            "Table %s does not exist" % (_qualified_name(table_name),)
        )
    pk = info["pk"]
    declared_pk = any(
        attr.is_pk and not attr.is_implicit
        for attr in entity._new_attrs_
        if not attr.is_collection
    )
    if len(pk) > 1 and not declared_pk:
        raise IntrospectionError(
            "Composite primary key of table %s must be declared explicitly: "
            "PrimaryKey(%s, name=...)" % (_qualified_name(table_name), ", ".join(pk))
        )
    if not pk and not declared_pk:
        raise IntrospectionError(
            "Table %s has no primary key; pony entities require a primary key"
            % (_qualified_name(table_name),)
        )

    for name, cinfo in info["column_list"]:
        if name in covered:
            continue
        type_name = _type_name(db, cinfo["type"], cinfo.get("typtype"))
        py_type = TYPE_PY.get(type_name) if type_name else None
        notnull = bool(cinfo["notnull"])
        is_pk = name in pk
        fk = info["fks"].get(name)
        unique_name = info["uniques"].get(name)
        index_name = info["indexes"].get(name)

        if fk is not None and fk.ncols == 1 and not is_pk:
            parent_entity = _lookup_table_entity(table_map, fk.pschema, fk.ptable)
            attr_name = _attr_name_for_fk(name)
            kwargs = {}
            if normalize_name(attr_name) != normalize_name(name):
                kwargs["column"] = name
            # unique/index/sql_default применяются и к FK-колонкам
            if unique_name:
                kwargs["unique"] = unique_name or True
            if index_name:
                kwargs["index"] = index_name or True
            if parent_entity is None and py_type is None:
                py_type = _py_type_from_sql(db, fk.ptype)
            attr = (Required if notnull else Optional)(
                parent_entity if parent_entity is not None else py_type, **kwargs
            )
            if not notnull:
                attr.nullable = True
            if cinfo["default"]:
                attr.sql_default = cinfo["default"]
            _add_attr(entity, attr_name, attr)
            if unique_name:
                _add_unique_index(entity, attr, unique_name)
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
            kwargs = {}
            if unique_name:
                kwargs["unique"] = unique_name or True
            if index_name:
                kwargs["index"] = index_name or True
            if notnull:
                attr = Required(py_type, **kwargs)
            else:
                attr = Optional(py_type, nullable=True, **kwargs)

        if cinfo["default"] and not is_pk:
            attr.sql_default = cinfo["default"]
        _add_attr(entity, name, attr)
        if unique_name:
            _add_unique_index(entity, attr, unique_name)

    _register_composite_indexes(entity, info)


def _add_unique_index(entity, attr, name):
    """Single-column UNIQUE из каталога → Index-объект сущности (как
    EntityMeta делает для объявленных unique=True — иначе констрейнт не
    доходит до dbschema и DDL-генерации)."""
    index = Index(
        attr, name=name if isinstance(name, str) else None, is_pk=False
    )
    index._init_(entity)
    entity._indexes_.append(index)


def _register_composite_indexes(entity, info):
    """Составные UNIQUE/индексы из каталога → Index-объекты сущности."""
    by_column = {}
    for attr in entity._new_attrs_:
        if attr.is_collection:
            continue
        columns = attr.columns or ([attr.column] if attr.column else [attr.name])
        for column in columns:
            by_column[column.lower()] = attr
    pairs = [(True, item) for item in info["composite_uniques"]] + [
        (False, item) for item in info["composite_indexes"]
    ]
    for is_unique, (idxname, cols) in pairs:
        attrs = []
        for col in cols:
            attr = by_column.get(col.lower())
            if attr is None:
                break
            attrs.append(attr)
        else:
            index = Index(*attrs, is_unique=is_unique, name=idxname or None)
            index._init_(entity)
            entity._indexes_.append(index)


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
        # одноколоночный select: db.select возвращает плоский список скаляров
        return [(None, row) for row in rows]
    rows = _q(
        db,
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name",
        {},
    )
    return [(None, row) for row in rows]


def _declared_entities(db, tables):
    declared = {}
    table_keys = set(tables)
    for entity in db.entities.values():
        if entity._root_ is not entity:
            continue
        table_name = _entity_table_name(db, entity)
        schema, base = db.provider.split_table_name(table_name)
        key = _catalog_key_for(table_keys, schema, base)
        if key is not None:
            declared[key] = entity
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


def _dump_col_name(declared_entity, fk_cols, column):
    """Имя атрибута в дампе для колонки составного индекса:
    объявленное имя, имя связи для FK, иначе имя колонки."""
    name = _declared_attr_name(declared_entity, column)
    if name is not None:
        return name
    if column in fk_cols:
        return _attr_name_for_fk(column)
    return column


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


def _catalog_key_for(table_keys, pschema, ptable):
    """Ключ каталога (schema, table) для родителя FK: точное совпадение,
    иначе единственная таблица с таким именем (MySQL: ключи без схемы)."""
    key = (pschema, ptable)
    if key in table_keys:
        return key
    # регистронезависимо: имена таблиц в SQLite/MySQL регистронезависимы
    matches = [k for k in table_keys if k[1].lower() == ptable.lower()]
    if len(matches) == 1:
        return matches[0]
    for k in matches:
        if k[0] is None or k[0] == pschema:
            return k
    return None


def _wanted_tables(db, scope, tables, declared):
    """Таблицы дампа по скоупу приложений. PostgreSQL — все таблицы схем
    приложений; MySQL/SQLite — таблицы объявленных сущностей приложений
    (схемной границы нет, pony-apps решение 8). None — весь каталог."""
    if scope is None:
        return set(tables)
    if db.provider.dialect == "PostgreSQL":
        schemas = {app.schema_name for app in scope}
        return {key for key in tables if key[0] in schemas}
    return {
        key
        for key, entity in declared.items()
        if getattr(entity, "_app_", None) in scope
    }


def _dump_declarations(db, scope=None):
    """Текст деклараций моделей приложений скоупа (все их таблицы) —
    для записи в файл опцией dump=."""
    tables = _list_tables(db)
    infos = {}
    children = {}
    for schema, table in tables:
        key = (schema, table)
        table_name = key if schema else table
        infos[key] = table_info(db, table_name)
    table_keys = set(tables)
    link_pairs = {}  # связочная таблица -> ((родитель_a, col_a), (родитель_b, col_b))
    for key, info in infos.items():
        link = _link_table_parents(info)
        if link is None:
            continue
        (col_a, fk_a), (col_b, fk_b) = link
        parent_a = _catalog_key_for(table_keys, fk_a.pschema, fk_a.ptable)
        parent_b = _catalog_key_for(table_keys, fk_b.pschema, fk_b.ptable)
        if parent_a is None or parent_b is None or parent_a == parent_b:
            continue
        link_pairs[key] = ((parent_a, col_a), (parent_b, col_b))
    declared = _declared_entities(db, tables)
    wanted = _wanted_tables(db, scope, tables, declared)
    selected_set = set(wanted)
    m2m = {}  # родитель -> [(другой родитель, связочная, своя FK-колонка)]
    dropped_links = set()
    for link_key, ((parent_a, col_a), (parent_b, col_b)) in link_pairs.items():
        if parent_a not in wanted or parent_b not in wanted:
            continue  # без обоих родителей в скоупе — не связочная
        selected_set.discard(link_key)
        dropped_links.add(link_key)
        # column= — колонка, ссылающаяся на таблицу цели атрибута,
        # поэтому родителю достаётся ЧУЖАЯ FK-колонка
        m2m.setdefault(parent_a, []).append((parent_b, link_key, col_b))
        m2m.setdefault(parent_b, []).append((parent_a, link_key, col_a))
    for key, info in infos.items():
        if key in dropped_links:
            continue  # ставшие Set-парой связочные — не «дети» через FK
        for col, fk in info["fks"].items():
            if fk.ncols != 1:
                continue
            parent_key = _catalog_key_for(table_keys, fk.pschema, fk.ptable)
            if parent_key is not None:
                children.setdefault(parent_key, []).append((key, col))
    declared_sets = _declared_sets(children, declared)
    lines = ["from pony.orm import *", "", ""]
    for schema, table in tables:
        key = (schema, table)
        if key not in selected_set:
            continue
        lines.extend(
            _dump_entity(
                db,
                key,
                infos[key],
                children.get(key, []),
                declared,
                declared_sets,
                m2m.get(key, []),
            )
        )
    return "\n".join(lines)


def _dump_entity(
    db, key, info, child_list, declared=None, declared_sets=None, m2m_list=None
):
    schema, table = key
    declared = declared or {}
    declared_sets = declared_sets or {}
    m2m_list = m2m_list or []
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
            if cinfo["default"]:
                args.append("sql_default=%r" % cinfo["default"])
            if name in info["uniques"]:
                args.append("unique=%r" % info["uniques"][name])
            if name in info["indexes"]:
                args.append("index=%r" % info["indexes"][name])
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
    for idxname, cols in info["composite_uniques"]:
        names = ", ".join(_dump_col_name(entity, fk_cols, col) for col in cols)
        lines.append("    unique(%s, name=%r)" % (names, idxname))
    for idxname, cols in info["composite_indexes"]:
        names = ", ".join(_dump_col_name(entity, fk_cols, col) for col in cols)
        lines.append("    composite_index(%s, name=%r)" % (names, idxname))
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
    for other_key, link_key, own_col in sorted(
        m2m_list, key=lambda item: (str(item[0][0]), item[0][1])
    ):
        other_entity = declared.get(other_key)
        other_cls = (
            other_entity.__name__
            if other_entity is not None
            else _entity_name(other_key[1])
        )
        reverse_name = cls.lower() + "_set"
        existing = None
        if entity is not None:
            for a in entity._new_attrs_:
                if a.is_collection and (
                    a.py_type is other_entity
                    or getattr(a.py_type, "__name__", a.py_type) == other_cls
                ):
                    existing = a
                    break
        if existing is not None:
            # пара уже есть на сущности (объявлена или добавлена fill'ом) —
            # дампим её как есть, со своими именем/table=/column=
            args = [repr(other_cls)]
            if existing.table:
                args.append("table=%r" % (existing.table,))
            existing_col = (
                existing.columns[0] if existing.columns else existing.column
            )
            if existing_col:
                args.append("column=%r" % existing_col)
            existing_reverse = existing.reverse
            args.append(
                "reverse=%r"
                % (
                    existing_reverse
                    if isinstance(existing_reverse, str)
                    else reverse_name
                )
            )
            lines.append("    %s = Set(%s)" % (existing.name, ", ".join(args)))
            continue
        set_name = other_cls.lower() + "_set"
        first = sorted(
            [key, other_key], key=lambda k: (str(k[0]), k[1])
        )[0] == key
        args = [repr(other_cls)]
        if first:
            args.append(
                "table=%r" % (_m2m_table_arg(db, link_key[0], link_key[1]),)
            )
        args.append("column=%r" % own_col)
        args.append("reverse=%r" % reverse_name)
        lines.append("    %s = Set(%s)" % (set_name, ", ".join(args)))
    lines.append("")
    return lines
