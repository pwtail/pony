"""Session cache: per-session identity map, connection and transaction state.

Классов три:

    AbstractSessionCache  общая часть: состояние сессии + sync-хелперы,
                          создаёт SessionCacheGen(self) — логику;
    SyncSessionCache      sync-режим (is_async = False): session-методы —
                          однострочные обёртки `drive(self._gen.<метод>())`;
    AsyncSessionCache     async-режим (is_async = True): session-методы —
                          `Delegate('_gen')`, корутину ожидает вызывающий.

    cache = SyncSessionCache(database) / AsyncSessionCache(database)
    cache._gen                          # SessionCacheGen(cache) — логика

Gen-код написан один раз в async стиле; единственное место, где режимы
расходятся физически — ProviderOps (provider.sync_ops / provider.async_ops).

Names owned by core (core.num_counter, core.local, exceptions, ...) are
resolved lazily at call time: core.py imports this module, so touching those
attributes at import time would be circular.
"""

import sys
from collections import defaultdict
from contextlib import contextmanager

import pony.orm.core as core
from pony.orm.drive import Delegate, drive
from pony.utils import throw


class AbstractSessionCache:
    """Общая часть кэша сессии: состояние и sync-хелперы.

    Режим и session-методы определяют наследники: SyncSessionCache (sync,
    session-методы протягиваются драйвером drive) и AsyncSessionCache
    (async, session-методы делегируются в Gen). Логика вынесена в
    SessionCacheGen (self._gen).
    """

    def __init__(self, database):
        self.is_alive = True
        self.num = next(core.num_counter)
        self.database = database
        # выбор режима инвариантен за жизнь кэша — ops вычисляем один раз
        provider = database.provider
        self.ops = provider.async_ops if self.is_async else provider.sync_ops
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
        self._gen = SessionCacheGen(self)

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




