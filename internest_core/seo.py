"""Search-engine signals in one place: canonical origin, per-page descriptions, index rules and JSON-LD.

- Canonical/og URLs use settings.SITE_URL (https://internest.app in production), never the Host header, so www,
  the bare IP or a proxy hostname can never become a duplicate "canonical" copy of a page.
- Only the PUBLIC pages below are meant for search engines. Everything signed-in, personal (CV, certificates,
  experience letters) or transactional answers `X-Robots-Tag: noindex` (SEOHeadersMiddleware), HTML pages also carry
  <meta name="robots" content="noindex">, and robots.txt keeps crawlers out of the private areas (site_views).
- Structured data states only what the product really has: no ratings, reviews, addresses or social profiles that
  do not exist. JobPosting is emitted only for an OPEN opportunity, from its own fields.
"""
import json

from django.conf import settings
from django.urls import reverse
from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _

# Areas behind a login or an API: robots.txt Disallow + noindex.
PRIVATE_PREFIXES = ("/admin/", "/accounts/", "/app/", "/apply/", "/partner/", "/profile/", "/student/", "/startups/", "/api/",
                    "/media/", "/i18n/", "/ops/", "/oauth/", "/staff/", "/checkout/", "/home/")
# Public on purpose (shareable proof for employers) but personal: crawlable, never indexed.
PERSONAL_PREFIXES = ("/verify/", "/courses/certificate/", "/notices/")
NOINDEX_PREFIXES = PRIVATE_PREFIXES + PERSONAL_PREFIXES

# Indexable pages (URL names), in sitemap order. Open opportunities are added by the sitemap itself.
PUBLIC_PAGES = ("landing", "university_landing", "trust", "contact", "privacy", "terms", "refund_policy")

DESCRIPTIONS = {
    "landing": _("Internest helps university students in Egypt find internships, gigs and part-time roles with startups, "
                 "prove their skills with short AI-powered tests and earn verified experience badges. Free for students."),
    "university_landing": _("Internest for universities: real internship opportunities with startups for your students, "
                            "a skills report per cohort and provable employability outcomes."),
    "trust": _("Why you can trust Internest: how skills are verified, how student data is protected, how companies are "
               "checked and how payments and refunds work."),
    "contact": _("Contact Internest support for students, startups and universities: account help, payments, partnerships "
                 "and data requests."),
    "privacy": _("How Internest collects, uses and protects student and company data, how long it is kept and how to "
                 "download or delete your data."),
    "terms": _("The terms for using Internest as a student, a startup or a university partner."),
    "refund_policy": _("Internest refund and availability policy for company plans, opportunity passes and paid courses."),
}

BRAND = "Internest"
# Real ways people write the name (spacing / capitalisation), declared once for search engines. Not keyword stuffing:
# schema.org alternateName exists exactly for this.
ALTERNATE_NAMES = ["InterNest", "Inter Nest", "Internest App"]
OG_LOCALES = {"ar": "ar_EG", "en": "en_US"}


def site_url() -> str:
    return (getattr(settings, "SITE_URL", "") or "").rstrip("/")


def absolute(request, path: str) -> str:
    base = site_url()
    return f"{base}{path}" if base else request.build_absolute_uri(path)


def is_noindex_path(path: str) -> bool:
    return path.startswith(NOINDEX_PREFIXES)


def ld_json(data) -> str:
    """JSON-LD safe to drop into <script>: no '</script>' or HTML-significant characters survive."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def organization(request) -> dict:
    from django.templatetags.static import static

    home = absolute(request, "/")
    return {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "Organization", "@id": f"{home}#organization", "name": BRAND, "alternateName": ALTERNATE_NAMES, "url": home,
             "logo": {"@type": "ImageObject", "url": absolute(request, static("img/brand/icon-512.png")), "width": 512, "height": 512},
             "description": str(DESCRIPTIONS["landing"]), "email": settings.SUPPORT_EMAIL,
             "areaServed": {"@type": "Country", "name": "Egypt"}},
            {"@type": "WebSite", "@id": f"{home}#website", "name": BRAND, "alternateName": ALTERNATE_NAMES, "url": home,
             "inLanguage": ["ar", "en"], "publisher": {"@id": f"{home}#organization"}},
        ],
    }


def job_posting(request, internship) -> dict | None:
    """schema.org JobPosting for an open opportunity (Google for Jobs). None once it is closed or past its deadline."""
    if internship.status != internship.STATUS_OPEN:
        return None
    partner = internship.partner
    org = {"@type": "Organization", "name": partner.company_name}
    if partner.official_website:
        org["sameAs"] = partner.official_website
    if partner.logo:
        org["logo"] = absolute(request, partner.logo.url)
    data = {
        "@context": "https://schema.org", "@type": "JobPosting",
        "title": internship.title, "description": internship.description,
        "datePosted": internship.created_at.date().isoformat(), "validThrough": f"{internship.deadline.isoformat()}T23:59:59",
        "employmentType": "INTERN", "hiringOrganization": org,
        "url": absolute(request, reverse("internship_detail", args=[internship.pk])),
        "identifier": {"@type": "PropertyValue", "name": BRAND, "value": str(internship.pk)},
        "directApply": True,
    }
    if internship.is_online:
        data["jobLocationType"] = "TELECOMMUTE"
        data["applicantLocationRequirements"] = {"@type": "Country", "name": "Egypt"}
    else:
        data["jobLocation"] = {"@type": "Place", "address": {"@type": "PostalAddress", "addressLocality": internship.location,
                                                              "addressCountry": "EG"}}
    return data


def seo(request):
    """Context processor: canonical URL, description, robots and JSON-LD for internship/base.html."""
    match = getattr(request, "resolver_match", None)
    name = match.url_name if match else None
    path = request.path
    ctx = {
        "canonical_url": absolute(request, path),
        "site_origin": site_url() or request.build_absolute_uri("/").rstrip("/"),
        "og_locale": OG_LOCALES.get((get_language() or "ar")[:2], "ar_EG"),
        "robots_noindex": is_noindex_path(path) or request.user.is_authenticated,
        "structured_data": [ld_json(organization(request))] if name == "landing" else [],
    }
    if name in DESCRIPTIONS:
        ctx["seo_description"] = DESCRIPTIONS[name]
    return ctx


class SEOHeadersMiddleware:
    """X-Robots-Tag on everything that must not be indexed, including PDFs and JSON that carry no <meta>."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        user = getattr(request, "user", None)
        if is_noindex_path(request.path) or (user is not None and user.is_authenticated) or response.status_code >= 400:
            response.setdefault("X-Robots-Tag", "noindex, nofollow")
        return response
