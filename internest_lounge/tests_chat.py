"""Chat-style Founders Network, Arabic/English profanity moderation and private B2B messages."""
from django.test import SimpleTestCase
from django.urls import reverse

from .models import DirectMessage, LoungeComment, LoungePost
from .profanity import contains_profanity
from .tests import LOUNGE, LoungeTestBase

AR_BLOCKED = "عذراً، يرجى الالتزام بمعايير التواصل المهني وتجنب الألفاظ المحظورة 🛡️"
EN_BLOCKED = "Sorry, please keep the conversation professional and avoid prohibited language 🛡️"


class ProfanityFilterTests(SimpleTestCase):
    def test_blocks_arabic_english_and_franco(self):
        for text in ("this is bullshit", "FUCK you", "fuuuuck this", "sh1t happens", "what a b1tch", "يا شرموطة",
                     "ابن الكلب ده", "كسمك", "يلعن أبوك", "إنتَ مُتناكة", "والشرموطه", "kosomak ya 3ars", "5awal", "يا عرصة"):
            with self.subTest(text=text):
                self.assertTrue(contains_profanity(text))

    def test_allows_ordinary_words(self):
        for text in ("We assess the class of 2026", "Scunthorpe united", "passionate assistant", "classic cocktail recipe",
                     "Dickens novel", "shitake mushrooms", "كسب العملاء مهم", "عرض سعر", "تكسير", "خولة صديقتي", "نيكولا",
                     "نشر إعلان", "كلب الحراسة", "Arsenal fan", "Kosovo office", "عندنا فرصة تدريب"):
            with self.subTest(text=text):
                self.assertFalse(contains_profanity(text))


class ChatFeedTests(LoungeTestBase):
    def test_composer_has_no_title_field(self):
        self.client.force_login(self.founder)
        page = self.client.get(LOUNGE)
        self.assertContains(page, 'class="ch-composer"')
        self.assertContains(page, "data-emoji-toggle")
        self.assertNotContains(page, 'name="title"')
        self.client.post(LOUNGE, {"title": "ignored", "body": "Quick question for founders", "category": ""})
        self.assertEqual(LoungePost.objects.get().title, "")

    def test_profane_post_rejected_with_polite_message(self):
        self.client.force_login(self.founder)
        resp = self.client.post(LOUNGE, {"body": "this deal is bullshit", "category": ""})
        self.assertContains(resp, EN_BLOCKED)
        self.client.cookies["internest_lang"] = "ar"
        resp = self.client.post(LOUNGE, {"body": "يا شرموط", "category": ""})
        self.assertContains(resp, AR_BLOCKED)
        self.assertFalse(LoungePost.objects.exists())

    def test_profane_comment_and_edit_rejected(self):
        post = LoungePost.objects.create(author=self.peers[0].partnerprofile, body="Pricing advice?", body_fingerprint="x")
        self.client.force_login(self.founder)
        resp = self.client.post(reverse("lounge_comment", args=[post.pk]), {"body": "f*ck off asshole"}, follow=True)
        self.assertContains(resp, EN_BLOCKED)
        self.assertFalse(LoungeComment.objects.exists())
        mine = LoungePost.objects.create(author=self.founder.partnerprofile, body="Clean text", body_fingerprint="y")
        resp = self.client.post(reverse("lounge_edit", args=[mine.pk]), {"body": "كسمك", "category": ""}, follow=True)
        self.assertContains(resp, EN_BLOCKED)
        mine.refresh_from_db()
        self.assertEqual(mine.body, "Clean text")

    def test_private_chat_button_on_other_companies_posts_only(self):
        other = LoungePost.objects.create(author=self.peers[0].partnerprofile, body="Hiring designers", body_fingerprint="a")
        LoungePost.objects.create(author=self.founder.partnerprofile, body="My own post", body_fingerprint="b")
        self.client.force_login(self.founder)
        page = self.client.get(LOUNGE)
        self.assertContains(page, reverse("lounge_thread", args=[other.author_id]))
        self.assertNotContains(page, reverse("lounge_thread", args=[self.founder.partnerprofile.pk]))
        self.assertContains(page, "Private chat 💬")


