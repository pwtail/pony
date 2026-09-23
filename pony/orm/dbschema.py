from operator import attrgetter

from pony.orm import core
from pony.orm.core import DBSchemaError, MappingError, log_sql
from pony.py23compat import int_types
from pony.utils import throw


class DBSchema:
    dialect = None
    inline_fk_syntax = True
    named_foreign_keys = True

    def __init__(self, provider, uppercase=True):
        self.provider = provider
        self.tables = {}
        self.constraints = {}
        self.indent = "  "
        self.command_separator = ";\n\n"
        self.uppercase = uppercase
        self.names = {}

    def column_list(self, columns):
        quote_name = self.provider.quote_name
        return "(%s)" % ", ".join(quote_name(column.name) for column in columns)

    def case(self, s):
        if self.uppercase:
            return (
                s.upper()
                .replace("%S", "%s")
                .replace(")S", ")s")
                .replace("%R", "%r")
                .replace(")R", ")r")
            )
        else:
            return s.lower()

    def add_table(self, table_name, entity=None):
        return self.table_class(table_name, self, entity)

    def order_tables_to_create(self, tables=None):
        if tables is None:
            tables = self.tables.values()
            subset = None
        else:
            tables = list(tables)
            subset = set(tables)
        ordered = []
        created_tables = set()
        split = self.provider.split_table_name
        remaining = sorted(tables, key=lambda table: split(table.name))
        while remaining:
            for table in remaining:
                parents = table.parent_tables
                if subset is not None:
                    parents = parents & subset
                if parents.issubset(created_tables):
                    created_tables.add(table)
                    remaining.remove(table)
                    break
            else:
                table = remaining.pop()
            ordered.append(table)
        return ordered

    def get_schema_names(self, tables=None):
        """Схемы, которые нужно создать перед таблицами (кроме дефолтной)."""
        if tables is None:
            tables = self.tables.values()
        default = self.provider.default_schema_name
        names = set()
        for table in tables:
            if not isinstance(table.name, str):
                schema_name = table.name[0]
                if schema_name != default:
                    names.add(schema_name)
        return names

    def schema_create_commands(self, tables=None):
        provider = self.provider
        return [
            provider.get_create_schema_sql(name)
            for name in sorted(self.get_schema_names(tables))
        ]

    def create_schemas(self, provider, connection):
        for sql in self.schema_create_commands():
            if core.local.debug:
                log_sql(sql)
            cursor = connection.cursor()
            provider.execute(cursor, sql)

    def generate_create_script(self, tables=None):
        if tables is None:
            tables = self.order_tables_to_create()
            subset = None
        else:
            tables = self.order_tables_to_create(tables)
            subset = set(tables)
        commands = list(self.schema_create_commands(tables))
        created_tables = set()
        for table in tables:
            for db_object in table.get_objects_to_create(created_tables, subset):
                commands.append(db_object.get_create_command())
        return self.command_separator.join(commands)

    def create_tables(self, provider, connection):
        created_tables = set()
        for table in self.order_tables_to_create():
            for db_object in table.get_objects_to_create(created_tables):
                base_name = provider.base_name(db_object.name)
                name = db_object.exists(provider, connection, case_sensitive=False)
                if name is None:
                    db_object.create(provider, connection)
                elif name != base_name:
                    quote_name = self.provider.quote_name
                    n1, n2 = quote_name(db_object.name), quote_name(name)
                    tn1, tn2 = db_object.typename, db_object.typename.lower()
                    throw(
                        DBSchemaError,
                        "%s %s cannot be created, because %s %s "
                        "(with a different letter case) already exists in the database. "
                        "Try to delete %s %s first." % (tn1, n1, tn2, n2, n2, tn2),
                    )

    def check_tables(self, provider, connection):
        cursor = connection.cursor()
        split = provider.split_table_name
        for table in sorted(self.tables.values(), key=lambda table: split(table.name)):
            alias = provider.base_name(table.name)
            sql_ast = [
                "SELECT",
                [
                    "ALL",
                ]
                + [["COLUMN", alias, column.name] for column in table.column_list],
                ["FROM", [alias, "TABLE", table.name]],
                ["WHERE", ["EQ", ["VALUE", 0], ["VALUE", 1]]],
            ]
            sql, adapter = provider.ast2sql(sql_ast)
            if core.local.debug:
                log_sql(sql)
            provider.execute(cursor, sql)


