import logging
from decimal import Decimal, InvalidOperation

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from django.db.models import F
from django.utils import timezone

from .models import (
    Internship, StudentProfile, Application,
    PartnerProfile, PartnerInternshipSubmission, PartnerCourseSubmission,
    PartnerApplicantData,
    GamificationTask, TaskAnswer, StudentTaskRecord,
    DiscountCode, StudentEnrollment,
)
from .forms import (
    ProfileForm, PartnerInternshipForm, PartnerCourseForm,
    PartnerProfileEditForm,
    TaskQuizForm,
    CustomStudentSignupForm,
)

logger = logging.getLogger(__name__)


def _get_student_profile(user):
    """Return the StudentProfile for `user` or None if it doesn't exist."""
    try:
        return user.studentprofile
    except StudentProfile.DoesNotExist:
        return None


def _get_partner_profile(user):
    try:
        return user.partnerprofile
    except PartnerProfile.DoesNotExist:
        return None


def get_user_context(request):
    context = {}
    if request.user.is_authenticated:
        partner_profile = _get_partner_profile(request.user)
        if partner_profile is not None:
            context["has_partner_profile"] = True
            context["partner_profile_obj"] = partner_profile
        else:
            context["has_partner_profile"] = False
    return context


@login_required
def home_redirect_view(request):
    """Send the user to their natural homepage based on account type."""
    if _get_partner_profile(request.user) is not None:
        return redirect("partner_dashboard")
    return redirect("list")


def landing_view(request):
    return render(request, "internship/landing_page.html", get_user_context(request))


def login_or_signup_view(request):
    return render(request, "registration/login_or_signup.html", get_user_context(request))


def signup(request):
    if request.method == "POST":
        form = CustomStudentSignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            personal_email = form.cleaned_data["personal_email"]
            StudentProfile.objects.create(user=user, personal_email=personal_email)
            login(request, user)
            messages.success(request, "تم إنشاء الحساب بنجاح! أكمل ملفك الشخصي الآن.")
            return redirect("profile")
    else:
        form = CustomStudentSignupForm()
    context = get_user_context(request)
    context["form"] = form
    return render(request, "registration/signup.html", context)


def contact_partner_view(request):
    return render(request, "internship/partner_contact.html", get_user_context(request))


def partner_code_login(request):
    error_message = None
    username = ""
    partner_code = ""
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        partner_code = request.POST.get("partner_code", "").strip()
        password = request.POST.get("password", "")

        partner_profile = PartnerProfile.objects.filter(
            user__username__iexact=username,
            partner_code=partner_code,
        ).first()

        if partner_profile is not None:
            authenticated_user = authenticate(
                request,
                username=partner_profile.user.username,
                password=password,
            )
            if authenticated_user is not None:
                login(request, authenticated_user)
                messages.success(
                    request,
                    f"أهلاً بعودتك، شريكنا {partner_profile.company_name}!",
                )
                return redirect("partner_dashboard")
            error_message = "كلمة المرور غير صحيحة."
        else:
            error_message = "بيانات الشريك غير صحيحة. تأكد من اسم المستخدم والرمز السري."

    context = get_user_context(request)
    context.update({
        "error_message": error_message,
        "username_val": username,
        "partner_code_val": partner_code,
    })
    return render(request, "registration/partner_login.html", context)


def logout_view(request):
    logout(request)
    messages.info(request, "تم تسجيل خروجك بنجاح.")
    return redirect("landing")


@login_required
def internship_list(request):
    query = request.GET.get("q", "").strip()
    major = request.GET.get("major", "").strip()

    internships = (
        Internship.objects
        .filter(is_active=True)
        .select_related("partner")
        .order_by("-deadline")
    )

    if query:
        internships = internships.filter(title__icontains=query)
    if major:
        internships = internships.filter(required_majors__icontains=major)

    verified_partner_names = set(
        PartnerProfile.objects
        .filter(is_fully_verified=True)
        .values_list("company_name", flat=True)
    )

    context = get_user_context(request)
    context.update({
        "internships": internships,
        "available_majors": ["CS", "Engineering", "Finance", "Marketing"],
        "verified_partner_names": verified_partner_names,
    })
    return render(request, "internship/list.html", context)


