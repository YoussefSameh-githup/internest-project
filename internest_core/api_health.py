"""Scheduled health check for every registered API: are the keys valid, and is the paid balance still healthy?

`python manage.py check_api_health` calls `run_health_check()`. It makes real, harmless read-only requests (list models, list
scopes, read credits) and never sends fake test alerts: an email goes out only when something is actually wrong, and then
through the same cooldown as every other alert.
"""
import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import httpx
from django.conf import settings
from django.utils import timezone

from . import api_monitor as monitor
from .api_registry import REGISTRY, TRACKING_LEDGER, TRACKING_LIVE

logger = logging.getLogger(__name__)

BALANCE_PARSERS = {
    "sendgrid_credits": lambda data: data["remain"],  # GET /v3/user/credits -> {"total": .., "used": .., "remain": ..}
}
TIMEOUT = 10
INCIDENT_RETENTION_DAYS = 90

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"


@dataclass
class Result:
    key: str
    name: str
    key_status: str      # OK / FAIL / SKIP / WARN
    key_note: str
    balance_status: str
    balance_note: str

    @property
    def problem(self) -> bool:
        return FAIL in (self.key_status, self.balance_status) or "LOW" in self.balance_note


def validate_key(spec) -> tuple[str, str]:
    """(status, note) for the provider's API key, using one harmless GET. Real problems are alerted through the monitor."""
    key = spec.api_key()
    if not key:
        return SKIP, f"not configured ({spec.env_var} is empty)"
    url = spec.validate_url()
    if not url:
        return WARN, "key present; this API has no safe validation request"
    feature = "Key validation (check_api_health)"
    try:
        response = httpx.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        monitor.report_failure(spec.key, feature, exc)
        return FAIL, f"unreachable: {type(exc).__name__}"
    status = response.status_code
    if status < 300:
        return OK, f"valid (HTTP {status})"
    if status in (401, 403) or status >= 500 or status == 429:
        monitor.report_failure(spec.key, feature, failure=monitor.classify_response(response))
        return FAIL, f"HTTP {status}" + (" - key rejected" if status in (401, 403) else "")
    return WARN, f"key present; validation endpoint answered HTTP {status} (inconclusive)"


def _threshold(row) -> Decimal:
    return row.low_balance_threshold or Decimal(str(getattr(settings, "API_LOW_BALANCE_THRESHOLD", 2.0)))


def check_balance(spec, row) -> tuple[str, str]:
    """(status, note) for a paid provider; updates the admin row and alerts when the remaining balance is low."""
    if not spec.is_paid or spec.tracking not in (TRACKING_LIVE, TRACKING_LEDGER):
        return SKIP, "free / not tracked" if not spec.is_paid else "paid, tracking off"
    row.last_checked_at = timezone.now()
    if spec.tracking == TRACKING_LIVE:
        key = spec.api_key()
        if not key:
            row.last_check_result = "no key"
            row.save(update_fields=["last_checked_at", "last_check_result"])
            return SKIP, "no key, balance not read"
        balance = monitor.check_api_balance(
            spec.key, spec.balance_url, {"Authorization": f"Bearer {key}"}, threshold=float(_threshold(row)), strict=False,
            parse=BALANCE_PARSERS.get(spec.balance_parser), timeout=TIMEOUT, feature="Balance check (check_api_health)")
        if balance is None:
            row.last_check_result = "balance not readable"
            row.save(update_fields=["last_checked_at", "last_check_result"])
            return WARN, "provider gave no readable balance (is the plan billed by credits?)"
        row.last_balance = Decimal(str(balance))
    remaining = row.remaining_balance
    if remaining is None:
        row.last_check_result = "no budget entered"
        row.save()
        return WARN, "no budget entered: set 'Initial budget' in Django admin to enable alerts"
    low = remaining <= _threshold(row)
    row.last_check_result = ("LOW: " if low else "ok: ") + f"{remaining:,.2f} {row.currency}"
    row.save()
    if low and spec.tracking == TRACKING_LEDGER:  # live mode already alerted inside check_api_balance
        monitor.send_alert(spec.key, "Balance check (check_api_health)", monitor.BALANCE, None,
                           f"Estimated remaining balance {remaining:,.2f} {row.currency} is at or below the alert threshold {_threshold(row):,.2f}.")
    return (FAIL if low else OK), f"{'LOW ' if low else ''}{remaining:,.2f} {row.currency} left (threshold {_threshold(row):,.2f})"


def run_health_check() -> list[Result]:
    from .models import APIIncident

    results = []
    for spec in REGISTRY.values():
        row = monitor.sync_provider(spec)
        key_status, key_note = validate_key(spec)
        if key_status == SKIP:
            balance_status, balance_note = SKIP, "-"
        else:
            balance_status, balance_note = check_balance(spec, row)
        results.append(Result(spec.key, spec.name, key_status, key_note, balance_status, balance_note))
    APIIncident.objects.filter(created_at__lt=timezone.now() - timedelta(days=INCIDENT_RETENTION_DAYS)).delete()
    return results