class DBObject:
    def create(self, provider, connection):
        sql = self.get_create_command()
        if core.local.debug:
            log_sql(sql)
        cursor = connection.cursor()
        provider.execute(cursor, sql)


class Table(DBObject):
    typename = "Table"

    def __init__(self, name, schema, entity=None):
        if name in schema.tables:
            throw(DBSchemaError, "Table %r already exists in database schema" % name)
        if name in schema.names:
            throw(
                DBSchemaError,
                "Table %r cannot be created, name is already in use" % name,
            )
        schema.tables[name] = self
        schema.names[name] = self
        self.schema = schema
        self.name = name
        self.column_list = []
        self.column_dict = {}
        self.indexes = {}
        self.pk_index = None
        self.checks = []
        self.comment = None
        self.foreign_keys = {}
        self.parent_tables = set()
        self.child_tables = set()
        self.entities = set()
        self.options = {}
        if entity is not None:
            self.entities.add(entity)
            self.options = entity._table_options_
        self.m2m = set()

    def __repr__(self):
        return "<Table(%s)>" % self.schema.provider.format_table_name(self.name)

    def add_entity(self, entity):
        for e in self.entities:
            if e._root_ is not entity._root_:
                throw(
                    MappingError,
                    "Entities %s and %s cannot be mapped to table %s "
                    "because they don't belong to the same hierarchy"
                    % (e, entity, self.name),
                )
        assert "_table_options_" not in entity.__dict__
        self.entities.add(entity)

    def exists(self, provider, connection, case_sensitive=True):
        return provider.table_exists(connection, self.name, case_sensitive)

    def get_create_command(self):
        schema = self.schema
        case = schema.case
        provider = schema.provider
        quote_name = provider.quote_name
        if_not_exists = False  # provider.table_if_not_exists_syntax and provider.index_if_not_exists_syntax
        cmd = []
        if not if_not_exists:
            cmd.append(case("CREATE TABLE %s (") % quote_name(self.name))
        else:
            cmd.append(case("CREATE TABLE IF NOT EXISTS %s (") % quote_name(self.name))
        for column in self.column_list:
            cmd.append(schema.indent + column.get_sql() + ",")
        if len(self.pk_index.columns) > 1 or self.pk_index.name is not None:
            cmd.append(schema.indent + self.pk_index.get_sql() + ",")
        indexes = [
            index
            for index in self.indexes.values()
            if not index.is_pk
            and index.is_unique
            and (len(index.columns) > 1 or index.is_named)
            and not index.renders_standalone
        ]
        for index in indexes:
            assert index.name is not None
        indexes.sort(key=attrgetter("name"))
        for index in indexes:
            cmd.append(schema.indent + index.get_sql() + ",")
        for check in sorted(self.checks, key=attrgetter("name")):
            cmd.append(schema.indent + check.get_sql() + ",")
        if not schema.named_foreign_keys:
            for foreign_key in sorted(
                self.foreign_keys.values(), key=lambda fk: fk.name
            ):
                if schema.inline_fk_syntax and len(foreign_key.child_columns) == 1:
                    continue
                cmd.append(schema.indent + foreign_key.get_sql() + ",")
        interleave_fks = [fk for fk in self.foreign_keys.values() if fk.interleave]
        if interleave_fks:
            assert len(interleave_fks) == 1
            fk = interleave_fks[0]
            cmd.append(schema.indent + fk.get_sql())
            cmd.append(
                case(") INTERLEAVE IN PARENT %s (%s)")
                % (
                    quote_name(fk.parent_table.name),
                    ", ".join(quote_name(col.name) for col in fk.child_columns),
                )
            )
        else:
            cmd[-1] = cmd[-1][:-1]
            cmd.append(")")
        for name, value in sorted(self.options.items()):
            option = self.format_option(name, value)
            if option:
                cmd.append(option)
        return "\n".join(cmd)

    def format_option(self, name, value):
        if value is True:
            return name
        if value is False:
            return None
        return "%s %s" % (name, value)

    def get_objects_to_create(self, created_tables=None, subset=None):
        if created_tables is None:
            created_tables = set()
        created_tables.add(self)
        result = [self]
        indexes = [
            index for index in self.indexes.values() if index.renders_standalone
        ]
        for index in indexes:
            assert index.name is not None
        indexes.sort(key=attrgetter("name"))
        result.extend(indexes)
        if self.comment:
            result.append(
                DBComment(self.schema, self.name, None, self.comment)
            )
        for column in self.column_list:
            if column.comment:
                result.append(
                    DBComment(
                        self.schema, self.name, column.name, column.comment
                    )
                )
        schema = self.schema
        if schema.named_foreign_keys:
            for foreign_key in sorted(
                self.foreign_keys.values(), key=lambda fk: fk.name
            ):
                parent_available = foreign_key.parent_table in created_tables
                if subset is not None and foreign_key.parent_table not in subset:
                    parent_available = True
                if not parent_available:
                    continue
                result.append(foreign_key)
            for child_table in self.child_tables:
                if subset is not None and child_table not in subset:
                    continue
                if child_table not in created_tables:
                    continue
                for foreign_key in sorted(
                    child_table.foreign_keys.values(), key=lambda fk: fk.name
                ):
                    if foreign_key.parent_table is not self:
                        continue
                    result.append(foreign_key)
        return result

    def add_column(
        self,
        column_name,
        sql_type,
        converter,
        is_not_null=None,
        sql_default=None,
        comment=None,
    ):
        return self.schema.column_class(
            column_name, self, sql_type, converter, is_not_null, sql_default, comment
        )

    def add_index(
        self,
        index_name,
        columns,
        is_pk=False,
        is_unique=None,
        m2m=False,
        using=None,
        where=None,
        include=None,
        nulls_not_distinct=False,
        key_spec=None,
    ):
        assert index_name is not False
        is_named = isinstance(index_name, str)
        if index_name is True:
            index_name = None
        if index_name is None and not is_pk:
            provider = self.schema.provider
            index_name = provider.get_default_index_name(
                self.name,
                (column.name for column in columns),
                is_pk=is_pk,
                is_unique=is_unique,
                m2m=m2m,
            )
        index = self.indexes.get(columns)
        if (
            index
            and index.name == index_name
            and index.is_pk == is_pk
            and index.is_unique == is_unique
            and index.using == using
            and index.where == where
            and index.include == include
            and index.nulls_not_distinct == nulls_not_distinct
            and index.key_spec == key_spec
        ):
            return index
        return self.schema.index_class(
            index_name,
            self,
            columns,
            is_pk,
            is_unique,
            is_named,
            using,
            where,
            include,
            nulls_not_distinct,
            key_spec,
        )

    def add_check(self, name, sql):
        assert name is not None
        if name in self.schema.constraints:
            throw(
                DBSchemaError, "Constraint with name %r already exists" % name
            )
        check = self.schema.check_class(name, self, sql)
        self.checks.append(check)
        return check

    def add_foreign_key(
        self,
        fk_name,
        child_columns,
        parent_table,
        parent_columns,
        index_name=None,
        on_delete=False,
        interleave=False,
    ):
        if fk_name is None:
            provider = self.schema.provider
            child_column_names = tuple(column.name for column in child_columns)
            fk_name = provider.get_default_fk_name(
                self.name, parent_table.name, child_column_names
            )
        return self.schema.fk_class(
            fk_name,
            self,
            child_columns,
            parent_table,
            parent_columns,
            index_name,
            on_delete,
            interleave=interleave,
        )