class DirectMessageTests(LoungeTestBase):
    def setUp(self):
        super().setUp()
        self.me, self.you, self.third = (u.partnerprofile for u in (self.founder, self.peers[0], self.peers[1]))

    def _send(self, user, to, text, ajax=False):
        self.client.force_login(user)
        headers = {"HTTP_X_REQUESTED_WITH": "fetch"} if ajax else {}
        return self.client.post(reverse("lounge_thread", args=[to.pk]), {"message_text": text}, **headers)

    def test_send_read_and_unread_badges(self):
        resp = self._send(self.founder, self.you, "Hi, want to partner on a campus event?", ajax=True)
        self.assertEqual(resp.json()["message"]["mine"], True)
        msg = DirectMessage.objects.get()
        self.assertEqual((msg.sender, msg.recipient, msg.is_read), (self.me, self.you, False))
        self.client.force_login(self.peers[0])
        self.assertContains(self.client.get(LOUNGE), 'class="ch-inbox-link">💬 Messages <b>1</b>')
        inbox = self.client.get(reverse("lounge_inbox"))
        self.assertContains(inbox, "founder Co")
        self.assertContains(inbox, 'class="dm-badge"')
        thread = self.client.get(reverse("lounge_thread", args=[self.me.pk]))
        self.assertContains(thread, "Hi, want to partner on a campus event?")
        msg.refresh_from_db()
        self.assertTrue(msg.is_read)

    def test_threads_are_private(self):
        self._send(self.founder, self.you, "Confidential pricing: 500 EGP")
        self._send(self.peers[1], self.me, "Hello from the third company")
        self.client.force_login(self.peers[1])
        # The third company only ever sees its own thread with each partner.
        for partner in (self.me, self.you):
            page = self.client.get(reverse("lounge_thread", args=[partner.pk]))
            self.assertNotContains(page, "Confidential pricing")
            poll = self.client.get(reverse("lounge_thread_poll", args=[partner.pk]), {"after": 0}).json()
            self.assertNotIn("Confidential pricing: 500 EGP", [m["text"] for m in poll["messages"]])
        self.assertNotContains(self.client.get(reverse("lounge_inbox")), "Confidential pricing")

    def test_poll_returns_new_messages_and_marks_read(self):
        self._send(self.founder, self.you, "first")
        first = DirectMessage.objects.get()
        self._send(self.founder, self.you, "second")
        self.client.force_login(self.peers[0])
        data = self.client.get(reverse("lounge_thread_poll", args=[self.me.pk]), {"after": first.pk}).json()
        self.assertEqual([m["text"] for m in data["messages"]], ["second"])
        self.assertTrue(DirectMessage.objects.get(message_text="second").is_read)

    def test_access_rules(self):
        self.client.force_login(self.founder)
        self.assertEqual(self.client.get(reverse("lounge_thread", args=[self.me.pk])).status_code, 404)  # yourself
        unverified = self.unverified.partnerprofile
        self.assertEqual(self.client.get(reverse("lounge_thread", args=[unverified.pk])).status_code, 404)
        self.client.force_login(self.unverified)
        self.assertEqual(self.client.get(reverse("lounge_inbox")).status_code, 302)  # verified startups only
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("lounge_inbox")).status_code, 302)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("lounge_inbox")).status_code, 302)

    def test_profanity_and_empty_messages_rejected(self):
        resp = self._send(self.founder, self.you, "you are a bitch", ajax=True)
        self.assertEqual((resp.status_code, resp.json()["error"]), (400, EN_BLOCKED))
        self.client.cookies["internest_lang"] = "ar"
        resp = self._send(self.founder, self.you, "يا عرص", ajax=True)
        self.assertEqual(resp.json()["error"], AR_BLOCKED)
        self.assertEqual(self._send(self.founder, self.you, "   ", ajax=True).status_code, 400)
        page = self.client.post(reverse("lounge_thread", args=[self.you.pk]), {"message_text": "kosomak"}, follow=True)
        self.assertContains(page, AR_BLOCKED)
        self.assertFalse(DirectMessage.objects.exists())

    def test_rate_limit(self):
        from unittest import mock

        with mock.patch("internest_lounge.moderation.DMS_PER_HOUR", 2):
            for i in range(3):
                resp = self._send(self.founder, self.you, f"message {i}", ajax=True)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(DirectMessage.objects.count(), 2)
