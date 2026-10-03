from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from internest_core.models import PartnerProfile, StudentProfile
from internest_startups.models import CompanyProfile

from .models import LoungeComment, LoungeFlag, LoungePost, Visibility

LOUNGE = "/startups/lounge/"
BLOCKED = "This space is strictly reserved for verified startup founders."


def _partner(username, verified=True, academic=False):
    user = User.objects.create_user(username, password="pw")
    partner = PartnerProfile.objects.create(user=user, company_name=f"{username} Co", partner_code=f"code-{username}",
                                            is_fully_verified=verified, is_academic=academic)
    if not academic:  # startups must have completed onboarding to reach partner pages
        CompanyProfile.objects.create(partner=partner, industry="software", founded_year=2022, description="Test startup")
    return user


class LoungeTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.founder = _partner("founder")
        cls.peers = [_partner(f"peer{i}") for i in range(3)]
        cls.unverified = _partner("unverified", verified=False)
        cls.university = _partner("uni", academic=True)
        cls.student = User.objects.create_user("student", password="pw")
        StudentProfile.objects.create(user=cls.student, university="Cairo", major="CS", study_level="3")

    def setUp(self):
        translation.activate("en")
        self.client.cookies["internest_lang"] = "en"

    def post_as(self, user, title="Hiring a React intern", body="We need help with our dashboard.", category="hiring"):
        self.client.force_login(user)
        return self.client.post(reverse("lounge_feed"), {"title": title, "body": body, "category": category})


class AccessControlTests(LoungeTestBase):
    def test_url_is_startups_lounge(self):
        self.assertEqual(reverse("lounge_feed"), LOUNGE)

    def _assert_blocked(self, resp):
        self.assertRedirects(resp, reverse("landing"), fetch_redirect_response=False)
        self.assertIn(BLOCKED, [str(m) for m in get_messages(resp.wsgi_request)])

    def test_guest_student_university_and_unverified_partner_are_blocked(self):
        self._assert_blocked(self.client.get(LOUNGE))
        for user in (self.student, self.university, self.unverified):
            self.client.force_login(user)
            self._assert_blocked(self.client.get(LOUNGE))

    def test_student_cannot_post_or_interact_via_direct_urls(self):
        post = LoungePost.objects.create(author=self.founder.partnerprofile, title="t", body="b", body_fingerprint="x")
        self.client.force_login(self.student)
        for name in ("lounge_comment", "lounge_upvote", "lounge_flag"):
            self._assert_blocked(self.client.post(reverse(name, args=[post.pk]), {"body": "hi"}))
        self._assert_blocked(self.client.get(reverse("lounge_post", args=[post.pk])))
        self._assert_blocked(self.client.post(LOUNGE, {"title": "x", "body": "y"}))
        self.assertFalse(LoungeComment.objects.exists())
        self.assertEqual(LoungePost.objects.count(), 1)

    def test_link_hidden_from_student_pages(self):
        self.client.force_login(self.student)
        for name in ("list", "profile", "my_applications", "course_list", "task_list"):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)
            self.assertNotContains(resp, LOUNGE)
            self.assertNotContains(resp, "Founders Lounge")

    def test_verified_founder_sees_link_and_feed(self):
        self.client.force_login(self.founder)
        self.assertContains(self.client.get(reverse("partner_dashboard")), LOUNGE)
        resp = self.client.get(LOUNGE)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Start a discussion")

    def test_unverified_partner_does_not_see_link(self):
        self.client.force_login(self.unverified)
        self.assertNotContains(self.client.get(reverse("partner_dashboard")), LOUNGE)


