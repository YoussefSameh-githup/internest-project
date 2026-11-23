"""X-Request-ID: one id per request on the response, in log records and as a Sentry tag; unsafe incoming ids ignored."""
import logging
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from internest_core import request_id
from internest_core.models import AsyncTask


class RequestIDTests(TestCase):
    def test_every_response_gets_an_id(self):
        first = self.client.get("/healthz/")["X-Request-ID"]
        second = self.client.get("/healthz/")["X-Request-ID"]
        self.assertRegex(first, r"^[0-9a-f]{16}$")
        self.assertNotEqual(first, second)

    def test_safe_incoming_id_is_kept_and_unsafe_one_replaced(self):
        self.assertEqual(self.client.get("/healthz/", HTTP_X_REQUEST_ID="nginx-abc123def")["X-Request-ID"], "nginx-abc123def")
        replaced = self.client.get("/healthz/", HTTP_X_REQUEST_ID="<script>alert(1)</script>")["X-Request-ID"]
        self.assertRegex(replaced, r"^[0-9a-f]{16}$")

    def test_log_records_carry_the_id_inside_a_request_and_a_dash_outside(self):
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
        request_id.RequestIDLogFilter().filter(record)
        self.assertEqual(record.request_id, "-")
        seen = {}

        def view(request):
            rec = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
            request_id.RequestIDLogFilter().filter(rec)
            seen["id"] = rec.request_id
            from django.http import HttpResponse
            return HttpResponse("ok")

        response = request_id.RequestIDMiddleware(view)(mock.Mock(META={}))
        self.assertEqual(seen["id"], response["X-Request-ID"])
        self.assertEqual(request_id.current(), "-")  # reset after the request

    def test_sentry_gets_the_tag(self):
        with mock.patch("sentry_sdk.set_tag") as set_tag:
            response = self.client.get("/healthz/")
        set_tag.assert_any_call("request_id", response["X-Request-ID"])


class AsyncTaskAdminTests(TestCase):
    def test_staff_can_list_tasks_but_never_see_payload_or_result(self):
        admin = User.objects.create_superuser("root", "root@example.com", "x")
        task = AsyncTask.objects.create(user=admin, kind="cv_analysis", payload={"text": "SECRET CV TEXT"},
                                        result={"facts": {"contacts": {"email": ["hidden@example.com"]}}})
        self.client.force_login(admin)
        changelist = self.client.get(reverse("admin:internest_core_asynctask_changelist"))
        self.assertEqual(changelist.status_code, 200)
        detail = self.client.get(reverse("admin:internest_core_asynctask_change", args=[task.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertNotContains(detail, "SECRET CV TEXT")
        self.assertNotContains(detail, "hidden@example.com")
