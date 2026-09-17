import time
from itertools import starmap
from operator import attrgetter

from pony.orm import core
from pony.orm.ops import ops_for
from pony.orm.session_cache import AbstractSessionCache
from pony.utils import throw

# ВАЖНО: при импорте core_gen обращается только к объекту модуля core (не к его
# атрибутам) — core импортирует core_gen, и обращение к ещё не определённому
# имени сломало бы импорт. throw берём напрямую из pony.utils (тот же объект,
# что core.throw), остальные core.<имя> читаются во время вызова.


async def exec_sql_gen(
    database, sql, arguments=None, returning_id=False, start_transaction=False
):
    cache = database._get_cache()
    if not isinstance(cache, AbstractSessionCache):
        throw(
            core.TransactionError,
            "exec_sql_gen() requires a pony session cache",
        )
    if start_transaction:
        cache.immediate = True
    connection = await cache._gen.prepare_connection_for_query_execution()
    cursor = connection.cursor()
    if core.local.debug:
        core.log_sql(sql, arguments)
    provider = database.provider
    ops = provider.async_ops if cache.is_async else provider.sync_ops
    t = time.time()
    try:
        new_id = await ops.execute(cursor, sql, arguments, returning_id)
    except Exception as e:
        connection = await cache._gen.reconnect(e)
        cursor = connection.cursor()
        if core.local.debug:
            core.log_sql(sql, arguments)
        t = time.time()
        new_id = await ops.execute(cursor, sql, arguments, returning_id)
    if cache.immediate:
        cache.in_transaction = True
    database._update_local_stat(sql, t)
    if not returning_id:
        return cursor
    return new_id


async def add_m2m_gen(attr, added):
    """Исполнение m2m-вставок (sync и async): SQL строит sync-хелпер атрибута."""
    database = attr.entity._database_
    sql, arguments_list = attr._m2m_add_sql_and_arguments(added)
    await exec_sql_gen(database, sql, arguments_list)


async def remove_m2m_gen(attr, removed):
    """Исполнение m2m-удалений (sync и async)."""
    database = attr.entity._database_
    sql, arguments_list = attr._m2m_remove_sql_and_arguments(removed)
    await exec_sql_gen(database, sql, arguments_list)


async def save_created_gen(obj):
    auto_pk = obj._pkval_ is None
    attrs = []
    values = []
    new_dbvals = {}
    for attr in obj._attrs_with_columns_:
        if auto_pk and attr.is_pk:
            continue
        val = obj._vals_[attr]
        if val is not None:
            attrs.append(attr)
            if not attr.reverse:
                assert len(attr.converters) == 1
                dbval = attr.converters[0].val2dbval(val, obj)
                new_dbvals[attr] = dbval
                values.append(dbval)
            else:
                new_dbvals[attr] = val
                values.extend(attr.get_raw_values(val))
    attrs = tuple(attrs)

    database = obj._database_
    cached_sql = obj._insert_sql_cache_.get(attrs)
    if cached_sql is None:
        columns = []
        converters = []
        for attr in attrs:
            columns.extend(attr.columns)
            converters.extend(attr.converters)
        assert len(columns) == len(converters)
        params = [
            ["PARAM", (i, None, None), converter]
            for i, converter in enumerate(converters)
        ]
        entity = obj.__class__
        if not columns and database.provider.dialect == "Oracle":
            sql_ast = [
                "INSERT",
                entity._table_,
                obj._pk_columns_,
                [["DEFAULT"] for column in obj._pk_columns_],
            ]
        else:
            sql_ast = ["INSERT", entity._table_, columns, params]
        if auto_pk:
            sql_ast.append(entity._pk_columns_[0])
        sql, adapter = database._ast2sql(sql_ast)
        entity._insert_sql_cache_[attrs] = sql, adapter
    else:
        sql, adapter = cached_sql

    arguments = adapter(values)
    try:
        if auto_pk:
            new_id = await exec_sql_gen(
                database, sql, arguments, returning_id=True, start_transaction=True
            )
        else:
            await exec_sql_gen(database, sql, arguments, start_transaction=True)
    except core.IntegrityError as e:
        msg = " ".join(core.tostring(arg) for arg in e.args)
        throw(
            core.TransactionIntegrityError,
            "Object %r cannot be stored in the database. %s: %s"
            % (obj, e.__class__.__name__, msg),
            e,
        )
    except core.DatabaseError as e:
        msg = " ".join(core.tostring(arg) for arg in e.args)
        throw(
            core.UnexpectedError,
            "Object %r cannot be stored in the database. %s: %s"
            % (obj, e.__class__.__name__, msg),
            e,
        )

    if auto_pk:
        pk_attrs = obj._pk_attrs_
        cache_index = obj._session_cache_.indexes[pk_attrs]
        obj2 = cache_index.setdefault(new_id, obj)
        if obj2 is not obj:
            throw(
                core.TransactionIntegrityError,
                "Newly auto-generated id value %s was already used in transaction cache for another object"
                % new_id,
            )
        obj._pkval_ = obj._vals_[pk_attrs[0]] = new_id
        obj._newid_ = None

    obj._status_ = "inserted"
    obj._rbits_ = obj._all_bits_except_volatile_
    obj._wbits_ = 0
    obj._update_dbvals_(True, new_dbvals)


