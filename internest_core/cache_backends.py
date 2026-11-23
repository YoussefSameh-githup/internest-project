"""Cache backends.

ThriftyFileBasedCache: Django's FileBasedCache calls _cull() on EVERY set(), and _cull() lists the whole cache
directory to count entries before deciding whether anything must go. That is O(entries) per write: the load
benchmark measured 83 ms of a 245 ms CV submission spent listing 2,808 files (the rate limiter writes on each
request), and it grows with every session and rate-limit key (seconds at 50k users, worse on a network disk).
Here the count runs on about one set() in CULL_EVERY, so the directory is still kept near MAX_ENTRIES (it can exceed
it by roughly CULL_EVERY entries between checks) without paying for a listing on every request. Redis (REDIS_URL)
remains the better choice wherever it is available.

It also treats a file another request holds open as a cache miss instead of an error. On Windows, deleting or
replacing a cache file while another thread reads it raises PermissionError (WinError 32 / errno 13); the load
benchmark hit it on the public landing page (2 of 10 concurrent requests answered 500). A cache must never take a
page down: a busy entry reads as a miss, and a write that loses the race is dropped (the next request writes it).
POSIX hosts (PythonAnywhere, the Azure VM) allow both operations, so behaviour there is unchanged.
"""
import logging
import random

from django.core.cache.backends.base import DEFAULT_TIMEOUT
from django.core.cache.backends.filebased import FileBasedCache


logger = logging.getLogger(__name__)


class ThriftyFileBasedCache(FileBasedCache):
    cull_every = 50

    def _cull(self):
        if random.random() * self.cull_every < 1:
            super()._cull()

    def get(self, key, default=None, version=None):
        try:
            return super().get(key, default, version)
        except PermissionError:
            return default

    def has_key(self, key, version=None):
        try:
            return super().has_key(key, version)
        except PermissionError:
            return False

    def set(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        try:
            super().set(key, value, timeout, version)
        except PermissionError:
            logger.debug("cache write for %s lost a file race; skipped", key)

    def touch(self, key, timeout=DEFAULT_TIMEOUT, version=None):
        try:
            return super().touch(key, timeout, version)
        except PermissionError:
            return False

    def _delete(self, fname):
        try:
            return super()._delete(fname)
        except PermissionError:
            return False  # still open elsewhere; it is expired or culled on a later pass
