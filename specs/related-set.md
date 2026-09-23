---
id: pony-related-set
type: task-spec
title: Автосоздание обратного Set для one-to-many (reverse)
status: draft
references: [pony-entity-declarations]
tags: [relationships, declarative-api]
---

# Цель

Для связей **one-to-many**: если объявлен только FK-атрибут, а обратный `Set`
на другой стороне не объявлен — пони создаёт его автоматически.

```python
class Author(db.Entity):
    name = Required(str)
    # books создаётся автоматически

class Book(db.Entity):
    title = Required(str)
    author = Required(Author)                       # → Author.book_set (имя по умолчанию)
    # author = Required(Author, reverse="books")    # явное имя (существующая опция)
    # author = Required(Author, reverse="-")         # обратной стороны нет вовсе
```

Имя по умолчанию — `{имя класса, объявившего FK, в нижнем регистре}_set`
(Django-конвенция): `Book.author` → `Author.book_set`. Явное имя — существующая
опция `reverse="books"`.

# Принятые решения

1. **Только one-to-many** (решение пользователя): `Required`/`Optional` со
   ссылкой на entity. m2m и one-to-one — без изменений: обе стороны обязательны,
   как сейчас.
2. **Автосоздание при отсутствии обратной стороны**: в `_link_reverse_attrs_`,
   где сейчас «Reverse attribute for %s not found», создаётся `Set` с именем
   по умолчанию `{entity}_set` (entity — класс, объявивший FK).
3. **`reverse="books"`**: если атрибут с таким именем на другой стороне есть —
   линкуется как сейчас; если нет — создаётся с этим именем.
4. **`reverse="-"` — обратная сторона не создаётся и не ищется** (решение
   пользователя). `reverse=None` остаётся дефолтом: автодетект → автосоздание.
5. **Обе стороны объявлены** (текущий стиль) — автодетект/связывание
   не меняются; неоднозначность — ошибка с подсказкой, как сейчас.
6. **Конфликт имён**: автоимя или имя из `reverse` совпадает с существующим
   атрибутом другой сущности — ошибка.
7. **FK без обратной стороны** (`reverse="-"`): FK-констрейнт в схеме
   создаётся как обычно; ORM-каскадов нет (дефолтное поведение на удаление).
   Потребует правок в местах, где ядро рассчитывает на reverse
   (`linked()`, `validate()`, `get_columns()`, генерация FK в `generate_mapping`).
8. Автосозданный `Set` функционально неотличим от объявленного (доступ,
   присваивание, запросы, каскады по умолчанию — `cascade_delete` как
   для обычного `Set` при `Required`-FK).

# Проверено (текущее состояние, сентябрь 2026)

- Опция `reverse=` (str / Attribute) — уже есть (`core.py`, `Attribute.__init__`).
- Автосвязывание объявленных с двух сторон атрибутов — уже есть: поиск
  единственного кандидата в `_link_reverse_attrs_` (`candidates1/candidates2`),
  покрыто `test_relations_one2many`.
- Автосоздание — **нет**: при отсутствии обратной стороны ошибка
  «Reverse attribute for %s not found» (`test_diagram_attribute.py`: 154, 222);
  `reverse="<нет такого>"` — ошибка «Reverse attribute %s.%s not found».
- Неоднозначность — ошибка с подсказкой «Ambiguous reverse attribute …»
  (`test_diagram_attribute.py`: 237, 256).
- `reverse="-"` как «без обратной стороны» — нет: `kwargs.pop("reverse", None)`
  не отличает `"-"` от обычного имени — сейчас это просто строка-имя,
  а отсутствующий атрибут даёт ошибку «Reverse attribute %s.%s not found».
- `Set` в one-to-many сегодня **обязателен**: связь требует обе стороны
  (явно или автодетектом).
