"""Response + query caching with group invalidation.

    @cache_public_page(ttl=600)          # anonymous GET pages: rendered once per language and deploy
    def landing_view(request): ...

    leaderboard = cached_value("campus", "leaderboard", 60, compute)   # shared aggregate, any visitor
    bump("campus")                                                      # in a post_save signal: invalidate the group

Groups are invalidated by bumping a version number that is part of every key, so invalidation is O(1) and works on
any cache backend (Redis, file, local memory). Pages are never cached for signed-in users, for non-GET requests, when
it sets cookies or when flash messages are pending. CSRF tokens are swapped for each visitor's own on a cache hit.
"""
import functools
import logging
import re

from django.core.cache import cache
from django.http import HttpResponse
from django.utils import translation

logger = logging.getLogger(__name__)


def _version(group: str) -> int:
    key = f"cachegroup:{group}"
    version = cache.get(key)
    if version is None:
        cache.add(key, 1, None)
        version = cache.get(key) or 1
    return version


def bump(group: str) -> None:
    """Invalidate every cached entry of `group` (called from model signals)."""
    key = f"cachegroup:{group}"
    try:
        cache.incr(key)
    except ValueError:  # not set yet (or evicted): start a fresh version
        cache.set(key, 2, None)
    except Exception:
        logger.exception("Could not invalidate cache group %s", group)


def cached_value(group: str, name: str, ttl: int, compute):
    key = f"cv:{group}:{_version(group)}:{name}"
    try:
        value = cache.get(key)
    except Exception:
        logger.exception("Cache read failed for %s", key)
        return compute()
    if value is None:
        value = compute()
        try:
            cache.set(key, value, ttl)
        except Exception:
            logger.exception("Cache write failed for %s", key)
    return value


def _deploy_version() -> str:
    from internest_core.templatetags.asset_tags import asset_version  # changes with every CSS/JS deploy

    return asset_version()


def _cacheable_request(request) -> bool:
    return (request.method in ("GET", "HEAD") and not request.user.is_authenticated
            and "messages" not in request.COOKIES)


def _cacheable_response(request, response) -> bool:
    return (response.status_code == 200 and not response.streaming and not response.cookies
            and not response.has_header("Set-Cookie"))


# Pages with forms (even the nav's language switcher) carry a CSRF token tied to the visitor's cookie. The stored copy
# keeps a placeholder instead, and every cache hit gets the current visitor's own fresh token (and cookie).
CSRF_PLACEHOLDER = b"__INTERNEST_CSRF_TOKEN__"
_CSRF_INPUT = re.compile(rb'name="csrfmiddlewaretoken" value="([A-Za-z0-9]{32,})"')


def _template_for_cache(request, content: bytes):
    """The content to store, or None when it cannot be shared safely."""
    if not request.META.get("CSRF_COOKIE_NEEDS_UPDATE"):
        return content
    match = _CSRF_INPUT.search(content)
    if match is None:  # the token is used somewhere we cannot recognise: never share it
        return None
    return content.replace(match.group(1), CSRF_PLACEHOLDER)


def _personalise(request, content: bytes) -> bytes:
    if CSRF_PLACEHOLDER not in content:
        return content
    from django.middleware.csrf import get_token

    return content.replace(CSRF_PLACEHOLDER, get_token(request).encode("ascii"))  # also schedules the CSRF cookie


def _cache_path(request, vary_on):
    """(path + the query params the page really uses, sorted; True when other params were sent).

    The key never holds the raw query string: `/?utm=1`, `/?x=<random>` ... would otherwise each store a copy of the
    page, so anyone could flood the cache. Unknown params (tracking tags, junk) are served the canonical copy."""
    from urllib.parse import urlencode

    kept = sorted((name, value) for name in vary_on for value in request.GET.getlist(name))
    extra = any(name not in vary_on for name in request.GET)
    return (f"{request.path}?{urlencode(kept)}" if kept else request.path), extra


def cache_public_page(ttl: int = 600, group: str = "public", vary_on=()):
    """`vary_on`: the GET params the view reads (each value gets its own copy); every other param is ignored."""
    vary_on = tuple(vary_on)

    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if not _cacheable_request(request):
                return view(request, *args, **kwargs)
            path, extra_params = _cache_path(request, vary_on)
            key = f"page:{group}:{_version(group)}:{_deploy_version()}:{translation.get_language()}:{path}"
            hit = cache.get(key)
            if hit is not None:
                content, content_type = hit
                response = HttpResponse(_personalise(request, content), content_type=content_type)
                response["X-Page-Cache"] = "HIT"
                return response
            response = view(request, *args, **kwargs)
            # A render for an unknown query string may echo it (the language switcher's `next`): never store it as
            # the canonical copy. The next plain request stores it; until then such requests just render.
            if not extra_params and _cacheable_response(request, response):
                if hasattr(response, "render") and callable(response.render) and not response.is_rendered:
                    response.render()
                stored = _template_for_cache(request, response.content)
                if stored is not None:
                    cache.set(key, (stored, response["Content-Type"]), ttl)
                    response["X-Page-Cache"] = "MISS"
            return response
        return wrapper
    return decorator
