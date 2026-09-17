"""Мост между sync- и async-миром: `drive()` и `Delegate`.

Логика сессии (SessionCacheGen) и остальные `*_gen`-функции написаны в async
стиле; состояние сессии живёт в AbstractSessionCache. Режимные классы
вызывают Gen-методы по-своему:

    class SyncSessionCache(AbstractSessionCache):
        def connect(self):               # drive(self._gen.connect())
            return drive(self._gen.connect())

    class AsyncSessionCache(AbstractSessionCache):
        connect = Delegate('_gen')       # await self._gen.connect()

`drive(coro)` прокручивает корутину до конца без event loop: каждый await
внутри обязан завершиться синхронно (весь I/O идёт через ops.SyncOps).
"""


def drive(coro):
    """Run a coroutine to completion without an event loop.

    All awaits inside must complete synchronously (I/O only through
    ProviderOps); this is the sync driver for Gen-classes.
    """
    try:
        next(coro.__await__())
    except StopIteration as ex:
        return ex.value


class Delegate:
    """Дескриптор: Gen-метод как async-метод кэша.

    `connect = Delegate('_gen')` делает `cache.connect(...)` эквивалентом
    `cache._gen.connect(...)` — вызывающий сам ожидает корутину.
    """

    def __init__(self, target):
        self.target = target

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, cls=None):
        if obj is None:
            return self
        return getattr(getattr(obj, self.target), self.name)
