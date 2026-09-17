"""Sync driver for the Gen implementation: `drive()` and the `DriveGen` descriptor.

The Gen implementation (SessionCacheGen and the `*_gen` functions) is written
in async style. Sync classes expose it through the DriveGen descriptor:

    class SessionCache(SessionCacheGen):
        connect = DriveGen()  # sync wrapper over SessionCacheGen.connect

`drive(coro)` runs a coroutine to completion without an event loop: every await
inside must complete synchronously (all I/O goes through ops.SyncOps).
"""

from functools import wraps


def drive(coro):
    """Run a coroutine to completion without an event loop.

    All awaits inside must complete synchronously (I/O only through
    ProviderOps); this is the sync driver for Gen-classes.
    """
    try:
        next(coro.__await__())
    except StopIteration as ex:
        return ex.value


class DriveGen:
    """Descriptor: exposes the parent's async Gen method as a sync one.

    `connect = DriveGen()` makes `instance.connect(...)` perform
    `drive(GenParent.connect(instance, ...))`. The implementation is looked up
    in the MRO *after* the class where the descriptor is defined — so the
    descriptor never re-enters itself.
    """

    def __init__(self):
        self.owner = None
        self.name = None
        self._methods = {}

    def __set_name__(self, owner, name):
        self.owner = owner
        self.name = name

    def _find_method(self, cls):
        mro = cls.__mro__
        try:
            start = mro.index(self.owner) + 1
        except ValueError:  # pragma: no cover
            start = 1
        for base in mro[start:]:
            # берём функцию прямо из __dict__, не задевая дескрипторы
            method = base.__dict__.get(self.name)
            if method is not None:
                return method
        raise AttributeError(  # pragma: no cover
            "Async implementation of %s.%s not found" % (cls.__name__, self.name)
        )

    def __get__(self, obj, cls=None):
        if obj is None:
            return self
        cls = type(obj)
        method = self._methods.get(cls)
        if method is None:
            method = self._methods[cls] = self._find_method(cls)

        @wraps(method)
        def wrapper(*args, **kwargs):
            return drive(method(obj, *args, **kwargs))

        return wrapper
