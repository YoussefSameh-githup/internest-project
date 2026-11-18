"""Prices and currencies. Free = 0; Hiring Pass = EGP 500 / $29 / €27, one-time, 30 days, no renewal.

Business decisions: EGP 500 (2026-11-18), USD 29 and EUR 27 for companies outside Egypt (2026-11-19). A currency missing
from HIRING_PASS_PRICES is not sold (the page shows "contact sales"); no converted price is ever invented.

Country priority: the company's registration country (set at onboarding, locked after verification),
then the visitor's country (Cloudflare header or a GeoIP2 database when configured), then the default.
"""
import logging
from decimal import Decimal

from django.conf import settings
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

logger = logging.getLogger(__name__)

FREE_PRICE = Decimal("0")
HIRING_PASS_PRICES = {"EGP": Decimal("500"), "USD": Decimal("29"), "EUR": Decimal("27")}  # the only place it is written
EUROZONE = {"AT", "BE", "HR", "CY", "EE", "FI", "FR", "DE", "GR", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PT", "SK", "SI", "ES"}
# Billed in EUR: the whole EU, the EEA, Switzerland and the UK (not only the euro area).
EUROPE = EUROZONE | {"BG", "CZ", "DK", "HU", "PL", "RO", "SE", "IS", "LI", "NO", "CH", "GB"}
OTHER_COUNTRY = "XX"

# Registration countries offered at onboarding (ISO 3166-1 alpha-2). Anything else → "Other" (USD).
COUNTRY_CHOICES = [
    ("EG", gettext_lazy("Egypt")),
    ("SA", gettext_lazy("Saudi Arabia")), ("AE", gettext_lazy("United Arab Emirates")), ("QA", gettext_lazy("Qatar")),
    ("KW", gettext_lazy("Kuwait")), ("BH", gettext_lazy("Bahrain")), ("OM", gettext_lazy("Oman")), ("JO", gettext_lazy("Jordan")),
    ("GB", gettext_lazy("United Kingdom")), ("DE", gettext_lazy("Germany")), ("FR", gettext_lazy("France")),
    ("NL", gettext_lazy("Netherlands")), ("ES", gettext_lazy("Spain")), ("IT", gettext_lazy("Italy")),
    ("US", gettext_lazy("United States")), ("CA", gettext_lazy("Canada")),
    ("XX", gettext_lazy("Other")),
]


def currency_for(country: str, in_europe: bool = False) -> str:
    """EG → EGP; European country (or an "Other" country marked as European) → EUR; anything else → USD."""
    country = (country or "").upper()
    if country == "EG":
        return "EGP"
    if country in EUROPE or (country == OTHER_COUNTRY and in_europe):
        return "EUR"
    return "USD"


def currency_for_partner(request, partner) -> str:
    """The company's own pricing currency once it picked a registration country, else the visitor's."""
    if partner is not None and partner.country_of_registration:
        return partner.pricing_currency
    return currency_for(pricing_country(request, partner))


def gateway_for(currency: str) -> str:
    """Payment gateway configured for this currency (see PAYMENT_GATEWAYS in settings)."""
    gateways = getattr(settings, "PAYMENT_GATEWAYS", {})
    return gateways.get(currency) or gateways.get("default", "manual")


def _client_ip(request):
    """Proxy-set address only: X-Forwarded-For is client-controlled and would let a visitor pick their price."""
    from internest_core.ratelimit import client_ip

    return client_ip(request)


def visitor_country(request) -> str:
    """Country of the visitor, or "" when unknown. Never raises."""
    cf = request.META.get("HTTP_CF_IPCOUNTRY", "").upper()
    if len(cf) == 2 and cf not in {"XX", "T1"}:
        return cf
    if getattr(settings, "GEOIP_PATH", None):
        try:
            from django.contrib.gis.geoip2 import GeoIP2

            return (GeoIP2().country_code(_client_ip(request)) or "").upper()
        except Exception:  # missing database/library, private IP, unknown address
            logger.debug("GeoIP lookup failed", exc_info=True)
    return ""


def pricing_country(request, partner=None) -> str:
    registered = getattr(partner, "country_of_registration", "") if partner is not None else ""
    return registered or visitor_country(request) or getattr(settings, "DEFAULT_PRICING_COUNTRY", "EG")


def pass_quote(currency="EGP", promo=None, discount_percent=None):
    """Hiring Pass price in `currency` after a promo code, or None if the pass is not sold in that currency."""
    from decimal import ROUND_HALF_UP

    list_price = HIRING_PASS_PRICES.get(currency)
    if list_price is None:
        return None
    percent = discount_percent if discount_percent is not None else (promo.discount_percent if promo else 0)
    discount = (list_price * percent / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    final = list_price - discount
    return {
        "currency": currency, "list_price": list_price, "discount_percent": percent, "discount": discount, "final_price": final,
        "list_label": format_price(list_price, currency), "discount_label": format_price(discount, currency),
        "final_label": format_price(final, currency),
    }


def format_price(amount, currency: str) -> str:
    amount = Decimal(amount)
    amount = f"{int(amount):,}" if amount == amount.to_integral_value() else f"{amount:,.2f}"
    if currency == "EGP":
        return _("%(amount)s EGP") % {"amount": amount}
    return ("€" if currency == "EUR" else "$") + amount