class AntiSpamTests(LoungeTestBase):
    def test_rate_limit_three_posts_per_hour(self):
        for i in range(3):
            self.post_as(self.founder, title=f"Post {i}", body=f"Unique body number {i}")
        resp = self.post_as(self.founder, title="Post 4", body="Another unique body")
        self.assertEqual(resp.status_code, 200)
        # Free tier: 4th post in an hour is blocked and the gold upgrade modal opens.
        self.assertTrue(resp.context["upsell_open"])
        self.assertEqual(resp.context["upsell_reason"], "hourly")
        self.assertContains(resp, 'id="upsell-dialog" class="upsell-dialog" data-autoopen')
        self.assertContains(resp, "You've reached the free posting limit (3 posts/hour)")
        self.assertEqual(LoungePost.objects.filter(author=self.founder.partnerprofile).count(), 3)

    def test_pro_bypasses_hourly_post_limit(self):
        from internest_startups.tiers import activate_pro
        activate_pro(self.founder.partnerprofile)
        for i in range(6):
            self.post_as(self.founder, title=f"Pro post {i}", body=f"Pro body number {i}")
        self.assertEqual(LoungePost.objects.filter(author=self.founder.partnerprofile).count(), 6)

    def test_pro_is_still_spam_filtered(self):
        from internest_startups.tiers import activate_pro
        activate_pro(self.founder.partnerprofile)
        resp = self.post_as(self.founder, body="Invest in crypto now")
        self.assertContains(resp, "looks like spam")
        self.assertFalse(LoungePost.objects.exists())

    def test_hourly_limit_message_in_arabic(self):
        for i in range(3):
            self.post_as(self.founder, title=f"Post {i}", body=f"Unique body number {i}")
        self.client.cookies["internest_lang"] = "ar"
        resp = self.post_as(self.founder, title="Post 4", body="Another unique body")
        self.assertContains(resp, "وصلت للحد الأقصى للمشاركات المجانية (3 منشورات/ساعة)")

    def test_spam_keywords_are_rejected_before_saving(self):
        for body in ("Invest in crypto now", "Quick cash for founders!", "Best casino bonus", "ربح سريع مضمون"):
            resp = self.post_as(self.founder, body=body)
            self.assertContains(resp, "looks like spam")
        self.assertFalse(LoungePost.objects.exists())

    def test_messaging_app_links_are_auto_hidden(self):
        self.post_as(self.founder, body="Join us https://t.me/freegroup")
        post = LoungePost.objects.get()
        self.assertEqual((post.visibility, post.hidden_reason), (Visibility.HIDDEN, "link_spam"))
        self.client.force_login(self.peers[0])
        self.assertNotContains(self.client.get(LOUNGE), post.title)
        self.assertEqual(self.client.get(reverse("lounge_post", args=[post.pk])).status_code, 404)
        self.client.force_login(self.founder)
        self.assertContains(self.client.get(LOUNGE), "Waiting for admin review")

    def test_duplicate_text_is_auto_hidden(self):
        self.post_as(self.founder, body="Looking for a co-founder in Cairo!!")
        self.post_as(self.peers[0], title="Other title", body="looking for a CO-FOUNDER in cairo")
        dup = LoungePost.objects.get(author=self.peers[0].partnerprofile)
        self.assertEqual((dup.visibility, dup.hidden_reason), (Visibility.HIDDEN, "duplicate"))

    def test_three_flags_hide_post_and_flags_are_unique(self):
        self.post_as(self.founder)
        post = LoungePost.objects.get()
        self.client.force_login(self.founder)
        self.client.post(reverse("lounge_flag", args=[post.pk]))  # own post: ignored
        self.client.force_login(self.peers[0])
        self.client.post(reverse("lounge_flag", args=[post.pk]))
        self.client.post(reverse("lounge_flag", args=[post.pk]))  # duplicate: ignored
        self.assertEqual(LoungeFlag.objects.count(), 1)
        for peer in self.peers[1:]:
            self.client.force_login(peer)
            self.client.post(reverse("lounge_flag", args=[post.pk]))
        post.refresh_from_db()
        self.assertEqual((post.visibility, post.hidden_reason), (Visibility.HIDDEN, "flags"))


class FeedFeatureTests(LoungeTestBase):
    def test_upvote_toggle_nested_comments_and_verified_badge(self):
        self.post_as(self.founder)
        post = LoungePost.objects.get()
        self.client.force_login(self.peers[0])
        self.client.post(reverse("lounge_upvote", args=[post.pk]))
        self.assertEqual(post.upvoters.count(), 1)
        self.client.post(reverse("lounge_upvote", args=[post.pk]))
        self.assertEqual(post.upvoters.count(), 0)

        self.client.post(reverse("lounge_comment", args=[post.pk]), {"body": "Interested!"})
        parent = LoungeComment.objects.get()
        self.client.force_login(self.founder)
        self.client.post(reverse("lounge_comment", args=[post.pk]), {"body": "Great, DM me", "parent_id": parent.pk})
        self.assertEqual(LoungeComment.objects.get(parent=parent).body, "Great, DM me")

        page = self.client.get(reverse("lounge_post", args=[post.pk]))
        self.assertContains(page, "Great, DM me")
        self.assertNotContains(page, "Pro Verified")  # Free accounts: clean name, no gold badge

    def test_tag_filter(self):
        self.post_as(self.founder, title="Need advice", body="pricing?", category="advice")
        self.client.force_login(self.peers[0])
        self.post_as(self.peers[0], title="Hiring now", body="frontend role", category="hiring")
        resp = self.client.get(LOUNGE + "?tag=advice")
        self.assertContains(resp, "Need advice")
        self.assertNotContains(resp, "Hiring now")

    def test_arabic_blocked_message(self):
        self.client.cookies["internest_lang"] = "ar"
        resp = self.client.get(LOUNGE, follow=True)
        self.assertContains(resp, "هذه المساحة مخصّصة حصرياً لمؤسسي الشركات الناشئة الموثّقة.")


