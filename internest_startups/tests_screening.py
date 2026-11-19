"""Phase 2: employer screening tasks as the first filter on applicants."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from internest_core.models import Application, PartnerProfile
from internest_core.tests_applications import ApplicationsBase, _student

from . import entitlements, screening
from .models import ScreeningSubmission, ScreeningTask

TASK = {"title": "Build a mini dashboard", "instructions": "Use the attached CSV and show weekly sales.", "deliverable": "both",
        "criteria_text": "Correct numbers | 4\nClear chart | 3\nShort explanation | 3", "pass_mark": "60", "due_days": "5",
        "required": "on"}
ANSWER = ("I grouped the orders by ISO week, summed the totals and plotted a bar chart; week 42 dropped because of the "
          "holiday, which I note in the summary below the chart.")


class ScreeningBase(ApplicationsBase):
    def setUp(self):
        super().setUp()
        self.amr_user, self.amr = _student("amr")
        self.app = Application.objects.create(internship=self.gig, applicant=self.amr_user)
        self.edit_url = reverse("screening_task_edit", args=[self.gig.pk])

    def _cover(self):
        entitlements.create_pass(self.partner, self.gig, source="admin")

    def _task(self, **kw):
        data = {"internship": self.gig, "title": "Task", "instructions": "Do it", "deliverable": "text",
                "criteria": [{"criterion": "Correct numbers", "points": 4}, {"criterion": "Clear chart", "points": 3},
                             {"criterion": "Short explanation", "points": 3}], "pass_mark": 60, "due_days": 5, "required": True}
        data.update(kw)
        return ScreeningTask.objects.create(**data)


class TaskSetupTests(ScreeningBase):
    def test_creating_a_task_needs_a_pass_for_that_opportunity(self):
        self.client.force_login(self.founder)
        resp = self.client.get(self.edit_url, follow=True)
        self.assertContains(resp, "Screening tasks come with a Hiring Pass for this opportunity.")
        self.client.post(self.edit_url, TASK)
        self.assertFalse(ScreeningTask.objects.exists())
        entitlements.create_pass(self.partner, self.role, source="admin")  # a pass for ANOTHER opportunity is not enough
        self.client.post(self.edit_url, TASK)
        self.assertFalse(ScreeningTask.objects.exists())
        self._cover()
        self.client.post(self.edit_url, TASK)
        task = ScreeningTask.objects.get()
        self.assertEqual(task.criteria, [{"criterion": "Correct numbers", "points": 4}, {"criterion": "Clear chart", "points": 3},
                                         {"criterion": "Short explanation", "points": 3}])
        self.assertEqual((task.pass_mark, task.due_days, task.required, task.deliverable), (60, 5, True, "both"))

    def test_other_companies_and_students_cannot_touch_it(self):
        from .tests_hiring_pass import _company

        _u, _rival = _company("rival")
        self.client.force_login(_u)
        self.assertEqual(self.client.get(self.edit_url).status_code, 404)
        self.login_student(self.amr_user)
        self.assertEqual(self.client.get(self.edit_url).status_code, 404)

    def test_criteria_are_validated(self):
        self._cover()
        self.client.force_login(self.founder)
        for text, error in (("", "This field is required."), ("Good | 11", "Points must be a whole number from 1 to 10."),
                            ("Good | x", "Points must be a whole number from 1 to 10."),
                            ("\n".join(f"C{i} | 1" for i in range(7)), "Use at most 6 criteria.")):
            page = self.client.post(self.edit_url, {**TASK, "criteria_text": text})
            self.assertContains(page, error)
        self.assertFalse(ScreeningTask.objects.exists())

    def test_criteria_lock_once_answers_exist(self):
        self._cover()
        task = self._task()
        screening.submit(task, self.app, ANSWER)
        self.client.force_login(self.founder)
        page = self.client.get(self.edit_url)
        self.assertContains(page, "the criteria and pass mark are locked")
        self.client.post(self.edit_url, {**TASK, "criteria_text": "Anything | 1", "pass_mark": "10", "instructions": "Clearer wording"})
        task.refresh_from_db()
        self.assertEqual((len(task.criteria), task.pass_mark, task.instructions), (3, 60, "Clearer wording"))
        self.client.post(self.edit_url, {"delete": "1"})
        self.assertTrue(ScreeningTask.objects.exists())  # can't be removed once applicants answered


class SubmissionTests(ScreeningBase):
    def setUp(self):
        super().setUp()
        self.task = self._task()
        self.url = reverse("screening_submit", args=[self.app.pk])

    def test_applicant_submits_once_before_the_due_date(self):
        self.login_student(self.amr_user)
        page = self.client.get(self.url)
        self.assertContains(page, "Do it")
        self.assertContains(page, "Required to be shortlisted")
        self.client.post(self.url, {"answer_text": ANSWER})
        sub = ScreeningSubmission.objects.get()
        self.assertEqual((sub.application, sub.status, sub.answer_text), (self.app, "submitted", ANSWER))
        self.client.post(self.url, {"answer_text": "second try"})
        self.assertEqual(ScreeningSubmission.objects.count(), 1)
        self.assertContains(self.client.get(self.url), "Waiting for the company to review it.")

    def test_late_and_empty_answers_are_refused(self):
        self.login_student(self.amr_user)
        self.assertContains(self.client.post(self.url, {"answer_text": "  "}), "Write your answer before submitting.")
        Application.objects.filter(pk=self.app.pk).update(application_date=timezone.now() - timedelta(days=6))
        type(self.task).objects.filter(pk=self.task.pk).update(created_at=timezone.now() - timedelta(days=6))
        self.assertContains(self.client.get(self.url), "The due date for this task has passed.")
        self.client.post(self.url, {"answer_text": ANSWER})
        self.assertFalse(ScreeningSubmission.objects.exists())

    def test_link_must_be_http(self):
        type(self.task).objects.filter(pk=self.task.pk).update(deliverable="link")
        self.login_student(self.amr_user)
        for bad in ("javascript:alert(1)", "data:text/html,x", "ftp://x.test/a", "not a link"):
            self.assertContains(self.client.post(self.url, {"link": bad}), "Enter a valid link starting with https://")
        self.client.post(self.url, {"link": "https://github.com/amr/dashboard"})
        self.assertEqual(ScreeningSubmission.objects.get().link, "https://github.com/amr/dashboard")

    def test_only_the_applicant_sees_their_task_page(self):
        other_user, _ = _student("sara")
        self.login_student(other_user)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_login(self.founder)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_my_applications_links_to_the_task(self):
        self.login_student(self.amr_user)
        self.assertContains(self.client.get(reverse("my_applications")), f'href="{self.url}"')

    def test_near_identical_answers_are_flagged_on_both(self):
        sara_user, _ = _student("sara")
        other_app = Application.objects.create(internship=self.gig, applicant=sara_user)
        first = screening.submit(self.task, other_app, ANSWER)
        second = screening.submit(self.task, self.app, ANSWER.upper().replace(",", ""))  # same words, cosmetic changes
        first.refresh_from_db()
        self.assertTrue(first.similarity_flag and second.similarity_flag)
        third_user, _ = _student("mona")
        third = screening.submit(self.task, Application.objects.create(internship=self.gig, applicant=third_user),
                                 "My own approach: pivot table per week in Excel with a line chart and a note about returns.")
        self.assertFalse(third.similarity_flag)


class ScoringAndGateTests(ScreeningBase):
    def setUp(self):
        super().setUp()
        self.task = self._task()
        self.decide = reverse("partner_applicant_decide", args=[self.app.pk])
        self.client.force_login(self.founder)

    def _shortlist(self):
        return self.client.post(self.decide, {"decision": "shortlist"}, follow=True)

    def test_required_task_blocks_shortlisting_until_passed(self):
        self.assertContains(self._shortlist(), "The applicant hasn&#x27;t submitted it yet.")
        sub = screening.submit(self.task, self.app, ANSWER)
        self.assertContains(self._shortlist(), "Score the screening task first.")
        self.client.post(reverse("screening_score", args=[self.app.pk]), {"points_0": "1", "points_1": "1", "points_2": "1"})
        sub.refresh_from_db()
        self.assertEqual((sub.status, sub.total_pct), ("failed", 30))
        self.assertContains(self._shortlist(), "did not pass the required screening task")
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, Application.STATUS_UNDER_REVIEW if self.app.reviewed_at else self.app.status)
        self.assertNotEqual(self.app.status, Application.STATUS_SHORTLISTED)
        self.client.post(reverse("screening_score", args=[self.app.pk]), {"points_0": "4", "points_1": "2", "points_2": "99", "note": "good"})
        sub.refresh_from_db()
        self.assertEqual((sub.status, sub.total_pct, sub.scores["2"]), ("passed", 90, 3))  # points capped at the maximum
        self._shortlist()
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, Application.STATUS_SHORTLISTED)

    def test_accept_is_gated_too(self):
        self.client.post(self.decide, {"decision": "accept"})
        self.app.refresh_from_db()
        self.assertNotEqual(self.app.status, Application.STATUS_ACCEPTED)

    def test_optional_task_never_blocks(self):
        type(self.task).objects.filter(pk=self.task.pk).update(required=False)
        self._shortlist()
        self.app.refresh_from_db()
        self.assertEqual(self.app.status, Application.STATUS_SHORTLISTED)

    def test_applicant_page_shows_submission_and_score_form_and_still_works_after_the_pass_ends(self):
        screening.submit(self.task, self.app, ANSWER)
        page = self.client.get(reverse("partner_applicant", args=[self.app.pk]))
        self.assertContains(page, "Screening task")
        self.assertContains(page, "ISO week")
        self.assertContains(page, 'name="points_0"')
        self.assertContains(page, "Pass mark: 60%.")
        self.client.post(reverse("screening_score", args=[self.app.pk]), {"points_0": "4", "points_1": "3", "points_2": "3"})
        self.assertEqual(ScreeningSubmission.objects.get().status, "passed")  # no pass needed to score existing answers

    def test_other_company_cannot_score(self):
        from .tests_hiring_pass import _company

        screening.submit(self.task, self.app, ANSWER)
        rival, _partner = _company("rival")
        self.client.force_login(rival)
        self.assertEqual(self.client.post(reverse("screening_score", args=[self.app.pk]), {"points_0": "4"}).status_code, 404)
        self.login_student(self.amr_user)
        self.assertEqual(self.client.post(reverse("screening_score", args=[self.app.pk]), {"points_0": "4"}).status_code, 404)
        self.assertEqual(ScreeningSubmission.objects.get().status, "submitted")

    def test_arabic(self):
        self.client.cookies["internest_lang"] = "ar"
        self.assertContains(self.client.get(reverse("partner_applicant", args=[self.app.pk])), "مهمة الفرز")


class ScreeningReviewFindingsTests(ScreeningBase):
    def setUp(self):
        super().setUp()
        self.task = self._task()
        self.client.force_login(self.founder)

    def test_without_a_pass_a_task_can_still_be_relaxed_but_not_tightened(self):
        screening.submit(self.task, self.app, ANSWER)  # locked by an answer, and no pass at all
        page = self.client.get(self.edit_url)
        self.assertContains(page, "You can still make the task optional or give applicants more time")
        self.client.post(self.edit_url, {**TASK, "required": "", "due_days": "10", "title": "Changed", "pass_mark": "10"})
        self.task.refresh_from_db()
        self.assertEqual((self.task.required, self.task.due_days, self.task.title, self.task.pass_mark), (False, 10, "Task", 60))
        page = self.client.post(self.edit_url, {**TASK, "required": "on", "due_days": "3"})
        self.assertContains(page, "the time to submit can only be extended")
        self.assertContains(page, "can&#x27;t become required")
        self.task.refresh_from_db()
        self.assertEqual((self.task.required, self.task.due_days), (False, 10))

    def test_closed_applications_cannot_submit(self):
        Application.objects.filter(pk=self.app.pk).update(status=Application.STATUS_FULFILLED)
        self.login_student(self.amr_user)
        self.assertEqual(self.client.get(reverse("screening_submit", args=[self.app.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("my_applications")), "Screening task: to do")

    def test_decimal_points_are_rounded_not_zeroed(self):
        sub = screening.submit(self.task, self.app, ANSWER)
        screening.score(sub, {"0": "4.0", "1": "2.6", "2": "x"})
        self.assertEqual(sub.scores, {"0": 4, "1": 3, "2": 0})

    def test_restating_instructions_is_not_flagged(self):
        type(self.task).objects.filter(pk=self.task.pk).update(
            instructions="Use the attached CSV of weekly orders and build a dashboard that shows total sales per week with a chart.")
        self.task.refresh_from_db()
        sara_user, _ = _student("sara")
        other = Application.objects.create(internship=self.gig, applicant=sara_user)
        a = screening.submit(self.task, other, self.task.instructions + " I used Excel pivot tables and a line chart, then wrote a note.")
        b = screening.submit(self.task, self.app, self.task.instructions + " I wrote Python with pandas resample and matplotlib bars.")
        a.refresh_from_db()
        self.assertFalse(a.similarity_flag or b.similarity_flag)