class Column:
    auto_template = "%(type)s PRIMARY KEY AUTOINCREMENT"
    identity_template = None
    # Варианты без inline PRIMARY KEY — для именованного PK-констрейнта
    # (PRIMARY KEY выносится в CONSTRAINT ... PRIMARY KEY (...)).
    auto_template_named = None
    identity_template_named = None

    def __init__(
        self,
        name,
        table,
        sql_type,
        converter,
        is_not_null=None,
        sql_default=None,
        comment=None,
    ):
        if name in table.column_dict:
            throw(
                DBSchemaError,
                "Column %r already exists in table %r" % (name, table.name),
            )
        table.column_dict[name] = self
        table.column_list.append(self)
        self.table = table
        self.name = name
        self.sql_type = sql_type
        self.converter = converter
        self.is_not_null = is_not_null
        self.sql_default = sql_default
        self.comment = comment
        self.is_pk = False
        self.is_pk_part = False
        self.is_unique = False

    def __repr__(self):
        return "<Column(%s.%s)>" % (self.table.name, self.name)

    def get_sql(self):
        table = self.table
        schema = table.schema
        quote_name = schema.provider.quote_name
        case = schema.case
        pk_index = table.pk_index
        named_pk = (
            bool(self.is_pk)
            and pk_index is not None
            and pk_index.name is not None
            and len(pk_index.columns) == 1
        )
        result = []
        append = result.append
        append(quote_name(self.name))

        def add_default():
            if self.sql_default not in (None, True, False):
                append(case("DEFAULT"))
                append(self.sql_default)

        if (
            self.is_pk in ("auto", "identity")
            and self.auto_template
            and self.converter.py_type in int_types
        ):
            if named_pk:
                if self.is_pk == "identity" and self.identity_template_named:
                    template = self.identity_template_named
                else:
                    template = self.auto_template_named
            else:
                template = (
                    self.identity_template
                    if self.is_pk == "identity" and self.identity_template
                    else self.auto_template
                )
            append(case(template % dict(type=self.sql_type)))
            add_default()
        else:
            append(case(self.sql_type))
            add_default()
            if self.is_pk:
                if schema.dialect == "SQLite":
                    append(case("NOT NULL"))
                if not named_pk:
                    append(case("PRIMARY KEY"))
            else:
                index = table.indexes.get((self,))
                if self.is_unique and not (
                    index and (index.is_named or index.renders_standalone)
                ):
                    append(case("UNIQUE"))
                if self.is_not_null:
                    append(case("NOT NULL"))
        if schema.inline_fk_syntax and not schema.named_foreign_keys:
            foreign_key = table.foreign_keys.get((self,))
            if foreign_key is not None:
                parent_table = foreign_key.parent_table
                append(case("REFERENCES"))
                append(quote_name(parent_table.name))
                append(schema.column_list(foreign_key.parent_columns))
                if foreign_key.on_delete:
                    append("ON DELETE %s" % foreign_key.on_delete)
        return " ".join(result)