def internship_detail_view(request, pk):
    internship = get_object_or_404(
        Internship.objects.select_related("partner"),
        pk=pk,
    )
    context = get_user_context(request)
    context["internship"] = internship
    return render(request, "internship/detail.html", context)


@login_required
def profile_view(request):
    if _get_partner_profile(request.user) is not None:
        messages.warning(request, "أنت مسجل كشريك. تم توجيهك إلى لوحة الشريك.")
        return redirect("partner_dashboard")

    profile, _ = StudentProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
        form = ProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            profile = form.save()
            profile.calculate_completion()
            messages.success(request, "تم تحديث ملفك الشخصي بنجاح!")
            return redirect("profile")
    else:
        form = ProfileForm(instance=profile)

    context = get_user_context(request)
    context.update({"profile": profile, "form": form})
    return render(request, "internship/profile.html", context)


@login_required
def apply_to_internship(request, internship_id):
    profile = _get_student_profile(request.user)
    if profile is None:
        messages.error(request, "هذه الميزة متاحة للطلاب فقط.")
        return redirect("landing")

    internship = get_object_or_404(Internship, id=internship_id, is_active=True)
    if profile.profile_completion_score < 100:
        return redirect("apply_error", internship_id=internship_id)

    try:
        Application.objects.create(internship=internship, applicant=request.user)
        messages.success(request, f"تم التقديم بنجاح على تدريب {internship.title}!")
    except IntegrityError:
        messages.warning(
            request,
            f"لقد قمت بالتقديم على تدريب {internship.title} مسبقاً.",
        )
    return redirect("application_success")


def application_success(request):
    return render(request, "internship/application_success.html", get_user_context(request))


def apply_error(request, internship_id):
    internship = get_object_or_404(Internship, id=internship_id)
    context = get_user_context(request)
    context.update({
        "message": "يجب إكمال ملفك الشخصي بنسبة 100% للتقديم على هذه الفرصة. أكمل ملفك ثم عُد للمحاولة.",
        "internship": internship,
        "internship_id": internship_id,
    })
    return render(request, "internship/apply_error.html", context)


@login_required
def my_applications_view(request):
    if _get_partner_profile(request.user) is not None:
        messages.warning(request, "أنت مسجل كشريك. تم توجيهك إلى لوحة الشريك.")
        return redirect("partner_dashboard")

    applications = (
        Application.objects
        .filter(applicant=request.user)
        .select_related("internship", "internship__partner")
        .order_by("-application_date")
    )
    context = get_user_context(request)
    context["applications"] = applications
    return render(request, "internship/my_applications.html", context)


@login_required
def partner_profile_view(request):
    partner_profile = _get_partner_profile(request.user)
    if partner_profile is None:
        messages.error(request, "أنت لست شريكاً مسجلاً.")
        return redirect("landing")

    if request.method == "POST":
        form = PartnerProfileEditForm(request.POST, request.FILES, instance=partner_profile)
        if form.is_valid():
            partner_profile = form.save()
            partner_profile.calculate_completion()
            messages.success(request, "تم تحديث بيانات الشريك بنجاح!")
            return redirect("partner_profile")
    else:
        form = PartnerProfileEditForm(instance=partner_profile)

    context = get_user_context(request)
    context.update({"partner_profile": partner_profile, "form": form})
    return render(request, "partner/profile.html", context)