async def save_updated_gen(obj):
    update_columns = []
    values = []
    new_dbvals = {}
    for attr in obj._attrs_with_bit_(obj._attrs_with_columns_, obj._wbits_):
        update_columns.extend(attr.columns)
        val = obj._vals_[attr]
        if not attr.reverse:
            assert len(attr.converters) == 1
            dbval = attr.converters[0].val2dbval(val, obj)
            new_dbvals[attr] = dbval
            values.append(dbval)
        else:
            new_dbvals[attr] = val
            values.extend(attr.get_raw_values(val))
    if update_columns:
        for attr in obj._pk_attrs_:
            val = obj._vals_[attr]
            values.extend(attr.get_raw_values(val))
        cache = obj._session_cache_
        optimistic_session = cache.db_session is None or cache.db_session.optimistic
        if optimistic_session and obj not in cache.for_update:
            (
                optimistic_ops,
                optimistic_columns,
                optimistic_converters,
                optimistic_values,
            ) = obj._construct_optimistic_criteria_()
            values.extend(optimistic_values)
        else:
            optimistic_columns = optimistic_converters = optimistic_ops = ()
        query_key = (
            tuple(update_columns),
            tuple(optimistic_columns),
            tuple(optimistic_ops),
        )
        database = obj._database_
        cached_sql = obj._update_sql_cache_.get(query_key)
        if cached_sql is None:
            update_converters = []
            for attr in obj._attrs_with_bit_(obj._attrs_with_columns_, obj._wbits_):
                update_converters.extend(attr.converters)
            assert len(update_columns) == len(update_converters)
            update_params = [
                ["PARAM", (i, None, None), converter]
                for i, converter in enumerate(update_converters)
            ]
            params_count = len(update_params)
            where_list = ["WHERE"]
            pk_columns = obj._pk_columns_
            pk_converters = obj._pk_converters_
            params_count = core.populate_criteria_list(
                where_list,
                pk_columns,
                pk_converters,
                core.repeat("EQ"),
                params_count,
            )
            if optimistic_columns:
                core.populate_criteria_list(
                    where_list,
                    optimistic_columns,
                    optimistic_converters,
                    optimistic_ops,
                    params_count,
                    optimistic=True,
                )
            sql_ast = [
                "UPDATE",
                obj._table_,
                list(zip(update_columns, update_params)),
                where_list,
            ]
            sql, adapter = database._ast2sql(sql_ast)
            obj._update_sql_cache_[query_key] = sql, adapter
        else:
            sql, adapter = cached_sql
        arguments = adapter(values)
        cursor = await exec_sql_gen(database, sql, arguments, start_transaction=True)
        if cursor.rowcount == 0 and cache.db_session.optimistic:
            throw(core.OptimisticCheckError, obj.find_updated_attributes())
    obj._status_ = "updated"
    obj._rbits_ |= obj._wbits_ & obj._all_bits_except_volatile_
    obj._wbits_ = 0
    obj._update_dbvals_(False, new_dbvals)


async def save_deleted_gen(obj):
    values = []
    values.extend(obj._get_raw_pkval_())
    cache = obj._session_cache_
    query_key = ()
    database = obj._database_
    cached_sql = obj._delete_sql_cache_.get(query_key)
    if cached_sql is None:
        where_list = ["WHERE"]
        core.populate_criteria_list(
            where_list, obj._pk_columns_, obj._pk_converters_, core.repeat("EQ")
        )
        from_ast = ["FROM", [None, "TABLE", obj._table_]]
        sql_ast = ["DELETE", None, from_ast, where_list]
        sql, adapter = database._ast2sql(sql_ast)
        obj.__class__._delete_sql_cache_[query_key] = sql, adapter
    else:
        sql, adapter = cached_sql
    arguments = adapter(values)
    await exec_sql_gen(database, sql, arguments, start_transaction=True)
    obj._status_ = "deleted"
    cache.indexes[obj._pk_attrs_].pop(obj._pkval_)


