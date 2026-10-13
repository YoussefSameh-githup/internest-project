"""Fawaterak payment gateway (EGP): create a hosted invoice link for a Pro checkout.

Pro is NOT activated from the browser's return URL (it can be forged); an admin confirms the payment in the
Fawaterak dashboard and activates it with the "Payment received → activate Pro" admin action.
"""
import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


class FawaterakError(Exception):
    pass


def enabled() -> bool:
    return bool(settings.FAWATERAK_API_KEY)


def create_invoice_link(order, item_name: str, return_urls: dict) -> tuple[str, str]:
    """(payment page URL, invoice id) for `order` (a ProUpgradeRequest). Raises FawaterakError."""
    first, _sep, last = order.customer_name.partition(" ")
    amount = f"{order.final_price:.2f}"
    payload = {
        "cartTotal": amount,
        "currency": order.currency,
        "customer": {"first_name": first or order.customer_name, "last_name": last or "-", "email": order.customer_email,
                     "phone": order.customer_phone, "address": "Cairo, Egypt"},
        "redirectionUrls": {"successUrl": return_urls["success"], "failUrl": return_urls["fail"], "pendingUrl": return_urls["pending"]},
        "cartItems": [{"name": item_name, "price": amount, "quantity": "1"}],
        "payLoad": {"order_id": order.pk},
    }
    try:
        resp = httpx.post(f"{settings.FAWATERAK_BASE_URL.rstrip('/')}/api/v2/createInvoiceLink", json=payload, timeout=20,
                          headers={"Authorization": f"Bearer {settings.FAWATERAK_API_KEY}", "Content-Type": "application/json"})
        resp.raise_for_status()
        data = resp.json().get("data") or {}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Fawaterak invoice failed for order %s (%s)", order.pk, type(exc).__name__)
        raise FawaterakError(str(exc)) from exc
    url, invoice_id = data.get("url"), data.get("invoiceId") or data.get("invoice_id")
    if not url:
        raise FawaterakError("No payment URL in Fawaterak response")
    return url, str(invoice_id or "")
