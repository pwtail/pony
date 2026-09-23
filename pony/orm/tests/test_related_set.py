import unittest

from pony.orm import *
from pony.orm.tests import db_params, teardown_database


class TestAutoReverseSet(unittest.TestCase):
    def setUp(self):
        self.db = Database(**db_params)

    def tearDown(self):
        teardown_database(self.db)

    def test_auto_created_with_default_name(self):
        db = self.db

        class Author(db.Entity):
            name = Required(str)

        class Book(db.Entity):
            title = Required(str)
            author = Required(Author)

        db.generate_mapping(create_tables=True)
        self.assertEqual(Author.book_set.reverse, Book.author)
        self.assertEqual(Book.author.reverse, Author.book_set)
        script = db.schema.generate_create_script()
        self.assertIn("ON DELETE CASCADE", script)

        with db_session:
            a = Author(name="a")
            Book(title="b", author=a)
        with db_session:
            self.assertEqual([b.title for b in Author[1].book_set], ["b"])
            self.assertEqual(Book[1].author.name, "a")

    def test_explicit_name(self):
        db = self.db

        class Author(db.Entity):
            name = Required(str)

        class Book(db.Entity):
            title = Required(str)
            author = Required(Author, reverse="books")

        db.generate_mapping(create_tables=True)
        self.assertEqual(Author.books.reverse, Book.author)
        with db_session:
            a = Author(name="a")
            Book(title="b", author=a)
        with db_session:
            self.assertEqual([b.title for b in Author[1].books], ["b"])

    def test_no_reverse(self):
        db = self.db

        class Author(db.Entity):
            name = Required(str)

        class Book(db.Entity):
            title = Required(str)
            author = Optional(Author, reverse="-")

        db.generate_mapping(create_tables=True)
        self.assertFalse(hasattr(Author, "book_set"))
        script = db.schema.generate_create_script()
        self.assertIn("ON DELETE SET NULL", script)
        self.assertNotIn("ON DELETE CASCADE", script)

        with db_session:
            a = Author(name="a")
            Book(title="b", author=a)
        with db_session:
            self.assertEqual(Book[1].author.name, "a")
            self.assertFalse(hasattr(Book[1], "author_set"))

    def test_cascade_delete_default(self):
        db = self.db

        class Author(db.Entity):
            name = Required(str)

        class Book(db.Entity):
            author = Required(Author)

        db.generate_mapping(create_tables=True)
        with db_session:
            a = Author(name="a")
            Book(author=a)
        with db_session:
            Author[1].delete()
        with db_session:
            self.assertEqual(Book.select().count(), 0)

    def test_name_conflict(self):
        db = self.db

        class Author(db.Entity):
            name = Required(str)
            book_set = Required(str)

        class Book(db.Entity):
            author = Required(Author)

        with self.assertRaises(ERDiagramError):
            db.generate_mapping(check_tables=False)

    def test_explicit_name_conflict_with_non_attribute(self):
        db = self.db

        class Author(db.Entity):
            name = Required(str)

            def books(self):
                return 42

        class Book(db.Entity):
            author = Required(Author, reverse="books")

        with self.assertRaises(ERDiagramError):
            db.generate_mapping(check_tables=False)

    def test_self_reference(self):
        db = self.db

        class Category(db.Entity):
            name = Required(str)
            parent = Optional("Category")

        db.generate_mapping(create_tables=True)
        with db_session:
            c1 = Category(name="root")
            Category(name="child", parent=c1)
        with db_session:
            self.assertEqual(
                [c.name for c in Category[1].category_set], ["child"]
            )

    def test_m2m_still_requires_reverse(self):
        db = self.db

        class A(db.Entity):
            name = Required(str)
            bs = Set("B")

        class B(db.Entity):
            name = Required(str)

        with self.assertRaises(ERDiagramError):
            db.generate_mapping(check_tables=False)

    def test_dash_on_collection_rejected(self):
        db = self.db

        class A(db.Entity):
            name = Required(str)
            bs = Set("B", reverse="-")

        class B(db.Entity):
            name = Required(str)

        with self.assertRaises(ERDiagramError):
            db.generate_mapping(check_tables=False)

    def test_fk_without_reverse_queries(self):
        db = self.db

        class Author(db.Entity):
            name = Required(str)

        class Book(db.Entity):
            title = Required(str)
            author = Optional(Author, reverse="-")

        db.generate_mapping(create_tables=True)
        with db_session:
            a1 = Author(name="a1")
            a2 = Author(name="a2")
            Book(title="b1", author=a1)
            Book(title="b2", author=a2)
            Book(title="b3", author=a2)

        with db_session:
            a2 = Author[2]
            self.assertEqual(
                select(b.title for b in Book if b.author == a2)[:],
                ["b2", "b3"],
            )
            self.assertEqual(
                select(b.author.name for b in Book if b.title == "b1")[:],
                ["a1"],
            )
            self.assertEqual(
                set(select((b.title, b.author.name) for b in Book)[:]),
                {("b1", "a1"), ("b2", "a2"), ("b3", "a2")},
            )


if __name__ == "__main__":
    unittest.main()
