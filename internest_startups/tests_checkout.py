"""Merchant compliance: policy pages, footer contact details and the Fawaterak checkout."""
from unittest import mock

import httpx
from django.test import override_settings
from django.urls import reverse

from internest_core.models import PartnerProfile
from internest_core.tests_applications import ApplicationsBase

from .models import PromoCode, ProUpgradeRequest

CHECKOUT = reverse("checkout")
BUYER = {"customer_name": "Youssef Sameh", "customer_email": "pay@nilecode.io", "customer_phone": "0111 399 9387", "accept_terms": "on"}


class PolicyPageTests(ApplicationsBase):
    def test_pages_render_in_both_languages(self):
        pages = {"privacy": ("Privacy Policy", "سياسة الخصوصية"), "terms": ("Terms &amp; Conditions", "الشروط والأحكام"),
                 "refund_policy": ("Refund &amp; Cancellation Policy", "سياسة الاسترجاع والإلغاء")}
        for name, (en, ar) in pages.items():
            with self.subTest(page=name):
                url = reverse(name)
                self.assertEqual(url, {"privacy": "/privacy/", "terms": "/terms/", "refund_policy": "/refund-policy/"}[name])
                self.client.cookies["internest_lang"] = "en"
                page = self.client.get(url)
                self.assertContains(page, f"<h1>{en}</h1>", html=True)
                self.assertContains(page, "engineerthe973@gmail.com")
                self.client.cookies["internest_lang"] = "ar"
                self.assertContains(self.client.get(url), ar)
        self.client.cookies["internest_lang"] = "en"
        self.assertContains(self.client.get(reverse("refund_policy")), "7–14 business days")
        self.assertContains(self.client.get(reverse("terms")), "EGP 800 per month")

    def test_footer_links_and_contact_details(self):
        page = self.client.get(reverse("landing"))
        for marker in ('href="/privacy/"', 'href="/terms/"', 'href="/refund-policy/"', 'href="tel:+201113999387"',
                       "01113999387", 'href="mailto:engineerthe973@gmail.com"', "Cairo, Egypt"):
            self.assertContains(page, marker)
        self.assertNotContains(page, "internest.com")
        self.assertContains(self.client.get(reverse("contact_partner")), "01113999387")


class CheckoutTests(ApplicationsBase):
    def setUp(self):
        super().setUp()
        PartnerProfile.objects.filter(pk=self.partner.pk).update(country_of_registration="EG", official_phone="01113999387")
        self.client.force_login(self.founder)

    def test_page_shows_summary_prefilled_details_and_consent(self):
        page = self.client.get(CHECKOUT, {"period": "year"})
        self.assertEqual(page.status_code, 200)
        for marker in ("Order summary", "Annual plan · 12 months", "8,000 EGP", "Total due", 'name="accept_terms"',
                       'value="01113999387"', 'value="hi@nilecode.io"', 'href="/terms/"', 'href="/refund-policy/"',
                       "Confirm order"):
            self.assertContains(page, marker)
        self.assertContains(self.client.get(reverse("startup_upgrade")), f'href="{CHECKOUT}"')

    def test_consent_and_phone_are_required(self):
        page = self.client.post(CHECKOUT, {**BUYER, "accept_terms": ""})
        self.assertContains(page, "Please accept the terms and the refund policy to continue.")
        page = self.client.post(CHECKOUT, {**BUYER, "customer_phone": "12345"})
        self.assertContains(page, "Enter an Egyptian mobile number")
        self.assertFalse(ProUpgradeRequest.objects.exists())

    def test_without_fawaterak_key_the_order_is_saved_for_our_team(self):
        PromoCode.objects.create(code="AUC20", discount_percent=20)
        self.client.post(reverse("startup_upgrade"), {"action": "apply", "promo_code": "AUC20"})
        resp = self.client.post(CHECKOUT, {**BUYER, "period": "month"})
        self.assertRedirects(resp, reverse("startup_upgrade"), fetch_redirect_response=False)
        order = ProUpgradeRequest.objects.get()
        self.assertEqual((order.final_price, order.currency, order.customer_phone, order.discount_percent), (640, "EGP", "01113999387", 20))
        self.assertIsNotNone(order.terms_accepted_at)
        self.assertEqual(order.payment_url, "")

    @override_settings(FAWATERAK_API_KEY="test-key", FAWATERAK_BASE_URL="https://staging.fawaterk.com")
    def test_fawaterak_invoice_link_redirect(self):
        reply = httpx.Response(200, json={"status": "success", "data": {"url": "https://staging.fawaterk.com/link/abc", "invoiceId": 991}},
                               request=httpx.Request("POST", "https://staging.fawaterk.com/api/v2/createInvoiceLink"))
        with mock.patch("httpx.post", return_value=reply) as post:
            page = self.client.get(CHECKOUT)
            self.assertContains(page, "Pay 800 EGP with Fawaterak")
            resp = self.client.post(CHECKOUT, BUYER)
        self.assertRedirects(resp, "https://staging.fawaterk.com/link/abc", fetch_redirect_response=False)
        order = ProUpgradeRequest.objects.get()
        self.assertEqual((order.gateway_reference, order.status), ("991", "pending"))
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://staging.fawaterk.com/api/v2/createInvoiceLink")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        body = kwargs["json"]
        self.assertEqual((body["cartTotal"], body["currency"], body["customer"]["phone"]), ("800.00", "EGP", "01113999387"))
        self.assertTrue(body["redirectionUrls"]["successUrl"].endswith(reverse("checkout_return", args=[order.pk, "success"])))

        page = self.client.get(reverse("checkout_return", args=[order.pk, "success"]))
        self.assertContains(page, "confirming your payment")
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")  # never activated from the return URL

    @override_settings(FAWATERAK_API_KEY="test-key")
    def test_gateway_down_keeps_the_order(self):
        with mock.patch("httpx.post", side_effect=httpx.ConnectError("blocked")), self.assertLogs("internest_startups.fawaterak", "WARNING"):
            resp = self.client.post(CHECKOUT, BUYER, follow=True)
        self.assertContains(resp, "Online payment is unavailable right now")
        self.assertEqual(ProUpgradeRequest.objects.count(), 1)

    def test_return_page_only_for_own_orders(self):
        order = ProUpgradeRequest.objects.create(partner=self.partner, list_price=800, final_price=800, payment_url="https://x.test/p")
        self.assertContains(self.client.get(reverse("checkout_return", args=[order.pk, "fail"])), "Try the payment again")
        self.assertRedirects(self.client.get(reverse("checkout_return", args=[order.pk + 99, "fail"])), reverse("startup_upgrade"),
                             fetch_redirect_response=False)

    def test_arabic_consent_text(self):
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(CHECKOUT), "أوافق على")