class Constraint(DBObject):
    def __init__(self, name, schema):
        if name is not None:
            assert name not in schema.names
            if name in schema.constraints:
                throw(DBSchemaError, "Constraint with name %r already exists" % name)
            schema.names[name] = self
            schema.constraints[name] = self
        self.schema = schema
        self.name = name


class DBIndex(Constraint):
    typename = "Index"

    def __init__(
        self,
        name,
        table,
        columns,
        is_pk=False,
        is_unique=None,
        is_named=False,
        using=None,
        where=None,
        include=None,
        nulls_not_distinct=False,
        key_spec=None,
    ):
        has_expressions = key_spec is not None and any(
            expr for _, _, expr in key_spec
        )
        assert len(columns) > 0 or has_expressions
        for column in columns:
            if column.table is not table:
                throw(
                    DBSchemaError,
                    "Column %r does not belong to table %r and cannot be part of its index"
                    % (column.name, table.name),
                )
        if columns in table.indexes:
            if len(columns) == 1:
                throw(
                    DBSchemaError,
                    "Index for column %r already exists" % columns[0].name,
                )
            else:
                throw(
                    DBSchemaError,
                    "Index for columns (%s) already exists"
                    % ", ".join(repr(column.name) for column in columns),
                )
        if is_pk:
            if table.pk_index is not None:
                throw(
                    DBSchemaError,
                    "Primary key for table %r is already defined" % table.name,
                )
            table.pk_index = self
            if is_unique is None:
                is_unique = True
            elif not is_unique:
                throw(
                    DBSchemaError,
                    "Incompatible combination of is_unique=False and is_pk=True",
                )
        elif is_unique is None:
            is_unique = False
        schema = table.schema
        if name is not None and name in schema.names:
            throw(
                DBSchemaError,
                "Index %s cannot be created, name is already in use" % name,
            )
        Constraint.__init__(self, name, schema)
        self.is_named = is_named
        self.using = using
        self.where = where
        self.include = include
        self.nulls_not_distinct = nulls_not_distinct
        self.key_spec = key_spec
        has_special_keys = key_spec is not None and any(
            order or expr for _, order, expr in key_spec
        )
        dialect = schema.provider.dialect
        if dialect != "PostgreSQL" and (using or include or nulls_not_distinct):
            throw(
                TypeError,
                "Index options 'using', 'include' and 'nulls_not_distinct' "
                "are supported only in PostgreSQL",
            )
        if dialect not in ("PostgreSQL", "SQLite") and (
            has_expressions or has_special_keys
        ):
            throw(
                TypeError,
                "Index expressions and column order are supported only "
                "in PostgreSQL and SQLite",
            )
        if where is not None and dialect not in ("PostgreSQL", "SQLite"):
            throw(
                TypeError,
                "'where' option is supported only in PostgreSQL and SQLite",
            )
        if nulls_not_distinct and not is_unique:
            throw(
                TypeError,
                "'nulls_not_distinct' option is allowed only for unique indexes",
            )
        if (
            name is not None
            and is_pk in ("auto", "identity")
            and schema.provider.dialect == "SQLite"
        ):
            throw(
                TypeError,
                "Named autoincrement primary key is not supported in SQLite",
            )
        if is_pk == "identity" and schema.provider.dialect != "PostgreSQL":
            throw(
                TypeError,
                "'auto=\"identity\"' is supported only in PostgreSQL",
            )
        for column in columns:
            column.is_pk = column.is_pk or (len(columns) == 1 and is_pk)
            column.is_pk_part = column.is_pk_part or bool(is_pk)
            column.is_unique = column.is_unique or (is_unique and len(columns) == 1)
        table.indexes[columns] = self
        self.table = table
        self.columns = columns
        self.is_pk = is_pk
        self.is_unique = is_unique

    def exists(self, provider, connection, case_sensitive=True):
        return provider.index_exists(
            connection, self.table.name, self.name, case_sensitive
        )

    @property
    def renders_standalone(self):
        if self.is_pk:
            return False
        return (
            not self.is_unique
            or self.using
            or self.where is not None
            or self.include
            or self.key_spec is not None
            and any(order or expr for _, order, expr in self.key_spec)
        )

    def get_sql(self):
        return self._get_create_sql(inside_table=True)

    def get_create_command(self):
        return self._get_create_sql(inside_table=False)

    def _get_key_list_sql(self):
        schema = self.schema
        case = schema.case
        quote_name = schema.provider.quote_name
        if self.key_spec is None:
            return schema.column_list(self.columns)
        items = []
        for col, order, expr in self.key_spec:
            if expr is not None:
                items.append("(%s)" % expr)
            else:
                item = quote_name(col.name)
                if order:
                    item += " " + case(order)
                items.append(item)
        return "(" + ", ".join(items) + ")"

    def _get_create_sql(self, inside_table):
        schema = self.schema
        case = schema.case
        quote_name = schema.provider.quote_name
        cmd = []
        append = cmd.append
        if not inside_table:
            if self.is_pk:
                throw(
                    DBSchemaError,
                    "Primary key index cannot be defined outside of table definition",
                )
            append(case("CREATE"))
            if self.is_unique:
                append(case("UNIQUE"))
            append(case("INDEX"))
            # if schema.provider.index_if_not_exists_syntax:
            #     append(case('IF NOT EXISTS'))
            append(quote_name(self.name))
            append(case("ON"))
            append(quote_name(self.table.name))
            if self.using:
                append(case("USING"))
                append(case(self.using))
            elif self.columns:
                converter = self.columns[0].converter
                if (
                    isinstance(converter.py_type, core.Array)
                    and converter.provider.dialect == "PostgreSQL"
                ):
                    append(case("USING GIN"))
            append(self._get_key_list_sql())
            if self.include:
                append(case("INCLUDE"))
                append(schema.column_list(self.include))
            if self.where:
                append(case("WHERE"))
                append(self.where)
            if self.nulls_not_distinct:
                append(case("NULLS NOT DISTINCT"))
        else:
            if self.name:
                append(case("CONSTRAINT"))
                append(quote_name(self.name))
            if self.is_pk:
                append(case("PRIMARY KEY"))
            elif self.is_unique:
                append(case("UNIQUE"))
            else:
                append(case("INDEX"))
            if self.nulls_not_distinct:
                append(case("NULLS NOT DISTINCT"))
            append(self._get_key_list_sql())
        return " ".join(cmd)


