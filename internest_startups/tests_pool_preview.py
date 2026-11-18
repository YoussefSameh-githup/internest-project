"""Phase 1: one verified-candidate rule (H-1), real pool numbers before paying (H-3), anonymised free preview."""
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from internest_core.models import PartnerProfile, StudentProfile
from internest_core.tests_applications import ApplicationsBase, _student
from internest_skills.models import Skill, StudentSkill
from internest_skills.verification import _band, anonymous_previews, is_pool_eligible, pool_candidates, pool_stats

from .tiers import activate_pro

PREVIEW = reverse("talent_pool_preview")


def _skill(student, slug, status=StudentSkill.STATUS_VERIFIED, score=90, via=StudentSkill.VIA_CHALLENGE):
    return StudentSkill.objects.create(student=student, skill=Skill.objects.get(slug=slug), source="explicit", status=status,
                                       score=score, verified_via=via if status == StudentSkill.STATUS_VERIFIED else "")


def _candidate(username, opted_in=True, **fields):
    user, profile = _student(username)
    StudentProfile.objects.filter(pk=profile.pk).update(talent_pool_visible=opted_in, **fields)
    profile.refresh_from_db()
    return user, profile


class VerifiedRuleTests(ApplicationsBase):
    """The rule every count, search and preview uses."""

    def setUp(self):
        super().setUp()
        _u, self.passed = _candidate("passed")
        _skill(self.passed, "python", score=72)
        _u, self.cert = _candidate("cert")
        _skill(self.cert, "excel", score=None, via=StudentSkill.VIA_CERTIFICATE)  # reviewed certificate: no score, still verified
        _u, self.lag = _candidate("lag")
        _skill(self.lag, "python", status=StudentSkill.STATUS_LAG, score=96)  # high score, weak sub-skill: failed
        _u, self.claimed = _candidate("claimed")
        _skill(self.claimed, "python", status=StudentSkill.STATUS_CLAIMED, score=88)
        _u, self.hidden = _candidate("hidden", opted_in=False)
        _skill(self.hidden, "python")
        gone_user, self.gone = _candidate("gone")
        _skill(self.gone, "python")
        User.objects.filter(pk=gone_user.pk).update(is_active=False)

    def test_only_verified_opted_in_active_students_count(self):
        self.assertEqual(set(pool_candidates()), {self.passed, self.cert})

    def test_high_score_without_verification_is_not_eligible(self):
        self.assertTrue(is_pool_eligible(self.passed))
        self.assertTrue(is_pool_eligible(self.hidden))  # eligible; opt-in is a separate, visible choice
        self.assertFalse(is_pool_eligible(self.lag))
        self.assertFalse(is_pool_eligible(self.claimed))
        self.assertFalse(is_pool_eligible(self.gone))

    def test_stats_are_exact(self):
        stats = pool_stats()
        self.assertEqual(stats["candidates"], 2)
        self.assertEqual(stats["verified_skills"], 2)
        self.assertEqual({r["name"]: r["candidates"] for r in stats["per_skill"]}, {"Python": 1, "Excel": 1})
        self.assertEqual({m["via"]: m["skills"] for m in stats["per_method"]},
                         {StudentSkill.VIA_CHALLENGE: 1, StudentSkill.VIA_CERTIFICATE: 1})

    def test_candidate_with_two_skills_is_one_candidate(self):
        _skill(self.passed, "excel")
        stats = pool_stats()
        self.assertEqual(stats["candidates"], 2)
        self.assertEqual(stats["verified_skills"], 3)
        self.assertEqual({r["name"]: r["candidates"] for r in stats["per_skill"]}, {"Python": 1, "Excel": 2})

    def test_empty_pool_reports_zero(self):
        StudentSkill.objects.all().delete()
        self.assertEqual(pool_stats(), {"candidates": 0, "verified_skills": 0, "per_skill": [], "per_method": []})
        self.assertEqual(anonymous_previews(), [])

    def test_score_bands(self):
        self.assertEqual(_band(72), "70–79%")
        self.assertEqual(_band(95), "90–100%")
        self.assertEqual(_band(100), "90–100%")
        self.assertEqual(_band(0), "0–9%")
        self.assertIsNone(_band(None))

    def test_previews_hold_no_identifying_fields(self):
        previews = anonymous_previews()
        self.assertEqual(len(previews), 2)
        for p in previews:
            self.assertEqual(set(p), {"major", "study_level", "skills"})
            for s in p["skills"]:
                self.assertEqual(set(s), {"name", "band", "via"})