async def save_gen(obj, dependent_objects=None):
    status = obj._status_
    if status in ("created", "modified"):
        obj._save_principal_objects_(dependent_objects)

    if status == "created":
        await save_created_gen(obj)
    elif status == "modified":
        await save_updated_gen(obj)
    elif status == "marked_to_delete":
        await save_deleted_gen(obj)
    else:
        assert False, "save_gen() called for object %r with incorrect status %s" % (
            obj,
            status,
        )  # pragma: no cover

    assert obj._status_ in core.saved_statuses
    cache = obj._session_cache_
    assert cache is not None and cache.is_alive
    cache.saved_objects.append((obj, obj._status_))
    objects_to_save = cache.objects_to_save
    save_pos = obj._save_pos_
    if save_pos == len(objects_to_save) - 1:
        objects_to_save.pop()
    else:
        objects_to_save[save_pos] = None
    obj._save_pos_ = None


async def fetch_objects_gen(
    cls,
    cursor,
    attr_offsets,
    max_fetch_count=None,
    for_update=False,
    used_attrs=(),
):
    if max_fetch_count is None:
        max_fetch_count = core.options.MAX_FETCH_COUNT
    ops = ops_for(cls._database_.provider, cls._database_._get_cache().is_async)
    if max_fetch_count is not None:
        rows = await ops.fetchmany(cursor, max_fetch_count + 1)
        if len(rows) == max_fetch_count + 1:
            if max_fetch_count == 1:
                throw(
                    core.MultipleObjectsFoundError,
                    "Multiple objects were found. Use %s.select(...) to retrieve them"
                    % cls.__name__,
                )
            throw(
                core.TooManyObjectsFoundError,
                "Found more then pony.options.MAX_FETCH_COUNT=%d objects"
                % core.options.MAX_FETCH_COUNT,
            )
    else:
        rows = await ops.fetchall(cursor)
    objects = []
    if attr_offsets is None:
        objects = [cls._get_by_raw_pkval_(row, for_update) for row in rows]
        await load_many_gen(cls, objects)
    else:
        for row in rows:
            real_entity_subclass, pkval, avdict = cls._parse_row_(row, attr_offsets)
            obj = real_entity_subclass._get_from_identity_map_(pkval, "loaded", for_update)
            if obj._status_ in core.del_statuses:
                continue
            obj._db_set_(avdict)
            objects.append(obj)
    if used_attrs:
        cls._set_rbits(objects, used_attrs)
    return objects


async def load_many_gen(cls, objects):
    database = cls._database_
    cache = database._get_cache()
    seeds = cache.seeds[cls._pk_attrs_]
    if not seeds:
        return
    objects = {obj for obj in objects if obj in seeds}
    objects = sorted(objects, key=attrgetter("_pkval_"))
    max_batch_size = database.provider.max_params_count // len(cls._pk_columns_)
    while objects:
        batch = objects[:max_batch_size]
        objects = objects[max_batch_size:]
        sql, adapter, attr_offsets = cls._construct_batchload_sql_(len(batch))
        arguments = adapter(batch)
        cursor = await exec_sql_gen(database, sql, arguments)
        result = await fetch_objects_gen(cls, cursor, attr_offsets)
        if len(result) < len(batch):
            for obj in result:
                if obj not in batch:
                    throw(
                        core.UnrepeatableReadError,
                        "Phantom object %s disappeared" % core.safe_repr(obj),
                    )


# ---------------------------------------------------------------------------
# async query / loading API (backing the public await-interface)
# ---------------------------------------------------------------------------