@login_required
def partner_dashboard_view(request):
    partner_profile = _get_partner_profile(request.user)
    if partner_profile is None:
        messages.error(request, "أنت غير مصرح لك بالوصول إلى لوحة الشريك.")
        return redirect("landing")

    internship_submissions = (
        PartnerInternshipSubmission.objects
        .filter(partner=partner_profile)
        .order_by("-submission_date")
    )
    published_internships = (
        Internship.objects
        .filter(partner=partner_profile)
        .order_by("-deadline")
        .prefetch_related("application_set")
    )
    course_submissions = (
        PartnerCourseSubmission.objects
        .filter(partner=partner_profile)
        .order_by("-submission_date")
    )
    received_applicants = (
        PartnerApplicantData.objects
        .filter(partner=partner_profile)
        .select_related("student", "student__user", "internship")
        .order_by("-forwarded_on")
    )

    context = get_user_context(request)
    context.update({
        "partner": partner_profile,
        "internship_submissions": internship_submissions,
        "course_submissions": course_submissions,
        "received_applicants": received_applicants,
        "published_internships": published_internships,
    })
    return render(request, "partner/dashboard.html", context)


def _calc_duration_text(today, deadline):
    total_days = (deadline - today).days
    if total_days >= 30:
        return f"{total_days // 30} أشهر تقريبًا"
    if total_days >= 7:
        return f"{total_days // 7} أسابيع تقريبًا"
    return f"{total_days} أيام متبقية"


@login_required
def partner_submit_internship(request):
    partner_profile = _get_partner_profile(request.user)
    if partner_profile is None:
        messages.error(request, "أنت غير مسجل كشريك.")
        return redirect("landing")

    if partner_profile.profile_completion_score < 100:
        messages.error(
            request,
            "يجب إكمال ملف الشريك بنسبة 100% لإرسال طلبات التدريب.",
        )
        return redirect("partner_profile")

    if request.method == "POST":
        form = PartnerInternshipForm(request.POST)
        if form.is_valid():
            today = timezone.now().date()
            deadline = form.cleaned_data["deadline"]

            if deadline <= today:
                messages.error(request, "تاريخ الموعد النهائي يجب أن يكون بعد اليوم الحالي.")
                context = get_user_context(request)
                context["form"] = form
                return render(request, "partner/submit_internship.html", context)

            submission = form.save(commit=False)
            submission.partner = partner_profile
            submission.status = "Pending"
            submission.duration = _calc_duration_text(today, deadline)
            try:
                submission.save()
                messages.success(request, "تم إرسال طلب التدريب للمراجعة بنجاح!")
                return redirect("partner_dashboard")
            except Exception:
                logger.exception("Failed to save partner internship submission")
                messages.error(request, "حدث خطأ أثناء الحفظ. حاول مرة أخرى.")
        else:
            messages.error(request, "الرجاء تصحيح الأخطاء في النموذج.")
    else:
        form = PartnerInternshipForm()

    context = get_user_context(request)
    context["form"] = form
    return render(request, "partner/submit_internship.html", context)


@login_required
def partner_submit_course(request):
    partner_profile = _get_partner_profile(request.user)
    if partner_profile is None:
        return redirect("landing")

    if partner_profile.profile_completion_score < 100:
        messages.error(
            request,
            "يجب إكمال ملف الشريك بنسبة 100% لإرسال طلبات الكورسات.",
        )
        return redirect("partner_profile")

    if request.method == "POST":
        form = PartnerCourseForm(request.POST)
        if form.is_valid():
            submission = form.save(commit=False)
            submission.partner = partner_profile
            submission.status = "Pending"
            submission.save()
            messages.success(request, "تم إرسال طلب الكورس للمراجعة بنجاح!")
            return redirect("partner_dashboard")
    else:
        form = PartnerCourseForm()

    context = get_user_context(request)
    context["form"] = form
    return render(request, "partner/submit_course.html", context)


