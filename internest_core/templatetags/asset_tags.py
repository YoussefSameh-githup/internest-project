"""{% asset 'css/styles.css' %} → /static/css/styles.css?v=<hash>.

The hash covers every CSS/JS file the staticfiles finders can see, computed once per process,
so each deploy busts browser caches (and stale CDN/proxy copies) automatically.
"""
import hashlib
from functools import lru_cache

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


@lru_cache(maxsize=1)
def asset_version() -> str:
    digest = hashlib.sha1()
    for finder in finders.get_finders():
        for path, storage in finder.list(ignore_patterns=None):
            if path.endswith((".css", ".js")):
                digest.update(path.encode())
                with storage.open(path) as fh:
                    digest.update(fh.read())
    return digest.hexdigest()[:10]


@register.simple_tag
def asset(path):
    return f"{static(path)}?v={asset_version()}"
