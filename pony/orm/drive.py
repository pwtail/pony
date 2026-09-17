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

    A coroutine that does not finish on the first step means the sync driver was
    given a real async coroutine (an async session reached a sync-only code
    path). Silent suspension used to produce `None` and obscure errors such as
    "object of type 'NoneType' has no len()"; now it is a loud error.
    """
    iterator = coro.__await__()
    try:
        next(iterator)
    except StopIteration as ex:
        return ex.value
    raise RuntimeError(
        "drive() got a coroutine with real await points: "
        "an async session reached a sync-only code path (see docs/async.md)"
    )


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
        delegate = getattr(obj, self.target)
        return getattr(delegate, self.name)