class DBCheck(Constraint):
    typename = "Check"

    def __init__(self, name, table, sql):
        assert name is not None and sql
        Constraint.__init__(self, name, table.schema)
        self.table = table
        self.sql = sql

    def get_sql(self):
        schema = self.schema
        case = schema.case
        return "%s %s %s (%s)" % (
            case("CONSTRAINT"),
            schema.provider.quote_name(self.name),
            case("CHECK"),
            self.sql,
        )


class DBComment(DBObject):
    typename = "Comment"

    def __init__(self, schema, table_name, column_name, text):
        self.schema = schema
        self.table_name = table_name
        self.column_name = column_name
        self.text = text
        self.name = (
            (table_name, column_name) if column_name is not None else table_name
        )

    def exists(self, provider, connection, case_sensitive=True):
        return None  # COMMENT ON is idempotent: always re-apply

    def get_create_command(self):
        schema = self.schema
        case = schema.case
        quote_name = schema.provider.quote_name
        if self.column_name is None:
            kind = case("TABLE")
            target = quote_name(self.table_name)
        else:
            kind = case("COLUMN")
            target = "%s.%s" % (
                quote_name(self.table_name),
                quote_name(self.column_name),
            )
        return "%s %s %s IS '%s'" % (
            case("COMMENT ON"),
            kind,
            target,
            self.text.replace("'", "''"),
        )


