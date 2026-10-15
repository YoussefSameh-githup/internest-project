"""Merchant compliance: policy pages, footer contact details and the Fawaterak checkout."""
from unittest import mock

import shutil
import tempfile

import httpx
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from internest_core.models import PartnerProfile
from internest_core.tests_applications import ApplicationsBase

from .models import PromoCode, ProUpgradeRequest

CHECKOUT = reverse("checkout")
BUYER = {"customer_name": "Youssef Sameh", "customer_email": "pay@nilecode.io", "customer_phone": "0111 399 9387", "accept_terms": "on"}
MEDIA = tempfile.mkdtemp()


def receipt(name="receipt.png"):
    return SimpleUploadedFile(name, b"\x89PNG\r\n\x1a\n" + b"0" * 64, content_type="image/png")


def paid(**extra):
    """Checkout POST for a manual transfer: buyer details + the receipt."""
    return {**BUYER, "payment_proof": receipt(), **extra}


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


@override_settings(MEDIA_ROOT=MEDIA)
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
                       "Submit receipt for verification"):
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
        resp = self.client.post(CHECKOUT, paid(period="month"))
        self.assertRedirects(resp, reverse("startup_upgrade"), fetch_redirect_response=False)
        order = ProUpgradeRequest.objects.get()
        self.assertEqual((order.status, order.gateway), (ProUpgradeRequest.STATUS_VERIFYING, "instapay"))
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


class PromoFieldTests(ApplicationsBase):
    """The promo field starts empty; a discount only applies after the buyer types a valid code and clicks Apply."""

    def setUp(self):
        super().setUp()
        PartnerProfile.objects.filter(pk=self.partner.pk).update(country_of_registration="EG")
        self.client.force_login(self.founder)

    def test_promo_input_is_empty_with_a_neutral_placeholder(self):
        page = self.client.get(reverse("startup_upgrade"))
        self.assertContains(page, 'name="promo_code" value=""')
        self.assertContains(page, 'placeholder="Enter promo code"')
        self.assertNotContains(page, "STARTUP50")  # a real code must never be shown as a hint
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("startup_upgrade")), 'placeholder="أدخل كود الخصم"')

    def test_full_price_until_a_code_is_applied(self):
        page = self.client.get(CHECKOUT, {"promo_code": "STARTUP50"})  # a code in the URL is ignored
        self.assertEqual((page.context["quote"]["list_price"], page.context["quote"]["final_price"]), (800, 800))
        self.assertEqual(page.context["quote"]["discount_percent"], 0)
        self.assertNotContains(page, "Promo code")
        self.assertContains(page, "800 EGP", count=3)  # subtotal, total due and the amount to transfer

        self.client.post(reverse("startup_upgrade"), {"action": "apply", "promo_code": "STARTUP50"})
        page = self.client.get(CHECKOUT)
        self.assertEqual(page.context["quote"]["final_price"], 400)
        self.assertContains(page, "Promo code STARTUP50")


class UpgradeNavigationAndMethodsTests(ApplicationsBase):
    def setUp(self):
        super().setUp()
        PartnerProfile.objects.filter(pk=self.partner.pk).update(country_of_registration="EG")
        self.client.force_login(self.founder)

    def test_cta_always_goes_to_checkout_with_plan(self):
        ProUpgradeRequest.objects.create(partner=self.partner, list_price=800, final_price=800)  # an old unpaid order
        page = self.client.get(reverse("startup_upgrade"), {"period": "year"})
        self.assertContains(page, f'href="{CHECKOUT}?period=year" class="btn btn--gold btn--lg btn--block" data-checkout-cta')
        self.assertNotContains(page, 'aria-disabled="true"')
        self.assertContains(page, "Pay now or change the order")
        self.assertEqual(self.client.get(f"{CHECKOUT}?period=year").context["quote"]["months"], 12)

    def test_fawaterak_methods_only_when_the_gateway_is_on(self):
        self.assertNotContains(self.client.get(CHECKOUT), "Payment methods via Fawaterak")
        with override_settings(FAWATERAK_API_KEY="test-key"):
            self.assertContains(self.client.get(CHECKOUT), "Payment methods via Fawaterak")

    def test_unpaid_order_can_be_cancelled_and_replaced(self):
        old = ProUpgradeRequest.objects.create(partner=self.partner, list_price=800, final_price=800)
        page = self.client.get(CHECKOUT)
        self.assertContains(page, "You have an unpaid order")
        self.assertContains(page, 'value="cancel_pending"')
        self.client.post(CHECKOUT, {"action": "cancel_pending"})
        old.refresh_from_db()
        self.assertEqual(old.status, ProUpgradeRequest.STATUS_CANCELLED)
        with override_settings(MEDIA_ROOT=MEDIA):
            self.client.post(CHECKOUT, paid())
        self.assertEqual(ProUpgradeRequest.objects.filter(status=ProUpgradeRequest.STATUS_VERIFYING).count(), 1)

    def test_pay_button_needs_consent_and_falls_back_to_manual(self):
        page = self.client.get(CHECKOUT)
        self.assertContains(page, "data-consent")
        self.assertContains(page, "pay.disabled = !consent.checked")
        self.assertContains(page, "Submit receipt for verification")  # no FAWATERAK_API_KEY in tests → manual transfer
        self.assertEqual(self.client.post(CHECKOUT, {**BUYER, "accept_terms": ""}).status_code, 200)
        self.assertFalse(ProUpgradeRequest.objects.exists())


