"""Phase 5: Free = EGP 0, Hiring Pass = EGP 500 for one opportunity and 30 days, no renewal, Founding 20, payment safety."""
import re
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from internest_core.models import Application, Internship, PartnerProfile, StudentProfile
from internest_core.tests_applications import ApplicationsBase, _student
from internest_skills.models import Skill, StudentSkill

from . import entitlements
from .models import FoundingSeat, HiringPass, ProUpgradeRequest
from .pricing import FREE_PRICE, HIRING_PASS_PRICES, pass_quote
from .tiers import activate_pro, is_pro

CHECKOUT = reverse("checkout")
UPGRADE = reverse("startup_upgrade")
CLAIM = reverse("founding_claim")
MEDIA = tempfile.mkdtemp()
BUYER = {"customer_name": "Youssef Sameh", "customer_email": "pay@nilecode.io", "customer_phone": "01113999387", "accept_terms": "on"}


def _receipt():
    return SimpleUploadedFile("r.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, content_type="image/png")


def _company(name, verified=True):
    from .models import CompanyProfile

    user = User.objects.create_user(name, password="pw")
    partner = PartnerProfile.objects.create(user=user, company_name=name, partner_code=name[:10].upper(), is_fully_verified=verified,
                                            official_email=f"hi@{name}.io", official_website=f"https://{name}.io",
                                            linkedin_url="https://linkedin.com/x", country_of_registration="EG")
    CompanyProfile.objects.create(partner=partner, industry="software", founded_year=2022, description="d", founder_email=f"f@{name}.io")
    partner.calculate_completion()
    partner.refresh_from_db()
    return user, partner


def _opportunity(partner, title="Role"):
    return Internship.objects.create(partner=partner, title=title, description="d", location="Cairo", required_majors="any",
                                     deadline=timezone.now().date() + timedelta(days=10))


class PriceRulesTests(ApplicationsBase):
    def test_free_is_zero_and_pass_is_500_for_30_days(self):
        self.assertEqual(FREE_PRICE, Decimal("0"))
        self.assertEqual(HIRING_PASS_PRICES, {"EGP": Decimal("500"), "USD": Decimal("29"), "EUR": Decimal("27")})
        q = pass_quote("EGP")
        self.assertEqual((q["list_price"], q["final_price"], q["final_label"]), (500, 500, "500 EGP"))
        self.assertEqual(entitlements.PASS_DAYS, 30)
        self.assertEqual((pass_quote("USD")["final_label"], pass_quote("EUR")["final_label"]), ("$29", "€27"))
        self.assertIsNone(pass_quote("GBP"))  # a currency without a decided price is never sold

    def test_no_old_advertised_price_anywhere_in_the_product(self):
        old = re.compile(r"(?:\b(?:700|800|8,000|8000)\s*(?:EGP|جنيه|جنيه مصري))|(?:EGP\s*(?:700|800|8,000|8000)\b)|PERIOD_YEAR|PRICES\s*=")
        root = Path(settings.BASE_DIR)
        hits = []
        for app in ("internest_core", "internest_skills", "internest_startups", "internest_lounge", "internest_campus", "internest_students"):
            for path in (root / app).rglob("*"):
                if path.suffix not in {".py", ".html", ".js", ".txt"} or "migrations" in path.parts or path.name.startswith("tests"):
                    continue
                if old.search(path.read_text(encoding="utf-8", errors="ignore")):
                    hits.append(str(path.relative_to(root)))
        po = (root / "locale" / "ar" / "LC_MESSAGES" / "django.po").read_text(encoding="utf-8")
        live = "\n".join(line for line in po.splitlines() if not line.startswith("#~"))
        if old.search(live):
            hits.append("locale/ar/LC_MESSAGES/django.po")
        self.assertEqual(hits, [])

    def test_same_price_on_every_page(self):
        PartnerProfile.objects.filter(pk=self.partner.pk).update(country_of_registration="EG")
        self.client.force_login(self.founder)
        self.assertContains(self.client.get(UPGRADE), "500 EGP")
        self.assertContains(self.client.get(CHECKOUT), "500 EGP", count=3)
        self.assertContains(self.client.get(reverse("terms")), "<strong>EGP 500</strong>")
        self.assertContains(self.client.get(reverse("refund_policy")), "<strong>EGP 500</strong>")
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("terms")), "<strong>500 جنيه مصري</strong>")
        self.assertContains(self.client.get(UPGRADE), "باقة التوظيف")
        dashboard = self.client.get(reverse("partner_dashboard"))
        self.assertContains(dashboard, "فرصة واحدة · 30 يوم · من غير تجديد")


