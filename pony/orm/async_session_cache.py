"""Async session cache: the asyncio facade over SessionCacheGen.

The single implementation lives in session_cache.SessionCacheGen; this class
only marks the async mode. All methods are awaited normally.
"""

from pony.orm.session_cache import SessionCacheGen


class AsyncSessionCache(SessionCacheGen):
    is_async = True