@override_settings(MEDIA_ROOT=MEDIA)
class ManualPaymentTests(ApplicationsBase):
    """InstaPay (Egypt) / RedotPay (abroad) transfer + receipt upload -> Pending verification -> admin activates Pro."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        PartnerProfile.objects.filter(pk=self.partner.pk).update(country_of_registration="EG", official_phone="01113999387")
        self.client.force_login(self.founder)

    def test_egypt_sees_instapay_link_and_ipa_without_any_phone_number(self):
        page = self.client.get(CHECKOUT)
        self.assertContains(page, 'href="https://ipn.eg/S/you_zersif7/instapay/5UiNUX"')
        self.assertContains(page, 'target="_blank" rel="noopener noreferrer" data-instapay-link')
        self.assertContains(page, "you_zersif7@instapay")
        self.assertContains(page, 'name="payment_proof"')
        self.assertContains(page, 'enctype="multipart/form-data"')
        self.assertNotContains(page, "RedotPay")
        section = page.content.decode().split("data-manual-payment", 1)[1].split("</section>", 1)[0]
        self.assertNotRegex(section, r"01[0125]\d{8}")  # no personal phone number in the payment box

    def test_outside_egypt_sees_redotpay_and_sales_contact(self):
        PartnerProfile.objects.filter(pk=self.partner.pk).update(country_of_registration="AE")
        page = self.client.get(CHECKOUT)
        for text in ("RedotPay", "1588800300", "Transfer directly to RedotPay ID: 1588800300 or contact",
                     'href="mailto:sales@internest.app"', "for SWIFT/International invoice details."):
            self.assertContains(page, text)
        self.assertNotContains(page, "you_zersif7@instapay")
        resp = self.client.post(CHECKOUT, paid(customer_phone="+971501234567"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ProUpgradeRequest.objects.get().gateway, "redotpay")

    def test_receipt_is_required_and_validated(self):
        page = self.client.post(CHECKOUT, BUYER)
        self.assertContains(page, "Upload a screenshot of your transfer receipt so we can verify it.")
        self.client.post(CHECKOUT, {**BUYER, "payment_proof": SimpleUploadedFile("r.exe", b"MZ", "application/octet-stream")})
        self.assertFalse(ProUpgradeRequest.objects.exists())

    def test_receipt_creates_a_pending_verification_order(self):
        resp = self.client.post(CHECKOUT, paid(payment_reference="IPN-7781"), follow=True)
        self.assertContains(resp, "verifying your transfer")
        order = ProUpgradeRequest.objects.get()
        self.assertEqual((order.status, order.payment_reference), (ProUpgradeRequest.STATUS_VERIFYING, "IPN-7781"))
        self.assertTrue(order.payment_proof.name.startswith("payment_proofs/"))
        self.assertIsNotNone(order.proof_submitted_at)
        self.assertEqual(order.get_status_display(), "Pending verification")
        page = self.client.get(CHECKOUT)
        self.assertContains(page, "data-verifying")
        self.assertNotContains(page, 'value="cancel_pending"')  # a sent receipt can't be cancelled by the company
        self.client.post(CHECKOUT, {"action": "cancel_pending"})
        order.refresh_from_db()
        self.assertEqual(order.status, ProUpgradeRequest.STATUS_VERIFYING)
        self.client.post(CHECKOUT, paid())
        self.assertEqual(ProUpgradeRequest.objects.count(), 1)  # no duplicate order while verifying

    def test_receipt_is_private(self):
        from django.contrib.auth.models import User

        self.client.post(CHECKOUT, paid())
        url = reverse("serve_media", args=[ProUpgradeRequest.objects.get().payment_proof.name])
        self.assertEqual(self.client.get(url).status_code, 200)  # the paying company
        self.client.force_login(User.objects.create_user("snoop", password="pw"))
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_admin_verifies_receipt_and_activates_pro(self):
        from django.contrib.auth.models import User

        from .tiers import is_pro

        self.client.post(CHECKOUT, paid())
        order = ProUpgradeRequest.objects.get()
        self.client.force_login(User.objects.create_superuser("boss", "boss@example.com", "pw"))
        changelist = reverse("admin:internest_startups_proupgraderequest_changelist")
        page = self.client.get(changelist, {"status__exact": "verifying"})
        self.assertContains(page, "Open receipt")
        self.assertContains(page, "Receipt verified")
        self.client.post(changelist, {"action": "activate_requests", "_selected_action": [order.pk]})
        order.refresh_from_db()
        self.assertEqual(order.status, ProUpgradeRequest.STATUS_ACTIVE)
        self.partner.refresh_from_db()
        self.assertTrue(is_pro(self.partner))
        self.assertContains(self.client.get(reverse("admin:index")), "Payment receipts to verify")