@override_settings(MEDIA_ROOT=MEDIA)
class CheckoutSafetyTests(ApplicationsBase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        PartnerProfile.objects.filter(pk=self.partner.pk).update(country_of_registration="EG")
        self.client.force_login(self.founder)

    def test_amount_and_product_come_only_from_the_server(self):
        tampered = {**BUYER, "internship": self.gig.pk, "payment_proof": _receipt(), "final_price": "1", "list_price": "1",
                    "currency": "USD", "product": "pro", "months": "12", "discount_percent": "100"}
        self.client.post(CHECKOUT, tampered)
        order = ProUpgradeRequest.objects.get()
        self.assertEqual((order.product, order.list_price, order.final_price, order.currency, order.months, order.discount_percent),
                         ("hiring_pass", 500, 500, "EGP", 1, 0))
        self.assertEqual(order.internship_id, self.gig.pk)
        self.assertFalse(is_pro(self.partner))  # nothing before an admin confirms the payment

    def test_cannot_buy_for_another_company_or_closed_opportunity(self):
        _u, other = _company("othercorp")
        foreign = _opportunity(other)
        closed = _opportunity(self.partner, "Closed")
        Internship.objects.filter(pk=closed.pk).update(is_active=False)
        for pk in (foreign.pk, closed.pk, 999999, "abc"):
            page = self.client.post(CHECKOUT, {**BUYER, "internship": pk, "payment_proof": _receipt()})
            self.assertContains(page, "Choose one of your open opportunities.")
        self.assertFalse(ProUpgradeRequest.objects.exists())

    def test_no_second_order_for_a_covered_opportunity(self):
        entitlements.create_pass(self.partner, self.gig, source="admin")
        resp = self.client.post(CHECKOUT, {**BUYER, "internship": self.gig.pk, "payment_proof": _receipt()}, follow=True)
        self.assertContains(resp, "This opportunity already has paid access. Nothing was charged.")
        self.assertFalse(ProUpgradeRequest.objects.exists())

    def test_checkout_shows_availability_before_paying(self):
        python = Skill.objects.get(slug="python")
        python.opportunities.add(self.gig)
        page = self.client.get(CHECKOUT)
        self.assertContains(page, "0 matching verified candidates now")
        self.assertContains(page, "below our availability minimum")
        for i in range(3):
            user, student = _student(f"cand{i}")
            StudentProfile.objects.filter(pk=student.pk).update(talent_pool_visible=True)
            StudentSkill.objects.create(student=student, skill=python, source="explicit", status="verified", score=80)
        page = self.client.get(CHECKOUT)
        self.assertContains(page, "3 matching verified candidates now")


class ActivationTests(ApplicationsBase):
    def _order(self, **kw):
        q = pass_quote("EGP")
        data = dict(partner=self.partner, product="hiring_pass", internship=self.gig, list_price=q["list_price"], final_price=q["final_price"],
                    status=ProUpgradeRequest.STATUS_VERIFYING)
        data.update(kw)
        return ProUpgradeRequest.objects.create(**data)

    def test_activation_creates_one_30_day_pass_and_is_idempotent(self):
        order = self._order()
        self.assertTrue(entitlements.activate_order(order.pk))
        self.assertFalse(entitlements.activate_order(order.pk))  # replay / double click
        hiring_pass = HiringPass.objects.get()
        self.assertEqual((hiring_pass.order_id, hiring_pass.internship_id, hiring_pass.source), (order.pk, self.gig.pk, "purchase"))
        self.assertEqual(hiring_pass.expires_at - hiring_pass.starts_at, timedelta(days=30))
        with self.assertRaises(IntegrityError), transaction.atomic():
            HiringPass.objects.create(partner=self.partner, internship=self.gig, source="purchase", order=order,
                                      starts_at=timezone.now(), expires_at=timezone.now() + timedelta(days=30))

    def test_tampered_amount_is_refused_and_nothing_changes(self):
        order = self._order(final_price=Decimal("1.00"))
        with self.assertRaises(ValueError):
            entitlements.activate_order(order.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, ProUpgradeRequest.STATUS_VERIFYING)  # rolled back
        self.assertFalse(HiringPass.objects.exists())

    def test_admin_action_reports_a_refused_order(self):
        order = self._order(list_price=Decimal("400.00"), final_price=Decimal("400.00"))
        self.client.force_login(User.objects.create_superuser("boss", "boss@example.com", "pw"))
        changelist = reverse("admin:internest_startups_proupgraderequest_changelist")
        resp = self.client.post(changelist, {"action": "activate_requests", "_selected_action": [order.pk]}, follow=True)
        self.assertContains(resp, "does not match the Hiring Pass price")
        self.assertFalse(HiringPass.objects.exists())

    def test_amounts_are_read_only_in_admin(self):
        from django.contrib.admin.sites import site

        admin = site._registry[ProUpgradeRequest]
        for field in ("list_price", "final_price", "currency", "discount_percent", "product", "internship", "partner"):
            self.assertIn(field, admin.readonly_fields)
        self.assertFalse(site._registry[HiringPass].has_add_permission(None))

    def test_no_automatic_renewal(self):
        hiring_pass = entitlements.create_pass(self.partner, self.gig, source="admin")
        self.assertTrue(is_pro(self.partner))
        HiringPass.objects.filter(pk=hiring_pass.pk).update(starts_at=timezone.now() - timedelta(days=31),
                                                            expires_at=timezone.now() - timedelta(days=1))
        entitlements.forget(self.partner)
        self.assertFalse(is_pro(self.partner))
        self.assertEqual(HiringPass.objects.count(), 1)  # nothing new was created or charged
        self.assertEqual(ProUpgradeRequest.objects.count(), 0)

    def test_historical_pro_orders_are_unchanged_and_honoured(self):
        old = ProUpgradeRequest.objects.create(partner=self.partner, product="pro", list_price=800, final_price=800, months=1,
                                               status=ProUpgradeRequest.STATUS_VERIFYING)
        done = ProUpgradeRequest.objects.create(partner=self.partner, product="pro", list_price=8000, final_price=8000, months=12,
                                                status=ProUpgradeRequest.STATUS_ACTIVE)
        self.assertEqual((old.product, done.product), ("pro", "pro"))
        self.assertTrue(entitlements.activate_order(old.pk))  # placed before the change: honoured as Pro
        old.refresh_from_db()
        done.refresh_from_db()
        self.assertEqual((old.final_price, old.months, done.final_price, done.months, done.status), (800, 1, 8000, 12, "activated"))
        self.assertTrue(entitlements.is_legacy_pro(PartnerProfile.objects.get(pk=self.partner.pk)))
        self.assertFalse(HiringPass.objects.exists())


class PassScopeTests(ApplicationsBase):
    """A pass gives paid features for its own opportunity only; legacy Pro covers every opportunity."""

    def setUp(self):
        super().setUp()
        self.user, self.student = _student("amr")
        StudentProfile.objects.filter(pk=self.student.pk).update(talent_pool_visible=True)
        StudentSkill.objects.create(student=self.student, skill=Skill.objects.get(slug="python"), source="explicit",
                                    status="verified", score=91, percentile=87)
        self.on_gig = Application.objects.create(internship=self.gig, applicant=self.user)
        other_user, _ = _student("sara")
        self.on_role = Application.objects.create(internship=self.role, applicant=other_user)
        StudentSkill.objects.create(student=StudentProfile.objects.get(user=other_user), skill=Skill.objects.get(slug="python"),
                                    source="explicit", status="verified", score=88, percentile=70)
        self.client.force_login(self.founder)

    def test_scores_only_for_the_covered_opportunity(self):
        entitlements.create_pass(self.partner, self.gig, source="admin")
        self.assertContains(self.client.get(reverse("partner_applicant", args=[self.on_gig.pk])), "91% · P87")
        page = self.client.get(reverse("partner_applicant", args=[self.on_role.pk]))
        self.assertNotContains(page, "88% · P70")
        self.assertContains(page, "Upgrade to see the detailed score report")

    def test_invitations_only_to_the_covered_opportunity(self):
        entitlements.create_pass(self.partner, self.gig, source="admin")
        page = self.client.get(reverse("talent_pool"))
        self.assertEqual([o.pk for o in page.context["opportunities"]], [self.gig.pk])
        invite = reverse("talent_pool_invite", args=[self.student.pk])
        self.client.post(invite, {"internship": self.role.pk})
        self.assertFalse(self.partner.talent_invitations.exists())
        self.client.post(invite, {"internship": self.gig.pk})
        self.assertEqual(list(self.partner.talent_invitations.values_list("internship_id", flat=True)), [self.gig.pk])

    def test_legacy_pro_covers_everything(self):
        activate_pro(self.partner)
        self.assertContains(self.client.get(reverse("partner_applicant", args=[self.on_role.pk])), "88% · P70")
        self.assertEqual(entitlements.covered_internship_ids(self.partner), None)

    def test_pass_does_not_lift_the_monthly_post_quota_or_add_a_badge(self):
        from .tiers import monthly_quota_reached

        entitlements.create_pass(self.partner, self.gig, source="admin")
        self.assertFalse(entitlements.is_legacy_pro(self.partner))
        self.assertNotContains(self.client.get(reverse("partner_dashboard")), "Pro Verified")
        self.assertIsNotNone(monthly_quota_reached(self.partner))  # still the free quota rule (checked elsewhere)


class FoundingTests(ApplicationsBase):
    def setUp(self):
        super().setUp()
        self.partner.refresh_from_db()
        self.client.force_login(self.founder)

    def test_claim_gives_seat_and_one_free_30_day_pass(self):
        self.assertEqual(self.partner.profile_completion_score, 100)
        self.assertFalse(FoundingSeat.objects.exists())  # nobody is enrolled automatically
        resp = self.client.post(CLAIM, {"internship": self.gig.pk}, follow=True)
        self.assertContains(resp, "Welcome to Founding 20 (seat 1)!")
        seat = FoundingSeat.objects.get()
        self.assertEqual((seat.number, seat.partner_id, seat.hiring_pass.source, seat.hiring_pass.internship_id),
                         (1, self.partner.pk, "founding", self.gig.pk))
        self.assertEqual(seat.hiring_pass.expires_at - seat.hiring_pass.starts_at, timedelta(days=30))
        self.assertFalse(ProUpgradeRequest.objects.exists())  # free: no order, no payment
        self.assertTrue(is_pro(PartnerProfile.objects.get(pk=self.partner.pk)))
        self.assertContains(self.client.get(UPGRADE), "You hold Founding seat 1 of 20.")

    def test_duplicate_claim_is_refused(self):
        self.client.post(CLAIM, {"internship": self.gig.pk})
        resp = self.client.post(CLAIM, {"internship": self.role.pk}, follow=True)
        self.assertContains(resp, "Your company already has a Founding 20 seat.")
        self.assertEqual((FoundingSeat.objects.count(), HiringPass.objects.count()), (1, 1))

    def test_eligibility_is_enforced_on_the_server(self):
        _u, other = _company("othercorp")
        self.assertEqual(self.client.post(CLAIM, {"internship": _opportunity(other).pk}).status_code, 302)  # not their opportunity
        PartnerProfile.objects.filter(pk=self.partner.pk).update(is_fully_verified=False)
        resp = self.client.post(CLAIM, {"internship": self.gig.pk}, follow=True)
        self.assertContains(resp, "Founding 20 opens once our team verifies your company.")
        PartnerProfile.objects.filter(pk=self.partner.pk).update(is_fully_verified=True, profile_completion_score=80)
        resp = self.client.post(CLAIM, {"internship": self.gig.pk}, follow=True)
        self.assertContains(resp, "Complete your company profile to 100%")
        self.assertFalse(FoundingSeat.objects.exists())
        student_user, _ = _student("stud")
        self.login_student(student_user)
        self.client.post(CLAIM, {"internship": self.gig.pk})
        self.assertFalse(FoundingSeat.objects.exists())

    def test_only_20_seats_and_numbers_are_unique(self):
        for i in range(20):
            _u, company = _company(f"co{i:02d}")
            entitlements.claim_founding(company, _opportunity(company))
        self.assertEqual(sorted(FoundingSeat.objects.values_list("number", flat=True)), list(range(1, 21)))
        self.assertEqual(entitlements.founding_seats_left(), 0)
        resp = self.client.post(CLAIM, {"internship": self.gig.pk}, follow=True)
        self.assertContains(resp, "All 20 Founding seats have been claimed.")
        self.assertEqual(FoundingSeat.objects.count(), 20)
        with self.assertRaises(IntegrityError), transaction.atomic():  # the database itself refuses seat 21
            hp = entitlements.create_pass(self.partner, self.gig, source="founding")
            FoundingSeat.objects.create(number=21, partner=self.partner, hiring_pass=hp)
        with self.assertRaises(IntegrityError), transaction.atomic():  # and a second seat 5
            hp = entitlements.create_pass(self.partner, self.gig, source="founding")
            FoundingSeat.objects.create(number=5, partner=self.partner, hiring_pass=hp)

    def test_feedback_is_optional_and_structured(self):
        self.assertEqual(self.client.get(reverse("founding_feedback")).status_code, 404)  # no seat yet
        self.client.post(CLAIM, {"internship": self.gig.pk})
        page = self.client.get(reverse("founding_feedback"))
        self.assertContains(page, "Every question is optional.")
        self.assertNotContains(page, "checked")  # nothing pre-filled
        resp = self.client.post(reverse("founding_feedback"), {}, follow=True)  # an empty answer is accepted
        self.assertContains(resp, "Thank you! Your feedback goes straight to the founding team.")
        self.client.post(reverse("founding_feedback"), {"found_candidates": "no", "would_pay": "no", "trust_in_verification": "2",
                                                         "missing": "More data analysts"})
        seat = FoundingSeat.objects.get()
        self.assertEqual((seat.found_candidates, seat.would_pay, seat.trust_in_verification), ("no", "no", 2))
        self.assertIsNotNone(seat.feedback_at)
        resp = self.client.post(reverse("founding_feedback"), {"trust_in_verification": "9"})
        self.assertEqual(resp.status_code, 200)  # out of range → form error, nothing saved
        seat.refresh_from_db()
        self.assertEqual(seat.trust_in_verification, 2)


class AvailabilityRecordTests(ApplicationsBase):
    def test_pass_records_matching_candidates_at_start(self):
        python = Skill.objects.get(slug="python")
        python.opportunities.add(self.gig)
        for i, status in enumerate(("verified", "verified", "lag")):
            _u, student = _student(f"s{i}")
            StudentProfile.objects.filter(pk=student.pk).update(talent_pool_visible=True)
            StudentSkill.objects.create(student=student, skill=python, source="explicit", status=status, score=95)
        hiring_pass = entitlements.create_pass(self.partner, self.gig, source="admin")
        self.assertEqual(hiring_pass.available_at_start, 2)  # the failed (lag) attempt is not counted
        self.assertLess(hiring_pass.available_at_start, entitlements.MIN_AVAILABLE)  # → refundable under the policy


@override_settings(MEDIA_ROOT=MEDIA)
class ReviewFixTests(ApplicationsBase):
    """Findings of the independent Phase 5 review, each pinned by a test."""

    def setUp(self):
        super().setUp()
        PartnerProfile.objects.filter(pk=self.partner.pk).update(country_of_registration="EG")
        self.partner.refresh_from_db()
        self.client.force_login(self.founder)

    def _pass_order(self, **kw):
        q = pass_quote("EGP")
        data = dict(partner=self.partner, product="hiring_pass", internship=self.gig, list_price=q["list_price"],
                    final_price=q["final_price"], status=ProUpgradeRequest.STATUS_VERIFYING)
        data.update(kw)
        return ProUpgradeRequest.objects.create(**data)

    def test_company_with_a_pass_and_seat_can_still_be_deleted(self):
        entitlements.activate_order(self._pass_order().pk)
        entitlements.claim_founding(self.partner, self.role)
        self.founder.delete()  # account erasure cascades through orders, passes and seats
        self.assertFalse(HiringPass.objects.exists())
        self.assertFalse(FoundingSeat.objects.exists())

    def test_old_unpaid_pro_order_is_labelled_cancellable_and_does_not_block(self):
        old = ProUpgradeRequest.objects.create(partner=self.partner, product="pro", list_price=8000, final_price=8000, months=12)
        page = self.client.get(CHECKOUT)
        self.assertContains(page, "data-legacy-order")
        self.assertContains(page, 'name="accept_terms"')  # the pass can still be bought
        self.client.post(CHECKOUT, {**BUYER, "internship": self.gig.pk, "payment_proof": _receipt()})
        self.assertTrue(ProUpgradeRequest.objects.filter(product="hiring_pass").exists())
        self.client.post(CHECKOUT, {"action": "cancel_legacy"})
        old.refresh_from_db()
        self.assertEqual(old.status, ProUpgradeRequest.STATUS_CANCELLED)

    def test_founding_refused_when_opportunity_covered_or_ordered_or_legacy_pro(self):
        self._pass_order()
        with self.assertRaisesMessage(ValueError, "already has paid access or an order in progress"):
            entitlements.claim_founding(self.partner, self.gig)
        entitlements.create_pass(self.partner, self.role, source="admin")
        with self.assertRaisesMessage(ValueError, "already has paid access or an order in progress"):
            entitlements.claim_founding(self.partner, self.role)
        _u, pro = _company("legacyco")
        activate_pro(pro)
        self.assertEqual(entitlements.founding_block_reason(pro), "Founding 20 is for companies without paid access.")

    def test_second_pass_for_the_same_opportunity_starts_when_the_first_ends(self):
        first = entitlements.create_pass(self.partner, self.gig, source="admin")
        second = entitlements.create_pass(self.partner, self.gig, source="admin")
        self.assertEqual(second.starts_at, first.expires_at)
        self.assertEqual(second.expires_at - second.starts_at, timedelta(days=30))
        self.assertFalse(second.is_active)

    def test_badge_check_never_loads_passes(self):
        _u, company = _company("badgeco")
        entitlements.create_pass(company, _opportunity(company), source="admin")
        fresh = PartnerProfile.objects.get(pk=company.pk)
        with self.assertNumQueries(1):  # the subscription row only
            self.assertFalse(entitlements.is_legacy_pro(fresh))
        with self.assertNumQueries(1):  # passes loaded once, when access is really asked for
            self.assertTrue(entitlements.has_hiring_access(fresh))
            self.assertTrue(entitlements.has_hiring_access(fresh))

    def test_status_is_read_only_and_re_running_activation_never_crashes(self):
        from django.contrib.admin.sites import site

        self.assertIn("status", site._registry[ProUpgradeRequest].readonly_fields)
        order = self._pass_order()
        entitlements.activate_order(order.pk)
        ProUpgradeRequest.objects.filter(pk=order.pk).update(status=ProUpgradeRequest.STATUS_VERIFYING)  # e.g. a manual DB edit
        self.client.force_login(User.objects.create_superuser("boss", "boss@example.com", "pw"))
        changelist = reverse("admin:internest_startups_proupgraderequest_changelist")
        resp = self.client.post(changelist, {"action": "activate_requests", "_selected_action": [order.pk]}, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f"Order {order.pk} not activated")
        self.assertEqual(HiringPass.objects.count(), 1)

    def test_activation_rechecks_opportunity_owner_and_discount(self):
        _u, other = _company("otherco")
        foreign = self._pass_order(internship=_opportunity(other))
        with self.assertRaisesMessage(ValueError, "belongs to another company"):
            entitlements.activate_order(foreign.pk)
        ProUpgradeRequest.objects.filter(pk=foreign.pk).update(status=ProUpgradeRequest.STATUS_CANCELLED)
        no_promo = self._pass_order(discount_percent=99, final_price=Decimal("5.00"))
        with self.assertRaisesMessage(ValueError, "does not match"):
            entitlements.activate_order(no_promo.pk)
        self.assertFalse(HiringPass.objects.exists())

    def test_database_allows_one_open_pass_order_per_company(self):
        self._pass_order()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._pass_order(internship=self.role)
        ProUpgradeRequest.objects.create(partner=self.partner, product="pro", list_price=800, final_price=800)  # legacy rows unaffected

    def test_dashboard_shows_the_pass_end_date(self):
        hiring_pass = entitlements.create_pass(self.partner, self.gig, source="admin")
        page = self.client.get(reverse("partner_dashboard"))
        self.assertContains(page, f"Hiring Pass active until {timezone.localtime(hiring_pass.expires_at):%Y/%m/%d}")