async def query_fetch_gen(query, limit=None, offset=None):
    translator = query._translator
    if query._prefetch:
        throw(
            core.NotImplementedError,
            "prefetch() is not supported in async mode yet; use explicit fetch() calls",
        )
    with query._prefetch_context:
        sql, arguments, attr_offsets, query_key = (
            query._construct_sql_and_arguments(limit, offset)
        )
        database = query._database
        cache = database._get_cache()
        if not isinstance(cache, AbstractSessionCache):
            throw(
                core.TransactionError,
                "query fetch requires a pony session cache",
            )
        ops = ops_for(database.provider, cache.is_async)
        if query._for_update:
            cache.immediate = True
        await cache._gen.prepare_connection_for_query_execution()
        items = cache.query_results.get(query_key)
        if items is None:
            cursor = await exec_sql_gen(database, sql, arguments)
            if isinstance(translator.expr_type, core.EntityMeta):
                entity = translator.expr_type
                items = await fetch_objects_gen(
                    entity,
                    cursor,
                    attr_offsets,
                    for_update=query._for_update,
                    used_attrs=translator.get_used_attrs(),
                )
            elif len(translator.row_layout) == 1:
                func, slice_or_offset, src = translator.row_layout[0]
                items = list(starmap(func, await ops.fetchall(cursor)))
            else:
                rows = await ops.fetchall(cursor)
                items = [
                    tuple(
                        func(sql_row[slice_or_offset])
                        for func, slice_or_offset, src in translator.row_layout
                    )
                    for sql_row in rows
                ]
                for i, t in enumerate(translator.expr_type):
                    if isinstance(t, core.EntityMeta) and t._subclasses_:
                        await load_many_gen(t, (row[i] for row in items))
            if query_key is not None:
                cache.query_results[query_key] = items
        else:
            stats = database._dblocal.stats
            stat = stats.get(sql)
            if stat is not None:
                stat.cache_count += 1
            else:
                stats[sql] = core.QueryStat(sql)
        return items


async def load_obj_gen(obj):
    cache = obj._session_cache_
    if cache is None or not cache.is_alive:
        core.throw_db_session_is_over("load object", obj)
    if not isinstance(cache, AbstractSessionCache):
        throw(
            core.TransactionError,
            "load requires a pony session cache",
        )
    entity = obj.__class__
    database = entity._database_
    if cache is not database._get_cache():
        throw(
            core.TransactionError,
            "Object %s doesn't belong to current transaction" % core.safe_repr(obj),
        )
    seeds = cache.seeds[entity._pk_attrs_]
    max_batch_size = database.provider.max_params_count // len(entity._pk_columns_)
    objects = [obj]
    for seed in seeds:
        if len(objects) >= max_batch_size:
            break
        if seed is not obj:
            objects.append(seed)
    sql, adapter, attr_offsets = entity._construct_batchload_sql_(len(objects))
    arguments = adapter(objects)
    cursor = await exec_sql_gen(database, sql, arguments)
    objects = await fetch_objects_gen(entity, cursor, attr_offsets)
    if obj not in objects:
        throw(
            core.UnrepeatableReadError,
            "Phantom object %s disappeared" % core.safe_repr(obj),
        )
    return obj


async def load_attr_gen(obj, attr):
    cache = obj._session_cache_
    if cache is None or not cache.is_alive:
        core.throw_db_session_is_over("load attribute", obj, attr)
    if not isinstance(cache, AbstractSessionCache):
        throw(
            core.TransactionError,
            "load requires a pony session cache",
        )
    if attr in obj._vals_:
        # значение уже есть: если это seed — догружаем сам seed-объект
        val = obj._vals_[attr]
        if val is not None and val in cache.seeds[val._pk_attrs_]:
            await load_obj_gen(val)
        return val
    if not attr.columns:
        throw(
            core.NotImplementedError,
            "fetch() of reverse attributes without columns is not supported yet",
        )
    if attr.lazy:
        entity = attr.entity
        database = entity._database_
        if not attr.lazy_sql_cache:
            select_list = ["ALL"] + [
                ["COLUMN", None, column] for column in attr.columns
            ]
            from_list = ["FROM", [None, "TABLE", entity._table_]]
            pk_columns = entity._pk_columns_
            pk_converters = entity._pk_converters_
            criteria_list = [
                [
                    converter.EQ,
                    ["COLUMN", None, column],
                    ["PARAM", (i, None, None), converter],
                ]
                for i, (column, converter) in enumerate(
                    zip(pk_columns, pk_converters)
                )
            ]
            sql_ast = ["SELECT", select_list, from_list, ["WHERE"] + criteria_list]
            sql, adapter = database._ast2sql(sql_ast)
            offsets = tuple(range(len(attr.columns)))
            attr.lazy_sql_cache = sql, adapter, offsets
        else:
            sql, adapter, offsets = attr.lazy_sql_cache
        arguments = adapter(obj._get_raw_pkval_())
        cursor = await exec_sql_gen(database, sql, arguments)
        ops = ops_for(database.provider, cache.is_async)
        row = await ops.fetchone(cursor)
        dbval = attr.parse_value(row, offsets, cache.dbvals_deduplication_cache)
        attr.db_set(obj, dbval)
    else:
        await load_obj_gen(obj)
    return obj._vals_[attr]


