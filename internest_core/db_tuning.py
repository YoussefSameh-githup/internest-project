"""SQLite tuning for concurrent web workers (used by settings.py; pure Python, safe to import before Django setup).

WAL lets readers work while one writer commits and `synchronous=NORMAL` is then still crash-safe; both need a local
disk, so they are skipped on network filesystems (PythonAnywhere), where SQLite's own docs require rollback mode.
busy_timeout makes a worker wait for a lock instead of failing with "database is locked".

transaction_mode IMMEDIATE: Django's atomic() otherwise opens a DEFERRED transaction that starts as a reader and
upgrades to a writer at its first write. In WAL mode that upgrade fails at once with "database is locked" (no
busy_timeout wait) whenever another process committed in between; measured under the load benchmark with two queue
workers. IMMEDIATE takes the write lock at BEGIN, so concurrent writers queue on busy_timeout instead of failing.
"""

BASE_PRAGMAS = ("PRAGMA cache_size=-64000", "PRAGMA temp_store=MEMORY", "PRAGMA foreign_keys=ON", "PRAGMA busy_timeout=10000")
WAL_PRAGMAS = ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL", "PRAGMA mmap_size=268435456")


def sqlite_pragmas(wal: bool) -> list[str]:
    return list(WAL_PRAGMAS if wal else ()) + list(BASE_PRAGMAS)


def sqlite_options(wal: bool) -> dict:
    return {"timeout": 20, "init_command": ";".join(sqlite_pragmas(wal)), "transaction_mode": "IMMEDIATE"}