@login_required
def partner_submit_choose_view(request):
    context = get_user_context(request)
    if not context.get("has_partner_profile"):
        messages.error(request, "أنت غير مصرح لك بالوصول لهذه الصفحة.")
        return redirect("landing")
    return render(request, "partner/submit_choose.html", context)


@login_required
def task_list_view(request):
    student_profile = _get_student_profile(request.user)
    if student_profile is None:
        messages.warning(request, "التاسكات متاحة للطلاب فقط.")
        return redirect("home_redirect")

    completed_task_ids = StudentTaskRecord.objects.filter(
        student=student_profile,
    ).values_list("task_id", flat=True)
    available_tasks = (
        GamificationTask.objects
        .filter(is_active=True)
        .exclude(id__in=completed_task_ids)
    )
    context = get_user_context(request)
    context["tasks"] = available_tasks
    return render(request, "internship/task_list.html", context)


@login_required
def take_task_view(request, task_id):
    student_profile = _get_student_profile(request.user)
    if student_profile is None:
        messages.warning(request, "التاسكات متاحة للطلاب فقط.")
        return redirect("home_redirect")

    task = get_object_or_404(GamificationTask, id=task_id, is_active=True)

    if StudentTaskRecord.objects.filter(student=student_profile, task=task).exists():
        messages.warning(request, "لقد أكملت هذا التاسك من قبل.")
        return redirect("task_list")

    if request.method == "POST":
        form = TaskQuizForm(request.POST, task=task)
        if form.is_valid():
            correct_answer_ids = set(
                TaskAnswer.objects.filter(
                    question__task=task,
                    is_correct=True,
                ).values_list("id", flat=True)
            )
            score = 0
            for _field_name, answer_id in form.cleaned_data.items():
                try:
                    if int(answer_id) in correct_answer_ids:
                        score += 1
                except (TypeError, ValueError):
                    continue

            num_questions = task.questions.count()
            total_points = task.points_awarded
            points_earned = (
                int((score / num_questions) * total_points) if num_questions else 0
            )

            with transaction.atomic():
                StudentProfile.objects.filter(pk=student_profile.pk).update(
                    gamification_score=F("gamification_score") + points_earned,
                )
                StudentTaskRecord.objects.create(
                    student=student_profile,
                    task=task,
                )

            messages.success(
                request,
                f"أحسنت! لقد أكملت التاسك وحصلت على {points_earned} نقطة.",
            )
            return redirect(
                "task_result",
                task_id=task.id,
                score=points_earned,
                total_points=total_points,
            )
    else:
        form = TaskQuizForm(task=task)

    context = get_user_context(request)
    context["task"] = task
    context["form"] = form
    return render(request, "internship/task_detail.html", context)


@login_required
def task_result_view(request, task_id, score, total_points):
    task = get_object_or_404(GamificationTask, id=task_id)
    context = get_user_context(request)
    context["task"] = task
    context["score"] = score
    context["total_points"] = total_points
    return render(request, "internship/task_result.html", context)


@login_required
def course_list_view(request):
    student_profile = _get_student_profile(request.user)
    if student_profile is None:
        messages.warning(request, "الكورسات متاحة للطلاب فقط.")
        return redirect("home_redirect")

    courses = (
        PartnerCourseSubmission.objects
        .filter(status="Approved")
        .select_related("partner")
        .order_by("-submission_date")
    )
    enrolled_course_ids = set(
        StudentEnrollment.objects
        .filter(student=student_profile)
        .values_list("course_id", flat=True)
    )
    context = get_user_context(request)
    context["courses"] = courses
    context["enrolled_course_ids"] = enrolled_course_ids
    return render(request, "internship/course_list.html", context)


def _discount_session_key(course_id: int) -> str:
    return f"course_discount:{course_id}"