async def load_collection_gen(obj, attr, items=None):
    cache = obj._session_cache_
    if cache is None or not cache.is_alive:
        core.throw_db_session_is_over("load collection", obj, attr)
    if not isinstance(cache, AbstractSessionCache):
        throw(
            core.TransactionError,
            "collection loading requires a pony session cache",
        )
    if obj._status_ in core.del_statuses:
        core.throw_object_was_deleted(obj)
    setdata = obj._vals_.get(attr)
    if setdata is None:
        setdata = obj._vals_[attr] = core.SetData()
    elif setdata.is_fully_loaded and not attr.is_volatile:
        return setdata
    entity = attr.entity
    reverse = attr.reverse
    rentity = reverse.entity
    database = obj._database_
    ops = ops_for(database.provider, cache.is_async)

    if items:
        if not reverse.is_collection:
            items = {item for item in items if reverse not in item._vals_}
        else:
            items = set(items)
            items -= setdata
            if setdata.removed:
                items -= setdata.removed
        if not items:
            return setdata

    if items and (attr.lazy or not setdata):
        items = list(items)
        if not reverse.is_collection:
            sql, adapter, attr_offsets = rentity._construct_batchload_sql_(len(items))
            arguments = adapter(items)
            cursor = await exec_sql_gen(database, sql, arguments)
            await fetch_objects_gen(rentity, cursor, attr_offsets)
            return setdata
        sql, adapter = attr.construct_sql_m2m(1, len(items))
        items.append(obj)
        arguments = adapter(items)
        cursor = await exec_sql_gen(database, sql, arguments)
        loaded_items = {
            rentity._get_by_raw_pkval_(row) for row in await ops.fetchall(cursor)
        }
        setdata |= loaded_items
        reverse.db_reverse_add(loaded_items, obj)
        return setdata

    # полный путь: с батчингом по nplus1_threshold (зеркало sync-версии)
    counter = cache.collection_statistics.setdefault(attr, 0)
    nplus1_threshold = attr.nplus1_threshold
    prefetching = (
        not attr.lazy
        and nplus1_threshold is not None
        and counter >= nplus1_threshold
    )

    objects = [obj]
    setdata_list = [setdata]
    if prefetching:
        pk_index = cache.indexes[entity._pk_attrs_]
        max_batch_size = database.provider.max_params_count // len(
            entity._pk_columns_
        )
        for obj2 in pk_index.values():
            if obj2 is obj:
                continue
            if obj2._status_ in core.created_or_deleted_statuses:
                continue
            setdata2 = obj2._vals_.get(attr)
            if setdata2 is None:
                setdata2 = obj2._vals_[attr] = core.SetData()
            elif setdata2.is_fully_loaded:
                continue
            objects.append(obj2)
            setdata_list.append(setdata2)
            if len(objects) >= max_batch_size:
                break

    if not reverse.is_collection:
        sql, adapter, attr_offsets = rentity._construct_batchload_sql_(
            len(objects), reverse
        )
        arguments = adapter(objects)
        cursor = await exec_sql_gen(database, sql, arguments)
        await fetch_objects_gen(rentity, cursor, attr_offsets)
    else:
        sql, adapter = attr.construct_sql_m2m(len(objects))
        arguments = adapter(objects)
        cursor = await exec_sql_gen(database, sql, arguments)
        pk_len = len(entity._pk_columns_)
        d = {}
        if len(objects) > 1:
            for row in await ops.fetchall(cursor):
                obj2 = entity._get_by_raw_pkval_(row[:pk_len])
                item = rentity._get_by_raw_pkval_(row[pk_len:])
                items = d.get(obj2)
                if items is None:
                    items = d[obj2] = set()
                items.add(item)
        else:
            d[obj] = {
                rentity._get_by_raw_pkval_(row) for row in await ops.fetchall(cursor)
            }
        for obj2, items in d.items():
            setdata2 = obj2._vals_.get(attr)
            if setdata2 is None:
                setdata2 = obj2._vals_[attr] = core.SetData()
            else:
                phantoms = setdata2 - items
                if setdata2.added:
                    phantoms -= setdata2.added
                if phantoms and not attr.is_volatile:
                    throw(
                        core.UnrepeatableReadError,
                        "Phantom object %s disappeared from collection %s.%s"
                        % (core.safe_repr(phantoms.pop()), core.safe_repr(obj2), attr.name),
                    )
            items -= setdata2
            if setdata2.removed:
                items -= setdata2.removed
            setdata2 |= items
            reverse.db_reverse_add(items, obj2)

    for setdata2 in setdata_list:
        setdata2.is_fully_loaded = True
        setdata2.absent = None
        setdata2.count = len(setdata2)
    cache.collection_statistics[attr] = counter + 1
    return setdata
