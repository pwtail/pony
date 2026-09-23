"""Интроспекция схемы PostgreSQL (этап 2 миграций pony).

- `db.introspect()` — достраивает объявленные entity-классы атрибутами,
  выводимыми из каталога БД, и генерирует маппинг; после вызова работают
  обычные `db_session` и ORM-запросы.
- `db.introspect(out='entities.py')` — отладочный режим: пишет в файл
  описания сущностей, выведенные из схемы БД (классы при этом не трогает).

Диалект этапа — PostgreSQL. Sync.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from pony.orm import core
from pony.orm.core import Database, Optional, PrimaryKey, Required, Set
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


def _q(db, sql, params):
    return db.select(sql, params)


def _qualified_name(table_name):
    if isinstance(table_name, str):
        return table_name
    return "%s.%s" % table_name


def _type_name(typname, typtype):
    """Имя python-типа для колонки; None — неизвестный тип."""
    mapped = PG_TYPE_MAP.get(typname)
    if mapped is not None:
        return mapped
    if typtype == "e":  # user-defined enum
        return "str"
    return None


def table_info(db, table_name):
    """Читает каталог для одной таблицы: колонки, PK, unique, индексы, FK."""
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
        fks[row.col] = row

    return {
        "columns": columns,
        "column_list": column_list,
        "pk": list(pk),
        "pk_name": pk_name,
        "uniques": uniques,
        "indexes": indexes,
        "fks": fks,
    }


def _create_referenced_entities(db):
    """Создаёт entity-классы для упомянутых в декларациях, но не объявленных
    сущностей, если в схеме есть таблица по конвенции имени."""
    missing = []
    for entity in list(db.entities.values()):
        if entity._root_ is not entity:
            continue
        for attr in entity._new_attrs_:
            py_type = attr.py_type
            if isinstance(py_type, str) and py_type not in db.entities:
                missing.append(py_type)
    for name in missing:
        table_name = db.provider.normalize_name(name)
        rows = _q(
            db,
            "SELECT to_regclass($q)",
            {"q": _qualified_name(table_name)},
        )
        if not rows or rows[0] is None:
            raise IntrospectionError(
                "Entity %s is referenced by the model but not declared, "
                "and table %r does not exist" % (name, table_name)
            )
        core.EntityMeta(name, (db.Entity,), {})


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


def introspect(db, out=None):
    """db.introspect(out=None): заполняет классы из схемы БД и строит маппинг;
    с out='файл' — пишет выведенные описания сущностей в файл (отладка)."""
    if db.provider is None:
        raise IntrospectionError("Database object is not bound with a provider yet")
    if db.provider.dialect != "PostgreSQL":
        raise IntrospectionError(
            "Introspection supports only PostgreSQL for now (got %s)"
            % db.provider.dialect
        )
    if db.schema is not None:
        raise IntrospectionError("Mapping was already generated")
    if out is None and not any(
        entity._root_ is entity for entity in db.entities.values()
    ):
        raise IntrospectionError(
            "No entities are declared: declare the entity classes you need "
            "(even empty ones) before db.introspect()"
        )
    with core.db_session(ddl=True):
        if out is not None:
            return _dump_declarations(db, out)
        _create_referenced_entities(db)
        entities = [
            entity
            for entity in sorted(db.entities.values(), key=lambda e: e._id_)
            if entity._root_ is entity
        ]
        table_map = {}
        for entity in entities:
            table_name = entity._table_ or db.provider.get_default_entity_table_name(
                entity
            )
            table_map[_qualified_name(table_name)] = entity
        for entity in entities:
            table_name = entity._table_ or db.provider.get_default_entity_table_name(
                entity
            )
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
        type_name = _type_name(cinfo["type"], cinfo["typtype"])
        py_type = TYPE_PY.get(type_name) if type_name else None
        notnull = bool(cinfo["notnull"])
        is_pk = name in pk
        fk = info["fks"].get(name)

        if fk is not None and fk.ncols == 1 and not is_pk:
            parent_key = "%s.%s" % (fk.pschema, fk.ptable)
            parent_entity = table_map.get(parent_key) or table_map.get(fk.ptable)
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
                py_type = TYPE_PY.get(PG_TYPE_MAP.get(fk.ptype, "int"), int)
            attr = (Required if notnull else Optional)(py_type, **kwargs)
            if not notnull:
                attr.nullable = True
            _add_attr(entity, attr_name, attr)
            continue

        if py_type is None:
            py_type = str  # неизвестный тип: базовый str (best effort)

        if is_pk and len(pk) == 1:
            kwargs = {"name": info["pk_name"]}
            if cinfo["identity"] in ("a", "d"):
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


def _dump_declarations(db, out):
    rows = _q(
        db,
        "SELECT schemaname, tablename FROM pg_tables "
        "WHERE schemaname = current_schema() ORDER BY tablename",
        {},
    )
    tables = [(row.schemaname, row.tablename) for row in rows]
    infos = {}
    children = {}
    for schema, table in tables:
        key = (schema, table)
        infos[key] = table_info(db, key)
    for key, info in infos.items():
        for col, fk in info["fks"].items():
            if fk.ncols != 1:
                continue
            parent_key = (fk.pschema, fk.ptable)
            children.setdefault(parent_key, []).append((key, col))
    lines = ["from pony.orm import *", "", ""]
    for schema, table in tables:
        key = (schema, table)
        lines.extend(_dump_entity(key, infos[key], children.get(key, [])))
    with open(out, "w") as f:
        f.write("\n".join(lines))
        f.write("\n")
    return out


def _dump_entity(key, info, child_list):
    schema, table = key
    lines = []
    cls = _entity_name(table)
    lines.append("class %s(db.Entity):" % cls)
    if schema != "public":
        lines.append("    _table_ = (%r, %r)" % (schema, table))
    elif table != table.lower():
        lines.append("    _table_ = %r" % table)
    pk = info["pk"]
    fk_cols = {col: row for col, row in info["fks"].items() if row.ncols == 1}
    for name, cinfo in info["column_list"]:
        fk = fk_cols.get(name)
        if fk is not None and name not in pk:
            attr_name = _attr_name_for_fk(name)
            parent = _entity_name(fk.ptable)
            kind = "Optional" if not cinfo["notnull"] else "Required"
            args = ["%r" % parent]
            if attr_name != name:
                args.append("column=%r" % name)
            args.append("reverse=%r" % (table + "_set"))
            lines.append(
                "    %s = %s(%s)" % (attr_name, kind, ", ".join(args))
            )
            continue
        type_name = _type_name(cinfo["type"], cinfo["typtype"])
        if type_name is None:
            type_name = "str"
            comment = "  # unknown type: %s" % cinfo["type"]
        elif cinfo["typtype"] == "e":
            comment = "  # enum: %s" % cinfo["type"]
        else:
            comment = ""
        if name in pk and len(pk) == 1:
            args = ["%s" % type_name]
            if cinfo["identity"] in ("a", "d"):
                args.append("auto='identity'")
            elif cinfo["default"] and cinfo["default"].startswith("nextval("):
                args.append("auto=True")
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
        lines.append(
            "    %s = Set(%r, reverse=%r)"
            % (child_table + "_set", _entity_name(child_table), _attr_name_for_fk(fk_col))
        )
    lines.append("")
    return lines
