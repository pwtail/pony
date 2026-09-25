import os
import unittest

try:
    import mariadb as mariadb_module
except ImportError:
    mariadb_module = None

from pony.orm import (
    Database,
    Optional,
    Required,
    Set,
    commit,
    db_session,
    rollback,
    select,
)

MDB = dict(
    user=os.environ.get("PONY_MARIADB_USER", "ponytest"),
    password=os.environ.get("PONY_MARIADB_PASSWORD", "ponytest"),
    host=os.environ.get("PONY_MARIADB_HOST", "127.0.0.1"),
    port=int(os.environ.get("PONY_MARIADB_PORT", "3306")),
    database=os.environ.get("PONY_MARIADB_DB", "pony_mariadb_test"),
)


def _mariadb_available():
    if mariadb_module is None:
        return False
    try:
        conn = mariadb_module.connect(**MDB)
    except Exception:
        return False
    else:
        conn.close()
        return True


@unittest.skipUnless(
    _mariadb_available(), "MariaDB 'pony_mariadb_test' is not available"
)
class TestMariaDBSync(unittest.TestCase):
    """Sync-режим провайдера mariadb (dbapi20, qmark)."""

    @classmethod
    def setUpClass(cls):
        cls.db = db = Database("mariadb", **MDB)

        class Dept(db.Entity):
            _table_ = "maria_sync_dept"
            name = Required(str)
            persons = Set("Person")

        class Person(db.Entity):
            _table_ = "maria_sync_person"
            name = Required(str)
            age = Required(int)
            dept = Optional(Dept)

        cls.Dept, cls.Person = Dept, Person
        db.generate_mapping(create_tables=True)

    def setUp(self):
        conn = mariadb_module.connect(**MDB)
        cur = conn.cursor()
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        cur.execute("TRUNCATE maria_sync_person")
        cur.execute("TRUNCATE maria_sync_dept")
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        conn.commit()
        conn.close()

    def test_create_and_select(self):
        with db_session:
            d = self.Dept(name="IT")
            self.Person(name="ann", age=30, dept=d)
            self.Person(name="bob", age=17, dept=d)
            commit()
        with db_session:
            objs = select(x for x in self.Person).order_by(self.Person.id)
            self.assertEqual([x.name for x in objs], ["ann", "bob"])

    def test_filter_and_update(self):
        with db_session:
            d = self.Dept(name="IT")
            self.Person(name="ann", age=30, dept=d)
            self.Person(name="bob", age=17, dept=d)
            commit()
        with db_session:
            adults = select(x for x in self.Person if x.age > 18)
            self.assertEqual([x.name for x in adults], ["ann"])
            bob = self.Person.get(name="bob")
            bob.age = 20
            commit()
        with db_session:
            adults = select(x for x in self.Person if x.age > 18)
            self.assertEqual(sorted(x.name for x in adults), ["ann", "bob"])

    def test_collection_load(self):
        with db_session:
            d = self.Dept(name="IT")
            self.Person(name="ann", age=30, dept=d)
            self.Person(name="bob", age=17, dept=d)
            commit()
        with db_session:
            d = self.Dept.get(name="IT")
            self.assertEqual(sorted(p.name for p in d.persons), ["ann", "bob"])

    def test_delete_and_rollback(self):
        with db_session:
            d = self.Dept(name="IT")
            self.Person(name="ann", age=30, dept=d)
            commit()
        with db_session:
            ann = self.Person.get(name="ann")
            ann.delete()
            rollback()
        with db_session:
            objs = select(x for x in self.Person)
            self.assertEqual([x.name for x in objs], ["ann"])


if __name__ == "__main__":
    unittest.main()
