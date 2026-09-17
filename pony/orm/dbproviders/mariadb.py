"""MariaDB provider: Python-коннектор mariadb (2.0RC).

SQL-диалект совместим с MySQL — переиспользуем mysql-провайдер целиком,
меняя только dbapi-модуль и его особенности:
- paramstyle qmark (коннектор использует ?-плейсхолдеры);
- нативная конвертация типов (conv из MySQLdb не нужен);
- пул — стандартный dbapiprovider.Pool (одно соединение на поток).
"""

import mariadb as mariadb_module
from mariadb.constants import CLIENT

from pony.orm import dbapiprovider
from pony.orm.dbapiprovider import Pool, wrap_dbapi_exceptions
from pony.orm.dbproviders.mysql import MySQLBuilder, MySQLProvider
from pony.orm.sqlbuilding import SQLBuilder

NoneType = type(None)


class MariaDBBuilder(MySQLBuilder):
    """MariaDB не поддерживает CAST(... AS JSON) (JSON у него — алиас LONGTEXT).
    Семантическое сравнение JSON — через JSON_EQUALS (MariaDB 10.7+),
    сравнение с JSON-нулём — текстовое (json_extract возвращает текст),
    а «приведение параметра к JSON» — no-op (строки парсятся JSON-функциями)."""

    def _on_mariadb(self):
        return getattr(self.provider, "is_mariadb", True)

    def JSON_EQ(self, left, right):
        if not self._on_mariadb():  # MySQL: CAST(... AS JSON) валиден
            return MySQLBuilder.JSON_EQ(self, left, right)
        return "json_equals(", self(left), ", ", self(right), ")"

    def JSON_NE(self, left, right):
        if not self._on_mariadb():
            return MySQLBuilder.JSON_NE(self, left, right)
        return "NOT (", "json_equals(", self(left), ", ", self(right), "))"

    def JSON_PARAM(self, expr):
        if not self._on_mariadb():
            return MySQLBuilder.JSON_PARAM(self, expr)
        return self(expr)

    def JSON_VALUE(self, expr, path, type):
        if not self._on_mariadb():
            return MySQLBuilder.JSON_VALUE(self, expr, path, type)
        if type is bool:
            # MariaDB: json_extract возвращает текст; CAST('true' AS SIGNED) = 0,
            # поэтому булево выражаем сравнением с 'true' (даёт 1/0)
            path_sql, has_params, has_wildcards = self.build_json_path(path)
            return "(", "json_extract(", self(expr), ", ", path_sql, ") = 'true')"
        if type is NoneType:
            path_sql, has_params, has_wildcards = self.build_json_path(path)
            result = "json_extract(", self(expr), ", ", path_sql, ")"
            return "NULLIF(", result, ", 'null')"
        return super().JSON_VALUE(expr, path, type)

class MariaDBProvider(MySQLProvider):
    dialect = "MySQL"  # SQL-диалект совместим с MySQL
    paramstyle = "qmark"
    dbapi_module = mariadb_module
    sqlbuilder_cls = MariaDBBuilder
    is_mariadb = True  # уточняется при инспекции соединения (коннектор умеет и MySQL)

    @wrap_dbapi_exceptions
    def inspect_connection(self, connection):
        MySQLProvider.inspect_connection(self, connection)
        server_mariadb = getattr(connection, "server_mariadb", None)
        if server_mariadb is None:
            server_mariadb = "mariadb" in str(
                getattr(self, "server_version", "")
            ).lower()
        self.is_mariadb = bool(server_mariadb)

    def should_reconnect(self, exc):
        return isinstance(exc, mariadb_module.OperationalError) and exc.args[0] in (
            2006,
            2013,
        )

    def get_pool(self, *args, **kwargs):
        # коннектор конвертирует типы нативно: conv/charset/client_flag не нужны
        kwargs.pop("conv", None)
        kwargs.pop("charset", None)
        # как MySQLdb/MySQLProvider: rowcount = число совпавших строк (для optimistic check)
        kwargs.setdefault("client_flag", CLIENT.FOUND_ROWS)
        return Pool(mariadb_module, *args, **kwargs)


provider_cls = MariaDBProvider