class ForeignKey(Constraint):
    typename = "Foreign key"

    def __init__(
        self,
        name,
        child_table,
        child_columns,
        parent_table,
        parent_columns,
        index_name,
        on_delete,
        interleave=False,
    ):
        schema = parent_table.schema
        if schema is not child_table.schema:
            throw(
                DBSchemaError,
                "Parent and child tables of foreign_key cannot belong to different schemata",
            )
        for column in parent_columns:
            if column.table is not parent_table:
                throw(
                    DBSchemaError,
                    "Column %r does not belong to table %r"
                    % (column.name, parent_table.name),
                )
        for column in child_columns:
            if column.table is not child_table:
                throw(
                    DBSchemaError,
                    "Column %r does not belong to table %r"
                    % (column.name, child_table.name),
                )
        if len(parent_columns) != len(child_columns):
            throw(DBSchemaError, "Foreign key columns count do not match")
        if child_columns in child_table.foreign_keys:
            if len(child_columns) == 1:
                throw(
                    DBSchemaError,
                    "Foreign key for column %r already defined" % child_columns[0].name,
                )
            else:
                throw(
                    DBSchemaError,
                    "Foreign key for columns (%s) already defined"
                    % ", ".join(repr(column.name) for column in child_columns),
                )
        if name is not None and name in schema.names:
            throw(
                DBSchemaError,
                "Foreign key %s cannot be created, name is already in use" % name,
            )
        Constraint.__init__(self, name, schema)
        child_table.foreign_keys[child_columns] = self
        if child_table is not parent_table:
            child_table.parent_tables.add(parent_table)
            parent_table.child_tables.add(child_table)
        self.parent_table = parent_table
        self.parent_columns = parent_columns
        self.child_table = child_table
        self.child_columns = child_columns
        self.on_delete = on_delete
        self.interleave = interleave

        if index_name is not False:
            child_columns_len = len(child_columns)
            if all(
                columns[:child_columns_len] != child_columns
                for columns in child_table.indexes
            ):
                child_table.add_index(
                    index_name,
                    child_columns,
                    is_pk=False,
                    is_unique=False,
                    m2m=bool(child_table.m2m),
                )

    def exists(self, provider, connection, case_sensitive=True):
        return provider.fk_exists(
            connection, self.child_table.name, self.name, case_sensitive
        )

    def get_sql(self):
        return self._get_create_sql(inside_table=True)

    def get_create_command(self):
        return self._get_create_sql(inside_table=False)

    def _get_create_sql(self, inside_table):
        schema = self.schema
        case = schema.case
        quote_name = schema.provider.quote_name
        cmd = []
        append = cmd.append
        if not inside_table:
            append(case("ALTER TABLE"))
            append(quote_name(self.child_table.name))
            append(case("ADD"))
        if schema.named_foreign_keys and self.name:
            append(case("CONSTRAINT"))
            append(quote_name(self.name))
        append(case("FOREIGN KEY"))
        append(schema.column_list(self.child_columns))
        append(case("REFERENCES"))
        append(quote_name(self.parent_table.name))
        append(schema.column_list(self.parent_columns))
        if self.on_delete:
            append(case("ON DELETE %s" % self.on_delete))
        return " ".join(cmd)


DBSchema.table_class = Table
DBSchema.column_class = Column
DBSchema.index_class = DBIndex
DBSchema.check_class = DBCheck
DBSchema.fk_class = ForeignKey
