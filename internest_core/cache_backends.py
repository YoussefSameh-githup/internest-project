"""Cache backends.

ThriftyFileBasedCache: Django's FileBasedCache calls _cull() on EVERY set(), and _cull() lists the whole cache
directory to count entries before deciding whether anything must go. That is O(entries) per write: the load
benchmark measured 83 ms of a 245 ms CV submission spent listing 2,808 files (the rate limiter writes on each
request), and it grows with every session and rate-limit key (seconds at 50k users, worse on a network disk).
Here the count runs on about one set() in CULL_EVERY, so the directory is still kept near MAX_ENTRIES (it can exceed
it by roughly CULL_EVERY entries between checks) without paying for a listing on every request. Redis (REDIS_URL)
remains the better choice wherever it is available.
"""
import random

from django.core.cache.backends.filebased import FileBasedCache


class ThriftyFileBasedCache(FileBasedCache):
    cull_every = 50

    def _cull(self):
        if random.random() * self.cull_every < 1:
            super()._cull()