class PreviewPageTests(ApplicationsBase):
    def setUp(self):
        super().setUp()
        self.amr_user, self.amr = _candidate("amr", university="Zewail Secret Uni", major="Computer Science",
                                             phone_number="01099887766", bio="Private bio text", portfolio_url="https://amr.dev")
        _skill(self.amr, "python", score=95)
        _u, self.sara = _candidate("sara", major="Finance")
        _skill(self.sara, "financial-analysis", score=81)
        _u, self.lag = _candidate("lagonly", major="Accounting")
        _skill(self.lag, "excel", status=StudentSkill.STATUS_LAG, score=99)

    def test_free_startup_sees_real_numbers_and_anonymous_cards(self):
        self.client.force_login(self.founder)
        resp = self.client.get(PREVIEW)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["stats"]["candidates"], 2)
        self.assertEqual(len(resp.context["previews"]), 2)
        self.assertContains(resp, "<strong>2</strong>", html=False)
        self.assertContains(resp, "Candidate 1")
        self.assertContains(resp, "Computer Science")
        self.assertContains(resp, "90–100%")
        self.assertNotContains(resp, "Accounting")  # the lag-only student is not a candidate

    def test_preview_never_leaks_identity_or_ids(self):
        self.client.force_login(self.founder)
        resp = self.client.get(PREVIEW)
        html = resp.content.decode()
        start = html.index('class="section pool-preview"')
        page = html[start:html.index("</section>", html.index("pool-preview__cta"))]  # the page body, not the navbar
        for secret in ("Amr", "amr", "amr@uni.edu", "01099887766", "Zewail Secret Uni", "Private bio text", "amr.dev",
                       "Sara", "sara", "/candidate/", "/media/", "95%"):
            self.assertNotIn(secret, page)
        for pk in (self.amr.pk, self.sara.pk, self.amr_user.pk):
            self.assertNotRegex(page, rf'(?:=|/|"){pk}(?:"|/|&)')  # no id in any attribute or link
        self.assertNotIn("score", str(resp.context["previews"]))

    def test_inactive_hidden_and_failed_students_never_rendered(self):
        _u, hidden = _candidate("hiddenx", opted_in=False, major="Hidden Major")
        _skill(hidden, "python")
        gone_user, gone = _candidate("gonex", major="Gone Major")
        _skill(gone, "python")
        User.objects.filter(pk=gone_user.pk).update(is_active=False)
        self.client.force_login(self.founder)
        resp = self.client.get(PREVIEW)
        for text in ("Hidden Major", "Gone Major", "Accounting"):
            self.assertNotContains(resp, text)
        self.assertEqual(resp.context["stats"]["candidates"], 2)

    def test_long_major_is_shortened(self):
        StudentProfile.objects.filter(pk=self.sara.pk).update(major="Finance - Ahmed Hassan Mohamed, AUC class of 2025 honours")
        self.client.force_login(self.founder)
        majors = [p["major"] for p in self.client.get(PREVIEW).context["previews"]]
        self.assertIn("Finance - Ahmed Hassan Mohamed, AUC cla…", majors)

    def test_unverified_company_sees_numbers_but_no_cards(self):
        type(self.partner).objects.filter(pk=self.partner.pk).update(is_fully_verified=False)
        self.client.force_login(self.founder)
        resp = self.client.get(PREVIEW)
        self.assertEqual(resp.context["stats"]["candidates"], 2)
        self.assertEqual(resp.context["previews"], [])
        self.assertContains(resp, "Anonymous candidate summaries appear once our team verifies your company")
        self.assertNotContains(resp, "Computer Science")

    def test_unicode_digits_do_not_crash(self):
        self.client.force_login(self.founder)
        for junk in ("²", "١", "１", "9" * 30, " 5 "):
            self.assertEqual(self.client.get(PREVIEW, {"skill": junk}).status_code, 200)
        activate_pro(self.partner)
        for junk in ("²", "١", "１", "9" * 30):
            self.assertEqual(self.client.get(reverse("talent_pool"), {"skill": junk, "min_score": junk}).status_code, 200)

    def test_previews_rank_scored_before_unscored(self):
        _u, cert = _candidate("certx", major="Cert Major")
        _skill(cert, "excel", score=None, via=StudentSkill.VIA_CERTIFICATE)
        self.client.force_login(self.founder)
        majors = [p["major"] for p in self.client.get(PREVIEW).context["previews"]]
        self.assertEqual(majors[-1], "Cert Major")

    def test_skill_filter_only_for_groups_of_three_or_more(self):
        self.client.force_login(self.founder)
        python = Skill.objects.get(slug="python").pk
        resp = self.client.get(PREVIEW, {"skill": python})
        self.assertIsNone(resp.context["skill_id"])  # one Python candidate: a filter would single them out
        self.assertNotContains(resp, f'href="?skill={python}"')
        self.assertContains(resp, '<span class="pool-chip"><bdi>Python</bdi> <b>1</b></span>', html=False)
        for name in ("py2", "py3"):
            _u, p = _candidate(name, major="Data Science")
            _skill(p, "python", score=75)
        resp = self.client.get(PREVIEW, {"skill": python})
        self.assertEqual(resp.context["skill_id"], python)
        self.assertEqual([p["major"] for p in resp.context["previews"]], ["Computer Science", "Data Science", "Data Science"])
        self.assertContains(resp, 'aria-current="true"')
        excel = Skill.objects.get(slug="excel").pk  # only a lagging student has it: not a filter option
        for junk in (str(excel), "abc", "999999", "-1"):
            self.assertIsNone(self.client.get(PREVIEW, {"skill": junk}).context["skill_id"])

    def test_pro_goes_to_full_search_and_free_is_sent_to_preview(self):
        self.client.force_login(self.founder)
        self.assertRedirects(self.client.get(reverse("talent_pool")), PREVIEW)
        activate_pro(self.partner)
        self.assertRedirects(self.client.get(PREVIEW), reverse("talent_pool"))
        python = Skill.objects.get(slug="python").pk
        self.assertRedirects(self.client.get(PREVIEW, {"skill": python}), reverse("talent_pool") + f"?skill={python}")

    def test_pro_search_ignores_failed_skill_of_a_verified_candidate(self):
        _skill(self.sara, "excel", status=StudentSkill.STATUS_LAG, score=99)
        activate_pro(self.partner)
        self.client.force_login(self.founder)
        resp = self.client.get(reverse("talent_pool"))
        sara = next(c for c in resp.context["page"] if c.pk == self.sara.pk)
        self.assertEqual([s.skill.slug for s in sara.pool_skills], ["financial-analysis"])
        self.assertEqual(sara.best_score, 81)
        names = {c.pk for c in self.client.get(reverse("talent_pool"), {"min_score": "90"}).context["page"]}
        self.assertEqual(names, {self.amr.pk})  # Sara's 99 is a failed attempt, not a verified score
        self.assertNotIn(self.sara.pk, {c.pk for c in self.client.get(reverse("talent_pool"), {"q": "excel"}).context["page"]})

    def test_students_universities_and_anonymous_are_refused(self):
        self.login_student(self.amr_user)
        self.assertEqual(self.client.get(PREVIEW).status_code, 404)
        uni_user = User.objects.create_user("uni", password="pw")
        PartnerProfile.objects.create(user=uni_user, company_name="Some University", partner_code="U1", is_academic=True)
        self.client.force_login(uni_user)
        self.assertEqual(self.client.get(PREVIEW).status_code, 404)
        self.client.logout()
        resp = self.client.get(PREVIEW)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])

    def test_empty_pool_is_honest(self):
        StudentSkill.objects.all().delete()
        self.client.force_login(self.founder)
        resp = self.client.get(PREVIEW)
        self.assertContains(resp, "No verified candidates yet.")
        self.assertContains(resp, "<strong>0</strong>", html=False)
        self.assertNotContains(resp, "Candidate 1")

    def test_upgrade_page_shows_numbers_to_free_accounts_only(self):
        self.client.force_login(self.founder)
        page = self.client.get(reverse("startup_upgrade"))
        self.assertContains(page, "Verified candidates open to offers")
        self.assertContains(page, f'href="{PREVIEW}"')
        activate_pro(self.partner)
        self.assertNotContains(self.client.get(reverse("startup_upgrade")), "Verified candidates open to offers")

    def test_dashboard_links_free_startups_to_preview(self):
        self.client.force_login(self.founder)
        self.assertContains(self.client.get(reverse("partner_dashboard")), f'href="{PREVIEW}"')

    def test_arabic(self):
        self.client.force_login(self.founder)
        self.client.cookies["internest_lang"] = "ar"
        resp = self.client.get(PREVIEW)
        self.assertContains(resp, "معاينة قاعدة المواهب")
        self.assertContains(resp, "مرشح 1")

    def test_query_count_does_not_grow_with_candidates(self):
        self.client.force_login(self.founder)
        self.client.get(PREVIEW)  # warm per-session caches
        with CaptureQueriesContext(connection) as small:
            self.client.get(PREVIEW)
        for i in range(4):
            _u, p = _candidate(f"more{i}")
            _skill(p, "python")
            _skill(p, "excel")
        with CaptureQueriesContext(connection) as big:
            self.client.get(PREVIEW)
        self.assertEqual(len(big), len(small))
