"""IndexNow: payload, retries, every publication path, on_commit scheduling and the DB worker. No real HTTP."""
import io
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from internest_core import async_tasks, indexnow
from internest_core.models import AsyncTask, Internship, PartnerInternshipSubmission, PartnerProfile

KEY = "0123456789abcdef0123456789abcdef"
SITE = "https://internest.app"
ON = dict(SITE_URL=SITE, INDEXNOW_API_KEY=KEY, ASYNC_TASK_BACKEND="db", ASYNC_TASK_MAX_QUEUED=500)
SITEMAP = f"{SITE}/sitemap.xml"


def response(status, headers=None):
    return httpx.Response(status, headers=headers or {}, request=httpx.Request("POST", indexnow.ENDPOINT))


@override_settings(**ON)
class IndexNowCase(TestCase):
    def setUp(self):
        self.partner = PartnerProfile.objects.create(user=User.objects.create_user("co", password="x"), company_name="Acme",
                                                     is_academic=False)
        post = mock.patch.object(indexnow.http_client, "post", return_value=response(202))
        self.post = post.start()
        self.addCleanup(post.stop)

    def job(self, days=9, **kw):
        fields = {"partner": self.partner, "title": "Data intern", "description": "Real work", "location": "Cairo",
                  "required_majors": "any", "deadline": timezone.localdate() + timezone.timedelta(days=days)}
        fields.update(kw)
        with self.captureOnCommitCallbacks(execute=True):
            return Internship.objects.create(**fields)

    def save(self, obj, **fields):
        for name, value in fields.items():
            setattr(obj, name, value)
        with self.captureOnCommitCallbacks(execute=True):
            obj.save()

    def tasks(self):
        return AsyncTask.objects.filter(kind=indexnow.JOB).order_by("pk")

    def queued(self):
        """URL lists of the queued submissions, oldest first; clears them so each step is checked on its own."""
        lists = [task.payload["urls"] for task in self.tasks()]
        self.tasks().delete()
        return lists

    def url(self, obj):
        return f"{SITE}/internship/{obj.pk}/"


class PayloadAndSubmitTests(IndexNowCase):
    def test_payload_has_host_key_location_and_urls(self):
        body = indexnow.payload([f"{SITE}/"])
        self.assertEqual(body, {"host": "internest.app", "key": KEY, "keyLocation": f"{SITE}/{KEY}.txt", "urlList": [f"{SITE}/"]})

    def test_only_public_urls_on_our_origin_are_kept(self):
        kept = indexnow.clean([f"{SITE}/", f"{SITE}/", "https://evil.example/", "http://internest.app/trust/",
                               f"{SITE}/accounts/login/", f"{SITE}/verify/abc/", f"{SITE}/app/dashboard/",
                               f"{SITE}/trust/?ref=x", f"{SITE}/internship/4/"])
        self.assertEqual(kept, [f"{SITE}/", f"{SITE}/internship/4/"])

    def test_public_pages_are_the_seo_pages_plus_sitemap(self):
        urls = indexnow.public_page_urls()
        self.assertIn(f"{SITE}/", urls)
        self.assertIn(SITEMAP, urls)
        self.assertEqual(indexnow.clean(urls), urls)

    def test_accepted_response(self):
        for status in (200, 202):
            self.post.return_value = response(status)
            self.assertEqual(indexnow.submit([f"{SITE}/"]), (200, {"submitted": 1, "indexnow_status": status}))
        args, kwargs = self.post.call_args
        self.assertEqual(args[0], "https://api.indexnow.org/indexnow")
        self.assertEqual(kwargs["json"]["urlList"], [f"{SITE}/"])
        self.assertIsInstance(kwargs["timeout"], httpx.Timeout)

    def test_429_and_5xx_are_retried(self):
        self.post.return_value = response(429, {"Retry-After": "120"})
        with self.assertRaises(async_tasks.RetryTask) as caught:
            indexnow.submit([f"{SITE}/"])
        self.assertEqual(caught.exception.delay, 120)
        self.post.return_value = response(503)
        with self.assertRaises(async_tasks.RetryTask):
            indexnow.submit([f"{SITE}/"])

    def test_permanent_rejection_is_not_retried(self):
        self.post.return_value = response(403)
        with self.assertLogs("internest_core.indexnow", "ERROR") as logs:
            status, body = indexnow.submit([f"{SITE}/"])
        self.assertEqual((status, body["code"]), (502, "indexnow_rejected"))
        self.assertNotIn(KEY, "\n".join(logs.output))

    def test_timeout_is_retryable(self):
        self.post.side_effect = httpx.ReadTimeout("slow")
        with self.assertRaises(async_tasks.RETRYABLE):
            indexnow.submit([f"{SITE}/"])

    @override_settings(INDEXNOW_API_KEY="")
    def test_missing_key_disables_without_http(self):
        self.assertFalse(indexnow.is_configured())
        self.assertEqual(indexnow.submit([f"{SITE}/"])[0], 503)
        self.job()
        self.assertEqual(self.queued(), [])
        self.post.assert_not_called()

    @override_settings(SITE_URL="")
    def test_missing_site_url_disables(self):
        self.job()
        self.assertEqual(self.queued(), [])