def _compute_final_price(course, discount_code_obj):
    """Compute the final price for a course on the server side."""
    original = course.price
    if not discount_code_obj or not discount_code_obj.is_active:
        return original, Decimal("0.00"), ""
    percentage = Decimal(discount_code_obj.discount_percentage) / Decimal(100)
    discount_amount = (original * percentage).quantize(Decimal("0.01"))
    final = (original - discount_amount).quantize(Decimal("0.01"))
    if final < Decimal("0.00"):
        final = Decimal("0.00")
        discount_amount = original
    return final, discount_amount, discount_code_obj.code


@login_required
def course_checkout_view(request, course_id):
    student_profile = _get_student_profile(request.user)
    if student_profile is None:
        messages.warning(request, "الكورسات متاحة للطلاب فقط.")
        return redirect("home_redirect")

    course = get_object_or_404(PartnerCourseSubmission, id=course_id, status="Approved")

    if StudentEnrollment.objects.filter(student=student_profile, course=course).exists():
        messages.warning(request, "لقد قمت بشراء هذا الكورس من قبل.")
        return redirect("course_list")

    def _lookup_valid_code(code_str):
        candidate = DiscountCode.objects.filter(code__iexact=code_str).first()
        if candidate is None or not candidate.is_valid():
            return None
        return candidate

    session_key = _discount_session_key(course.id)
    discount_code_obj = None
    saved_code = request.session.get(session_key)
    if saved_code:
        discount_code_obj = _lookup_valid_code(saved_code)
        if discount_code_obj is None:
            request.session.pop(session_key, None)

    if request.method == "POST":
        if "apply_discount" in request.POST:
            code_str = request.POST.get("discount_code", "").strip()
            candidate = _lookup_valid_code(code_str)
            if candidate is not None:
                request.session[session_key] = candidate.code
                discount_code_obj = candidate
                final_price, _amount, _code_str = _compute_final_price(course, candidate)
                messages.success(
                    request,
                    f"تم تطبيق الخصم بنجاح! السعر الجديد {final_price:.2f} ج.م",
                )
            else:
                messages.error(request, "هذا الكود غير صالح أو منتهي الصلاحية.")

        elif "confirm_purchase" in request.POST:
            with transaction.atomic():
                discount_for_purchase = None
                if saved_code:
                    discount_for_purchase = (
                        DiscountCode.objects
                        .select_for_update()
                        .filter(code__iexact=saved_code)
                        .first()
                    )
                    if discount_for_purchase is not None and not discount_for_purchase.is_valid():
                        discount_for_purchase = None

                final_price, _amount, _code = _compute_final_price(course, discount_for_purchase)

                enrollment, created = StudentEnrollment.objects.get_or_create(
                    student=student_profile,
                    course=course,
                    defaults={"final_price": final_price},
                )
                if created:
                    StudentProfile.objects.filter(pk=student_profile.pk).update(
                        gamification_score=F("gamification_score") + course.points_awarded,
                    )
                    if discount_for_purchase is not None:
                        DiscountCode.objects.filter(pk=discount_for_purchase.pk).update(
                            times_used=F("times_used") + 1,
                        )

            request.session.pop(session_key, None)
            messages.success(request, "تم شراء الكورس بنجاح! تمت إضافة النقاط لحسابك.")
            return redirect("purchase_success", enrollment_id=enrollment.id)

    final_price, discount_amount, discount_code_str = _compute_final_price(course, discount_code_obj)

    context = get_user_context(request)
    context.update({
        "course": course,
        "original_price": course.price,
        "price_after_discount": final_price,
        "discount_code_str": discount_code_str,
        "discount_amount": discount_amount,
    })
    return render(request, "internship/checkout.html", context)


@login_required
def purchase_success_view(request, enrollment_id):
    student_profile = _get_student_profile(request.user)
    if student_profile is None:
        return redirect("home_redirect")
    enrollment = get_object_or_404(
        StudentEnrollment,
        id=enrollment_id,
        student=student_profile,
    )
    context = get_user_context(request)
    context["enrollment"] = enrollment
    return render(request, "internship/purchase_success.html", context)
