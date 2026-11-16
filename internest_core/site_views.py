"""Crawler and uptime endpoints: /robots.txt, /sitemap.xml, /healthz/."""
from xml.sax.saxutils import escape

from django.db import DatabaseError, connection
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import Internship

# Everything behind a login or an API stays out of search engines.
PRIVATE_PREFIXES = ("/admin/", "/accounts/", "/app/", "/apply/", "/partner/", "/profile/", "/student/", "/startups/", "/api/", "/media/", "/i18n/")
# Public, indexable pages (URL names). Open opportunities are added dynamically.
PUBLIC_PAGES = ("landing", "university_landing", "list", "contact", "privacy", "terms", "refund_policy")


@require_GET
def robots_txt(request):
    lines = ["User-agent: *", "Allow: /", *(f"Disallow: {prefix}" for prefix in PRIVATE_PREFIXES), "",
             f"Sitemap: {request.build_absolute_uri(reverse('sitemap'))}"]
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain; charset=utf-8")


@require_GET
def sitemap_xml(request):
    """Lightweight sitemap: the public pages plus every open opportunity."""
    urls = [(request.build_absolute_uri(reverse(name)), None) for name in PUBLIC_PAGES]
    for pk, created in Internship.objects.open().values_list("pk", "created_at"):
        urls.append((request.build_absolute_uri(reverse("internship_detail", args=[pk])), created.date().isoformat()))
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