class FeedRenderingTests(LoungeTestBase):
    def test_feed_cards_render_avatars_badges_and_actions(self):
        from internest_startups.tiers import activate_pro
        self.founder.first_name, self.founder.last_name = "Mona", "Adel"
        self.founder.save()
        self.post_as(self.founder, title="Hiring frontend", body="React role", category="hiring")
        pro = self.peers[0]
        activate_pro(pro.partnerprofile)
        self.post_as(pro, title="Pro advice", body="Pricing tips", category="advice")
        self.client.force_login(self.peers[1])
        page = self.client.get(reverse("lounge_feed"))
        html = page.content.decode()
        self.assertEqual(html.count('<article class="nw-card'), 2)
        self.assertGreaterEqual(html.count('class="nw-avatar"'), 3)  # 2 posts + composer
        self.assertContains(page, "Mona Adel")
        self.assertContains(page, "founder Co")
        self.assertContains(page, "👑 Pro Verified", count=1)
        self.assertContains(page, 'class="nw-verified"')
        self.assertContains(page, "data-share-url=")
        self.assertContains(page, "data-toggle-comments=")
        self.assertContains(page, "#Hiring")
        self.assertContains(page, "Trending tags")
        self.assertContains(page, "nw-widget--pro")
        self.assertRegex(html, r'/static/lounge/network\.css\?v=[0-9a-f]{10}')
        # Pro posts are pinned first.
        self.assertLess(html.index("Pro advice"), html.index("Hiring frontend"))

    def test_post_detail_uses_card_with_open_thread(self):
        self.post_as(self.founder)
        post = LoungePost.objects.get()
        page = self.client.get(reverse("lounge_post", args=[post.pk]))
        self.assertContains(page, f'id="comments-{post.pk}"')
        self.assertNotContains(page, f'id="comments-{post.pk}" hidden')


class PostEditingRulesTests(LoungeTestBase):
    """Optional titles; authors may edit for 5 minutes and delete for 15 minutes."""

    def _post(self, **kw):
        self.post_as(self.founder, **kw)
        return LoungePost.objects.latest("created_at")

    def _age(self, post, minutes):
        from datetime import timedelta
        from django.utils import timezone
        LoungePost.objects.filter(pk=post.pk).update(created_at=timezone.now() - timedelta(minutes=minutes))

    def test_post_without_title(self):
        resp = self.post_as(self.founder, title="", body="Just a quick thought for fellow founders.")
        self.assertEqual(resp.status_code, 302)
        post = LoungePost.objects.get()
        self.assertEqual(post.title, "")
        page = self.client.get(LOUNGE)
        self.assertContains(page, "Just a quick thought for fellow founders.")
        self.assertNotContains(page, '<h3 class="nw-card__title">')
        self.assertContains(self.client.get(reverse("lounge_post", args=[post.pk])), "<title>Just a quick thought")

    def test_edit_within_5_minutes(self):
        post = self._post()
        self._age(post, 4)
        self.assertContains(self.client.get(LOUNGE), reverse("lounge_edit", args=[post.pk]))
        resp = self.client.post(reverse("lounge_edit", args=[post.pk]), {"title": "", "body": "Edited text", "category": "advice"})
        self.assertRedirects(resp, reverse("lounge_post", args=[post.pk]), fetch_redirect_response=False)
        post.refresh_from_db()
        self.assertEqual((post.title, post.body, post.category), ("", "Edited text", "advice"))

    def test_edit_fails_after_5_minutes(self):
        post = self._post(body="Original body")
        self._age(post, 6)
        self.assertNotContains(self.client.get(LOUNGE), reverse("lounge_edit", args=[post.pk]))
        resp = self.client.post(reverse("lounge_edit", args=[post.pk]), {"body": "Too late"}, follow=True)
        self.assertContains(resp, "Posts can only be edited within 5 minutes of publishing.")
        post.refresh_from_db()
        self.assertEqual(post.body, "Original body")

    def test_edit_still_spam_filtered_and_link_hidden(self):
        post = self._post()
        self.client.post(reverse("lounge_edit", args=[post.pk]), {"body": "Buy crypto now"})
        post.refresh_from_db()
        self.assertNotIn("crypto", post.body)
        self.client.post(reverse("lounge_edit", args=[post.pk]), {"body": "Join https://t.me/group"})
        post.refresh_from_db()
        self.assertEqual(post.visibility, Visibility.HIDDEN)

    def test_delete_within_15_minutes(self):
        post = self._post()
        self._age(post, 14)
        self.assertContains(self.client.get(LOUNGE), reverse("lounge_delete", args=[post.pk]))
        resp = self.client.post(reverse("lounge_delete", args=[post.pk]))
        self.assertRedirects(resp, LOUNGE, fetch_redirect_response=False)
        self.assertFalse(LoungePost.objects.filter(pk=post.pk).exists())

    def test_delete_fails_after_15_minutes(self):
        post = self._post()
        self._age(post, 16)
        self.assertNotContains(self.client.get(LOUNGE), reverse("lounge_delete", args=[post.pk]))
        resp = self.client.post(reverse("lounge_delete", args=[post.pk]), follow=True)
        self.assertContains(resp, "Posts can only be deleted within 15 minutes of publishing.")
        self.assertTrue(LoungePost.objects.filter(pk=post.pk).exists())

    def test_only_author_can_edit_or_delete(self):
        post = self._post()
        self.client.force_login(self.peers[0])
        self.assertEqual(self.client.get(reverse("lounge_edit", args=[post.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("lounge_delete", args=[post.pk])).status_code, 404)
        self.assertNotContains(self.client.get(LOUNGE), reverse("lounge_edit", args=[post.pk]))
        self.assertTrue(LoungePost.objects.filter(pk=post.pk).exists())