class PublicationPathTests(IndexNowCase):
    def test_new_public_internship(self):
        job = self.job()
        self.assertEqual(self.queued(), [[self.url(job), SITEMAP]])

    def test_updated_open_internship(self):
        job = self.job()
        self.queued()
        self.save(job, title="Data analyst intern")
        self.assertEqual(self.queued(), [[self.url(job), SITEMAP]])

    def test_closed_internship_is_recrawled_once(self):
        job = self.job()
        self.queued()
        job.is_active = False
        with self.captureOnCommitCallbacks(execute=True):
            job.save(update_fields=["is_active"])  # partner_close_opportunity
        self.assertEqual(self.queued(), [[self.url(job), SITEMAP]])
        self.save(job, title="Edited while closed")
        self.assertEqual(self.queued(), [])

    def test_reactivated_internship(self):
        job = self.job(is_active=False)
        self.assertEqual(self.queued(), [])
        self.save(job, is_active=True)
        self.assertEqual(self.queued(), [[self.url(job), SITEMAP]])

    def test_deadline_moved_into_the_past_and_back(self):
        job = self.job()
        self.queued()
        self.save(job, deadline=timezone.localdate() - timezone.timedelta(days=1))
        self.assertEqual(self.queued(), [[self.url(job), SITEMAP]])
        self.save(job, deadline=timezone.localdate() + timezone.timedelta(days=5))
        self.assertEqual(self.queued(), [[self.url(job), SITEMAP]])

    def test_never_public_opportunities_are_not_sent(self):
        self.job(is_active=False)
        self.job(days=-3)
        self.assertEqual(self.queued(), [])

    def test_deleting_an_open_internship(self):
        job = self.job()
        url = self.url(job)
        self.queued()
        with self.captureOnCommitCallbacks(execute=True):
            job.delete()
        self.assertEqual(self.queued(), [[url, SITEMAP]])

    def test_repeated_saves_before_the_worker_runs_are_one_task(self):
        job = self.job()
        self.save(job, title="v2")
        self.save(job, title="v3")
        self.assertEqual(self.tasks().count(), 1)


