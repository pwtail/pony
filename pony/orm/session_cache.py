"""Session cache: per-session identity map, connection and transaction state.

SessionCacheGen is the single implementation, written in async style; every
I/O point goes through the ProviderOps adapter (provider.sync_ops). Two thin
facades drive it:

- SessionCache      — sync: methods are DriveGen descriptors, no loop.
- AsyncSessionCache — async: awaited normally (same module, below).

Names owned by core (core.num_counter, core.local, exceptions, ...) are
resolved lazily: core.py imports this module, so module-level attribute
access at import time would be circular.
"""

import sys
from collections import defaultdict
from contextlib import contextmanager

import pony.orm.core as core
from pony.orm.drive import DriveGen
from pony.utils import throw


class SessionCacheGen:
    is_async = False

    def _ops(self):
        provider = self.database.provider
        if self.is_async:
            return provider.async_ops
        return provider.sync_ops

    def __init__(self, database):
        self.is_alive = True
        self.num = next(core.num_counter)
        self.database = database
        self.objects = set()
        self.indexes = defaultdict(dict)
        self.seeds = defaultdict(set)
        self.max_id_cache = {}
        self.collection_statistics = {}
        self.for_update = set()
        self.noflush_counter = 0
        self.modified_collections = defaultdict(set)
        self.objects_to_save = []
        self.saved_objects = []
        self.query_results = {}
        self.dbvals_deduplication_cache = defaultdict(dict)
        self.modified = False
        self.db_session = db_session = core.local.db_session
        self.immediate = db_session is not None and db_session.immediate
        self.connection = None
        self.in_transaction = False
        self.saved_fk_state = None
        self.perm_cache = defaultdict(
            lambda: defaultdict(dict)
        )  # user -> perm -> cls_or_attr_or_obj -> bool
        self.user_roles_cache = defaultdict(dict)  # user -> obj -> roles
        self.obj_labels_cache = {}  # obj -> labels

    async def connect(self):
        assert self.connection is None
        if self.in_transaction:
            core.throw(
                core.ConnectionClosedError,
                "Transaction cannot be continued because database connection failed",
            )
        database = self.database
        provider = database.provider
        connection = await self._ops().connect(database, self)
        try:
            await self._ops().set_transaction_mode(connection, self)
        except:
            await self._ops().drop(connection, self)
            raise

        self.connection = connection
        return connection

    async def reconnect(self, exc):
        provider = self.database.provider
        if exc is not None:
            exc = getattr(exc, "original_exc", exc)
            if not provider.should_reconnect(exc):
                core.reraise(*sys.exc_info())
            if core.local.debug:
                core.log_orm("CONNECTION FAILED: %s" % exc)
            connection = self.connection
            assert connection is not None
            self.connection = None
            await self._ops().drop(connection, self)
        else:
            assert self.connection is None
        return await SessionCacheGen.connect(self)

    async def prepare_connection_for_query_execution(self):
        db_session = core.local.db_session
        if db_session is not None and self.db_session is None:
            # This situation can arise when a transaction was started
            # in the interactive mode, outside of the db_session
            if self.in_transaction or self.modified:
                core.local.db_session = None
                try:
                    await SessionCacheGen.flush_and_commit(self)
                finally:
                    core.local.db_session = db_session
            self.db_session = db_session
            self.immediate = self.immediate or db_session.immediate
        else:
            assert self.db_session is db_session, (self.db_session, db_session)
        connection = self.connection
        if connection is None:
            connection = await SessionCacheGen.connect(self)
        elif self.immediate and not self.in_transaction:
            provider = self.database.provider
            try:
                await self._ops().set_transaction_mode(connection, self)
            except Exception as e:
                connection = await SessionCacheGen.reconnect(self, e)
        if not self.noflush_counter and self.modified:
            await SessionCacheGen.flush(self)
        return connection

    async def flush_and_commit(self):
        try:
            await SessionCacheGen.flush(self)
        except:
            await SessionCacheGen.rollback(self)
            raise
        try:
            await SessionCacheGen.commit(self)
        except BaseException:
            core.transact_reraise(core.CommitException, [sys.exc_info()])

    async def commit(self):
        assert self.is_alive
        try:
            if self.modified:
                await SessionCacheGen.flush(self)
            if self.in_transaction:
                assert self.connection is not None
                await self._ops().commit(self.connection, self)
            self.for_update.clear()
            self.query_results.clear()
            self.max_id_cache.clear()
            self.immediate = True
        except:
            await SessionCacheGen.rollback(self)
            raise

    async def rollback(self):
        await SessionCacheGen.close(self, rollback=True)

    async def release(self):
        await SessionCacheGen.close(self, rollback=False)

    async def close(self, rollback=True):
        assert self.is_alive
        if not rollback:
            assert not self.in_transaction
        database = self.database
        x = core.local.db2cache.pop(database)
        assert x is self
        self.is_alive = False
        provider = database.provider
        connection = self.connection
        if connection is None:
            return
        self.connection = None

        try:
            if rollback:
                try:
                    await self._ops().rollback(connection, self)
                except:
                    await self._ops().drop(connection, self)
                    raise
            await self._ops().release(connection, self)
        finally:
            db_session = self.db_session or core.local.db_session
            if db_session and db_session.strict:
                for obj in self.objects:
                    obj._vals_ = obj._dbvals_ = obj._session_cache_ = None
                self.perm_cache = self.user_roles_cache = self.obj_labels_cache = None
            else:
                for obj in self.objects:
                    obj._dbvals_ = obj._session_cache_ = None
                    for attr, setdata in obj._vals_.items():
                        if attr.is_collection:
                            if not setdata.is_fully_loaded:
                                obj._vals_[attr] = None

            self.objects = self.objects_to_save = self.saved_objects = (
                self.query_results
            ) = self.indexes = self.seeds = self.for_update = self.max_id_cache = (
                self.modified_collections
            ) = self.collection_statistics = self.dbvals_deduplication_cache = None

    async def flush(self):
        from pony.orm.core_gen import save_gen  # lazy: core_gen imports pony.orm

        if self.noflush_counter:
            return
        assert self.is_alive
        assert not self.saved_objects
        prev_immediate = self.immediate
        self.immediate = True
        try:
            for _i in range(50):
                if not self.modified:
                    return

                with self.flush_disabled():
                    for obj in self.objects_to_save:  # can grow during iteration
                        if obj is not None:
                            obj._before_save_()

                    self.query_results.clear()
                    modified_m2m = self._calc_modified_m2m()
                    for attr, (_added, removed) in modified_m2m.items():
                        if not removed:
                            continue
                        attr.remove_m2m(removed)
                    for obj in self.objects_to_save:
                        if obj is not None:
                            await save_gen(obj)
                    for attr, (added, _removed) in modified_m2m.items():
                        if not added:
                            continue
                        attr.add_m2m(added)

                self.max_id_cache.clear()
                self.modified_collections.clear()
                self.objects_to_save[:] = ()
                self.modified = False

                self.call_after_save_hooks()
            else:
                if self.modified:
                    core.throw(
                        core.TransactionError,
                        "Recursion depth limit reached in obj._after_save_() call",
                    )
        finally:
            if not self.in_transaction:
                self.immediate = prev_immediate

    @contextmanager
    def flush_disabled(self):
        self.noflush_counter += 1
        try:
            yield
        finally:
            self.noflush_counter -= 1

    def call_after_save_hooks(self):
        saved_objects = self.saved_objects
        self.saved_objects = []
        for obj, status in saved_objects:
            obj._after_save_(status)

    def _calc_modified_m2m(self):
        modified_m2m = {}
        for attr, objects in sorted(
            self.modified_collections.items(),
            key=lambda pair: (pair[0].entity.__name__, pair[0].name),
        ):
            if not isinstance(attr, core.Set):
                core.throw(NotImplementedError)
            reverse = attr.reverse
            if not reverse.is_collection:
                for obj in objects:
                    setdata = obj._vals_[attr]
                    setdata.added = setdata.removed = setdata.absent = None
                continue

            if not isinstance(reverse, core.Set):
                core.throw(NotImplementedError)
            if reverse in modified_m2m:
                continue
            added, removed = modified_m2m.setdefault(attr, (set(), set()))
            for obj in objects:
                setdata = obj._vals_[attr]
                if setdata.added:
                    for obj2 in setdata.added:
                        added.add((obj, obj2))
                if setdata.removed:
                    for obj2 in setdata.removed:
                        removed.add((obj, obj2))
                if obj._status_ == "marked_to_delete":
                    del obj._vals_[attr]
                else:
                    setdata.added = setdata.removed = setdata.absent = None
        self.modified_collections.clear()
        return modified_m2m

    def update_simple_index(self, obj, attr, old_val, new_val, undo):
        if old_val == new_val:
            return
        cache_index = self.indexes[attr]
        if new_val is not None:
            obj2 = cache_index.setdefault(new_val, obj)
            if obj2 is not obj:
                core.throw(
                    core.CacheIndexError,
                    "Cannot update %s.%s: %s with key %s already exists"
                    % (obj.__class__.__name__, attr.name, obj2, new_val),
                )
        if old_val is core.NOT_LOADED:
            old_val = None
        if old_val is not None:
            del cache_index[old_val]
        undo.append((cache_index, old_val, new_val))

    def db_update_simple_index(self, obj, attr, old_dbval, new_dbval):
        if old_dbval == new_dbval:
            return
        cache_index = self.indexes[attr]
        if new_dbval is not None:
            obj2 = cache_index.setdefault(new_dbval, obj)
            if obj2 is not obj:
                core.throw(
                    core.TransactionIntegrityError,
                    "%s with unique index %s.%s already exists: %s"
                    % (
                        obj2.__class__.__name__,
                        obj.__class__.__name__,
                        attr.name,
                        new_dbval,
                    ),
                )
                # attribute which was created or updated lately clashes with one stored in database
        cache_index.pop(old_dbval, None)

    def update_composite_index(self, obj, attrs, prev_vals, new_vals, undo):
        if None in prev_vals:
            prev_vals = None
        if None in new_vals:
            new_vals = None
        if prev_vals is None and new_vals is None:
            return
        if prev_vals == new_vals:
            return
        cache_index = self.indexes[attrs]
        if new_vals is not None:
            obj2 = cache_index.setdefault(new_vals, obj)
            if obj2 is not obj:
                attr_names = ", ".join(attr.name for attr in attrs)
                core.throw(
                    core.CacheIndexError,
                    "Cannot update %r: composite key (%s) with value %s already exists for %r"
                    % (obj, attr_names, new_vals, obj2),
                )
        if prev_vals is not None:
            del cache_index[prev_vals]
        undo.append((cache_index, prev_vals, new_vals))

    def db_update_composite_index(self, obj, attrs, prev_vals, new_vals):
        if prev_vals == new_vals:
            return
        cache_index = self.indexes[attrs]
        if None not in new_vals:
            obj2 = cache_index.setdefault(new_vals, obj)
            if obj2 is not obj:
                key_str = ", ".join(repr(item) for item in new_vals)
                core.throw(
                    core.TransactionIntegrityError,
                    "%s with unique index (%s) already exists: %s"
                    % (
                        obj2.__class__.__name__,
                        ", ".join(attr.name for attr in attrs),
                        key_str,
                    ),
                )
        cache_index.pop(prev_vals, None)