class SessionCacheGen:
    """Логика сессии в async стиле; состояние читает через self.cache."""

    def __init__(self, cache):
        self.cache = cache

    def _ops(self):
        return self.cache.ops

    async def connect(self):
        assert self.cache.connection is None
        if self.cache.in_transaction:
            core.throw(
                core.ConnectionClosedError,
                "Transaction cannot be continued because database connection failed",
            )
        database = self.cache.database
        provider = database.provider
        connection = await self._ops().connect(database, self.cache)
        try:
            await self._ops().set_transaction_mode(connection, self.cache)
        except:
            await self._ops().drop(connection, self.cache)
            raise

        self.cache.connection = connection
        return connection

    async def reconnect(self, exc):
        provider = self.cache.database.provider
        if exc is not None:
            exc = getattr(exc, "original_exc", exc)
            if not provider.should_reconnect(exc):
                core.reraise(*sys.exc_info())
            if core.local.debug:
                core.log_orm("CONNECTION FAILED: %s" % exc)
            connection = self.cache.connection
            assert connection is not None
            self.cache.connection = None
            await self._ops().drop(connection, self.cache)
        else:
            assert self.cache.connection is None
        return await self.connect()

    async def prepare_connection_for_query_execution(self):
        db_session = core.local.db_session
        if db_session is not None and self.cache.db_session is None:
            # This situation can arise when a transaction was started
            # in the interactive mode, outside of the db_session
            if self.cache.in_transaction or self.cache.modified:
                core.local.db_session = None
                try:
                    await self.flush_and_commit()
                finally:
                    core.local.db_session = db_session
            self.cache.db_session = db_session
            self.cache.immediate = self.cache.immediate or db_session.immediate
        else:
            assert self.cache.db_session is db_session, (self.cache.db_session, db_session)
        connection = self.cache.connection
        if connection is None:
            connection = await self.connect()
        elif self.cache.immediate and not self.cache.in_transaction:
            provider = self.cache.database.provider
            try:
                await self._ops().set_transaction_mode(connection, self.cache)
            except Exception as e:
                connection = await self.reconnect(e)
        if not self.cache.noflush_counter and self.cache.modified:
            await self.flush()
        return connection

    async def flush_and_commit(self):
        try:
            await self.flush()
        except:
            await self.rollback()
            raise
        try:
            await self.commit()
        except BaseException:
            core.transact_reraise(core.CommitException, [sys.exc_info()])

    async def commit(self):
        assert self.cache.is_alive
        try:
            if self.cache.modified:
                await self.flush()
            if self.cache.in_transaction:
                assert self.cache.connection is not None
                await self._ops().commit(self.cache.connection, self.cache)
            self.cache.for_update.clear()
            self.cache.query_results.clear()
            self.cache.max_id_cache.clear()
            self.cache.immediate = True
        except:
            await self.rollback()
            raise

    async def rollback(self):
        await self.close(rollback=True)

    async def release(self):
        await self.close(rollback=False)

    async def close(self, rollback=True):
        assert self.cache.is_alive
        if not rollback:
            assert not self.cache.in_transaction
        database = self.cache.database
        x = core.local.db2cache.pop(database)
        assert x is self.cache
        self.cache.is_alive = False
        provider = database.provider
        connection = self.cache.connection
        if connection is None:
            return
        self.cache.connection = None

        try:
            if rollback:
                try:
                    await self._ops().rollback(connection, self.cache)
                except:
                    await self._ops().drop(connection, self.cache)
                    raise
            await self._ops().release(connection, self.cache)
        finally:
            db_session = self.cache.db_session or core.local.db_session
            if db_session and db_session.strict:
                for obj in self.cache.objects:
                    obj._vals_ = obj._dbvals_ = obj._session_cache_ = None
                self.cache.perm_cache = self.cache.user_roles_cache = (
                    self.cache.obj_labels_cache
                ) = None
            else:
                for obj in self.cache.objects:
                    obj._dbvals_ = obj._session_cache_ = None
                    for attr, setdata in obj._vals_.items():
                        if attr.is_collection:
                            if not setdata.is_fully_loaded:
                                obj._vals_[attr] = None

            self.cache.objects = self.cache.objects_to_save = (
                self.cache.saved_objects
            ) = self.cache.query_results = self.cache.indexes = (
                self.cache.seeds
            ) = self.cache.for_update = self.cache.max_id_cache = (
                self.cache.modified_collections
            ) = self.cache.collection_statistics = (
                self.cache.dbvals_deduplication_cache
            ) = None

    async def flush(self):
        if self.cache.noflush_counter:
            return
        assert self.cache.is_alive
        assert not self.cache.saved_objects
        prev_immediate = self.cache.immediate
        self.cache.immediate = True
        try:
            for _i in range(50):
                if not self.cache.modified:
                    return

                with self.cache.flush_disabled():
                    for obj in self.cache.objects_to_save:  # can grow during iteration
                        if obj is not None:
                            obj._before_save_()

                    self.cache.query_results.clear()
                    modified_m2m = self._calc_modified_m2m()
                    for attr, (_added, removed) in modified_m2m.items():
                        if not removed:
                            continue
                        await core.remove_m2m_gen(attr, removed)
                    for obj in self.cache.objects_to_save:
                        if obj is not None:
                            # save_gen живёт в core_gen; берём его через core:
                            # модульный импорт core -> core_gen дал бы цикл
                            await core.save_gen(obj)
                    for attr, (added, _removed) in modified_m2m.items():
                        if not added:
                            continue
                        await core.add_m2m_gen(attr, added)

                self.cache.max_id_cache.clear()
                self.cache.modified_collections.clear()
                self.cache.objects_to_save[:] = ()
                self.cache.modified = False

                self.cache.call_after_save_hooks()
            else:
                if self.cache.modified:
                    core.throw(
                        core.TransactionError,
                        "Recursion depth limit reached in obj._after_save_() call",
                    )
        finally:
            if not self.cache.in_transaction:
                self.cache.immediate = prev_immediate


    def _calc_modified_m2m(self):
        modified_m2m = {}
        for attr, objects in sorted(
            self.cache.modified_collections.items(),
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
        self.cache.modified_collections.clear()
        return modified_m2m


class SyncSessionCache(AbstractSessionCache):
    """Синхронный кэш сессии: session-методы исполняются драйвером drive()."""

    is_async = False

    def connect(self):
        return drive(self._gen.connect())

    def reconnect(self, exc):
        return drive(self._gen.reconnect(exc))

    def prepare_connection_for_query_execution(self):
        return drive(self._gen.prepare_connection_for_query_execution())

    def flush_and_commit(self):
        return drive(self._gen.flush_and_commit())

    def commit(self):
        return drive(self._gen.commit())

    def rollback(self):
        return drive(self._gen.rollback())

    def release(self):
        return drive(self._gen.release())

    def close(self, rollback=True):
        return drive(self._gen.close(rollback))

    def flush(self):
        return drive(self._gen.flush())



class AsyncSessionCache(AbstractSessionCache):
    """Асинхронный кэш сессии: то же состояние, session-методы — корутины Gen.

    Наследует состояние и sync-хелперы AbstractSessionCache; методы соединения и
    транзакций делегируются в Gen через Delegate и ожидаются вызывающим:
    `await cache.connect()`, `await cache.commit()`.
    """

    is_async = True

    connect = Delegate('_gen')
    reconnect = Delegate('_gen')
    prepare_connection_for_query_execution = Delegate('_gen')
    flush_and_commit = Delegate('_gen')
    commit = Delegate('_gen')
    rollback = Delegate('_gen')
    release = Delegate('_gen')
    close = Delegate('_gen')
    flush = Delegate('_gen')

# ---------------------------------------------------------------------------
# async session globals (mirror core.flush / core.commit / core.rollback)
# ---------------------------------------------------------------------------


def _get_async_caches():
    return [
        cache for cache in core._get_caches() if isinstance(cache, AsyncSessionCache)
    ]


async def async_flush():
    caches = core._async_caches()
    if caches is None:
        return
    for cache in caches:
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
        for cache in core._async_caches() or []:
            try:
                await cache.rollback()
            except BaseException:
                exceptions.append(sys.exc_info())
        if exceptions:
            core.transact_reraise(core.RollbackException, exceptions)
        assert not core.local.db2cache
    finally:
        del exceptions