class OnCommitTests(IndexNowCase):
    def test_nothing_is_queued_before_commit(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            Internship.objects.create(partner=self.partner, title="T", description="d", location="Cairo",
                                      required_majors="any", deadline=timezone.localdate() + timezone.timedelta(days=3))
        self.assertEqual(self.tasks().count(), 0)
        for callback in callbacks:
            callback()
        self.assertEqual(self.tasks().count(), 1)

    def test_rolled_back_change_is_never_sent(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    Internship.objects.create(partner=self.partner, title="T", description="d", location="Cairo",
                                              required_majors="any", deadline=timezone.localdate() + timezone.timedelta(days=3))
                    raise RuntimeError("rollback")
            except RuntimeError:
                pass
        self.assertEqual(self.tasks().count(), 0)

    def test_batch_sends_one_request(self):
        with self.captureOnCommitCallbacks(execute=True):
            with indexnow.batch():
                first = Internship.objects.create(partner=self.partner, title="A", description="d", location="Cairo",
                                                  required_majors="any", deadline=timezone.localdate() + timezone.timedelta(days=3))
                second = Internship.objects.create(partner=self.partner, title="B", description="d", location="Cairo",
                                                   required_majors="any", deadline=timezone.localdate() + timezone.timedelta(days=3))
        self.assertEqual(self.queued(), [[self.url(first), SITEMAP, self.url(second)]])


class AdminPathTests(IndexNowCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser("root", "root@x.com", "pw"))

    def action(self, model, action, pks):
        with self.captureOnCommitCallbacks(execute=True):
            page = self.client.post(reverse(f"admin:internest_core_{model}_changelist"),
                                    {"action": action, "_selected_action": [str(pk) for pk in pks]})
        self.assertEqual(page.status_code, 302)

    def test_bulk_close_and_activate_use_update_but_still_notify(self):
        open_job, closed_job, expired_job = self.job(), self.job(is_active=False), self.job(days=-2)
        self.queued()
        self.action("internship", "close_internships", [open_job.pk, closed_job.pk, expired_job.pk])
        self.assertEqual(self.queued(), [[self.url(open_job), SITEMAP]])  # only the one that was public
        self.action("internship", "activate_internships", [open_job.pk, closed_job.pk, expired_job.pk])
        urls = self.queued()
        self.assertEqual(len(urls), 1)
        self.assertCountEqual(urls[0], [self.url(open_job), self.url(closed_job), SITEMAP])  # expired stays private
        self.assertTrue(Internship.objects.get(pk=closed_job.pk).is_active)

    def test_approving_submissions_publishes_in_one_request(self):
        deadline = timezone.localdate() + timezone.timedelta(days=10)
        pending = [PartnerInternshipSubmission.objects.create(partner=self.partner, title=f"S{i}", description="d", location="Cairo",
                                                              required_majors="any", deadline=deadline) for i in range(2)]
        rejected = PartnerInternshipSubmission.objects.create(partner=self.partner, title="R", description="d", location="Cairo",
                                                              required_majors="any", deadline=deadline, status="Rejected")
        self.assertEqual(self.queued(), [])  # a submission alone is never public
        self.action("partnerinternshipsubmission", "approve_submissions", [s.pk for s in pending] + [rejected.pk])
        published = Internship.objects.filter(title__in=["S0", "S1"]).order_by("pk")
        self.assertEqual(published.count(), 2)
        self.assertEqual(self.queued(), [[self.url(published[0]), SITEMAP, self.url(published[1])]])

    def test_rejecting_submissions_sends_nothing(self):
        pending = PartnerInternshipSubmission.objects.create(partner=self.partner, title="P", description="d", location="Cairo",
                                                             required_majors="any", deadline=timezone.localdate())
        self.action("partnerinternshipsubmission", "reject_submissions", [pending.pk])
        self.assertEqual(self.queued(), [])


class WorkerTests(IndexNowCase):
    def run_worker(self):
        call_command("run_async_tasks", "--once", stdout=io.StringIO())

    def test_worker_sends_the_queued_submission(self):
        job = self.job()
        self.run_worker()
        task = self.tasks().get()
        self.assertEqual((task.status, task.result["submitted"]), (AsyncTask.STATUS_DONE, 2))
        self.assertEqual(self.post.call_args.kwargs["json"]["urlList"], [self.url(job), SITEMAP])

    def test_temporary_failure_is_requeued_with_backoff(self):
        self.post.return_value = response(503)
        self.job()
        with self.assertLogs("internest_core.async_tasks", "WARNING"):
            self.assertTrue(async_tasks.run_next())
        task = self.tasks().get()
        self.assertEqual((task.status, task.attempts, task.max_attempts), (AsyncTask.STATUS_QUEUED, 1, indexnow.MAX_ATTEMPTS))
        self.assertGreater(task.next_run_at, timezone.now())

    def test_network_error_never_breaks_publishing(self):
        self.post.side_effect = httpx.ConnectError("down")
        with override_settings(ASYNC_TASK_BACKEND="sync"), self.assertLogs("internest_core.async_tasks", "ERROR"):
            job = self.job()  # worst case: the job runs right after the commit, in the request
        self.assertTrue(Internship.objects.filter(pk=job.pk).exists())

    def test_command_queues_public_pages_and_open_internships_only(self):
        open_job, closed_job = self.job(), self.job(is_active=False)
        self.queued()
        call_command("indexnow_submit", stdout=io.StringIO())
        urls = self.queued()[0]
        self.assertIn(f"{SITE}/", urls)
        self.assertIn(self.url(open_job), urls)
        self.assertNotIn(self.url(closed_job), urls)
        self.assertFalse(any("/accounts/" in u or "/app/" in u for u in urls))
