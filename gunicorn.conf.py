"""Gunicorn settings for a VPS (not used on PythonAnywhere, which runs its own uWSGI).

    # ASGI (Uvicorn workers):
    gunicorn internest_app_project.asgi:application -c gunicorn.conf.py
    # WSGI (sync/threaded workers):
    GUNICORN_WORKER_CLASS=gthread gunicorn internest_app_project.wsgi:application -c gunicorn.conf.py

Every value can be overridden with an environment variable of the same name (GUNICORN_WORKERS=8, ...).
"""
import multiprocessing
import os


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8000")
worker_class = os.getenv("GUNICORN_WORKER_CLASS", "uvicorn.workers.UvicornWorker")
# (2 x cores) + 1 is the classic sizing for I/O-bound Django; heavy AI work runs in the async task runner instead.
workers = _env_int("GUNICORN_WORKERS", multiprocessing.cpu_count() * 2 + 1)
threads = _env_int("GUNICORN_THREADS", 4)  # used by the gthread worker class
worker_connections = _env_int("GUNICORN_WORKER_CONNECTIONS", 1000)
backlog = _env_int("GUNICORN_BACKLOG", 2048)

timeout = _env_int("GUNICORN_TIMEOUT", 60)          # requests never wait on the LLM (async tasks), so 60 s is ample
graceful_timeout = _env_int("GUNICORN_GRACEFUL_TIMEOUT", 30)
keepalive = _env_int("GUNICORN_KEEPALIVE", 5)        # behind nginx: keep upstream connections warm

# Recycle workers regularly (with jitter, so they never restart together) to cap slow memory growth.
max_requests = _env_int("GUNICORN_MAX_REQUESTS", 2000)
max_requests_jitter = _env_int("GUNICORN_MAX_REQUESTS_JITTER", 200)

preload_app = os.getenv("GUNICORN_PRELOAD", "1") == "1"  # shared code pages; the HTTP pool is re-created per worker
forwarded_allow_ips = os.getenv("GUNICORN_FORWARDED_ALLOW_IPS", "127.0.0.1")
accesslog = os.getenv("GUNICORN_ACCESSLOG", "-")
errorlog = os.getenv("GUNICORN_ERRORLOG", "-")
loglevel = os.getenv("GUNICORN_LOGLEVEL", "info")


def post_fork(server, worker):
    """Database connections must never be shared across forked workers."""
    from django.db import connections

    for conn in connections.all():
        conn.close()
