"""Crawler, uptime and ops endpoints: /robots.txt, /sitemap.xml, /healthz/, /ops/sentry-check/."""
from xml.sax.saxutils import escape

from django.db import DatabaseError, connection
from django.http import Http404, HttpResponse, JsonResponse
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import Internship
from .seo import PRIVATE_PREFIXES, PUBLIC_PAGES, absolute


@require_GET
def robots_txt(request):
    lines = ["User-agent: *", "Allow: /", *(f"Disallow: {prefix}" for prefix in PRIVATE_PREFIXES), "",
             f"Sitemap: {absolute(request, reverse('sitemap'))}"]
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain; charset=utf-8")


@require_GET
def sitemap_xml(request):
    """Lightweight sitemap: the public pages plus every open opportunity."""
    urls = [(absolute(request, reverse(name)), None) for name in PUBLIC_PAGES]
    for pk, created in Internship.objects.open().values_list("pk", "created_at"):
        urls.append((absolute(request, reverse("internship_detail", args=[pk])), created.date().isoformat()))
    body = "".join(f"<url><loc>{escape(loc)}</loc>{f'<lastmod>{last}</lastmod>' if last else ''}</url>" for loc, last in urls)
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>\n'
    return HttpResponse(xml, content_type="application/xml; charset=utf-8")


@require_GET
@never_cache
def healthz(request):
    """Uptime monitors: 200 {"status": "ok"} when the app and its database answer, else 503."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        return JsonResponse({"status": "error", "database": "down"}, status=503)
    return JsonResponse({"status": "ok", "database": "ok"})


@require_GET
@never_cache
def sentry_check(request):
    """Superusers only: send one test error from THIS web worker and show whether Sentry stored it.

    The real end-to-end check on PythonAnywhere (`manage.py sentry_test` runs in a console, where threads work)."""
    if not request.user.is_superuser:
        raise Http404
    from .observability import send_check

    return JsonResponse(send_check("/ops/sentry-check/"))