class SessionCache(SessionCacheGen):
    """Sync-фасад SessionCacheGen: те же методы, исполняются драйвером drive()."""

    connect = DriveGen()
    reconnect = DriveGen()
    prepare_connection_for_query_execution = DriveGen()
    flush_and_commit = DriveGen()
    commit = DriveGen()
    rollback = DriveGen()
    release = DriveGen()
    close = DriveGen()
    flush = DriveGen()


# ---------------------------------------------------------------------------
# async facade and session globals (asyncio)
# ---------------------------------------------------------------------------


class AsyncSessionCache(SessionCacheGen):
    is_async = True


# ---------------------------------------------------------------------------
# async session globals (mirror core.flush / core.commit / core.rollback)
# ---------------------------------------------------------------------------


def _get_async_caches():
    return [
        cache for cache in core._get_caches() if isinstance(cache, AsyncSessionCache)
    ]


async def async_flush():
    for cache in _get_async_caches():
        await cache.flush()


async def async_commit():
    caches = core._get_caches()
    if not caches:
        return
    for cache in caches:
        if not isinstance(cache, AsyncSessionCache):
            throw(
                core.TransactionError,
                "mixing sync and async sessions in one transaction is not supported",
            )
    for cache in caches:
        await cache.flush()
    primary_cache = caches[0]
    other_caches = caches[1:]
    exceptions = []
    try:
        await primary_cache.commit()
    except BaseException:
        exceptions.append(sys.exc_info())
        for cache in other_caches:
            try:
                await cache.rollback()
            except BaseException:
                exceptions.append(sys.exc_info())
        core.transact_reraise(core.CommitException, exceptions)
    else:
        for cache in other_caches:
            try:
                await cache.commit()
            except BaseException:
                exceptions.append(sys.exc_info())
        if exceptions:
            core.transact_reraise(core.PartialCommitException, exceptions)
    finally:
        del exceptions


async def async_rollback():
    exceptions = []
    try:
        for cache in _get_async_caches():
            try:
                await cache.rollback()
            except BaseException:
                exceptions.append(sys.exc_info())
        if exceptions:
            core.transact_reraise(core.RollbackException, exceptions)
        assert not core.local.db2cache
    finally:
        del exceptions
