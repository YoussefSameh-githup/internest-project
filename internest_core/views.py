# C:\Internest\internest_core\views.py

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.db import IntegrityError 
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages 
from django.db.models import F
from decimal import Decimal # (استيراد Decimal للفلوس)

# استيراد جميع النماذج المطلوبة
from .models import (
    Internship, StudentProfile, Application, 
    PartnerProfile, PartnerInternshipSubmission, PartnerCourseSubmission,
    PartnerApplicantData,
    GamificationTask, TaskAnswer, StudentTaskRecord,
    DiscountCode, StudentEnrollment 
)
# استيراد جميع النماذج/الفورمز المطلوبة
from .forms import (
    ProfileForm, PartnerInternshipForm, PartnerCourseForm, 
    PartnerProfileEditForm,
    TaskQuizForm
)

# ... (كل دوال get_user_context, landing_view, ...إلخ زي ما هي) ...

def get_user_context(request):
    context = {}
    if request.user.is_authenticated:
        try:
            partner_profile = request.user.partnerprofile
            context['has_partner_profile'] = True
            context['partner_profile_obj'] = partner_profile 
        except PartnerProfile.DoesNotExist:
            context['has_partner_profile'] = False
    return context

def landing_view(request):
    context = get_user_context(request)
    return render(request, 'internship/landing_page.html', context)

def login_or_signup_view(request):
    context = get_user_context(request)
    return render(request, 'registration/login_or_signup.html', context)

def signup(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            StudentProfile.objects.create(user=user) 
            login(request, user) 
            messages.success(request, "تم إنشاء الحساب بنجاح! أكمل ملفك الشخصي الآن.")
            return redirect('profile') 
    else:
        form = UserCreationForm()
    context = get_user_context(request)
    context['form'] = form
    return render(request, 'registration/signup.html', context) 

def contact_partner_view(request):
    context = get_user_context(request)
    return render(request, 'internship/partner_contact.html', context)

def partner_code_login(request): 
    error_message = None
    if request.method == 'POST':
        username = request.POST.get('username') 
        partner_code = request.POST.get('partner_code')
        password = request.POST.get('password') 
        try:
            partner_profile = PartnerProfile.objects.filter(
                user__username__iexact=username, 
                partner_code=partner_code
            ).first()
            if partner_profile:
                user = partner_profile.user 
                authenticated_user = authenticate(request, username=user.username, password=password)
                if authenticated_user is not None:
                    login(request, authenticated_user)
                    messages.success(request, f"أهلاً بعودتك، شريكنا {partner_profile.company_name}!")
                    return redirect('partner_dashboard')
                else:
                    error_message = 'كلمة المرور غير صحيحة.'
            else:
                error_message = 'بيانات الشريك غير صحيحة. تأكد من اسم المستخدم والرمز السري.'
        except Exception:
            error_message = 'حدث خطأ غير متوقع أثناء المصادقة.'
        
        context = get_user_context(request)
        context.update({
            'error_message': error_message,
            'username_val': username, 
            'partner_code_val': partner_code
        })
        return render(request, 'registration/partner_login.html', context)
    return render(request, 'registration/partner_login.html', get_user_context(request))

def logout_view(request):
    logout(request)
    messages.info(request, "تم تسجيل خروجك بنجاح.")
    return redirect('landing')

@login_required
def internship_list(request):
    query = request.GET.get('q')
    major = request.GET.get('major')
    internships = Internship.objects.filter(is_active=True).order_by('-deadline') 
    if query:
        internships = internships.filter(title__icontains=query)
    if major:
        internships = internships.filter(required_majors__icontains=major)
    verified_partner_names = set(PartnerProfile.objects.filter(is_fully_verified=True).values_list('company_name', flat=True))
    context = get_user_context(request)
    context.update({
        'internships': internships, 
        'available_majors': ["CS", "Engineering", "Finance", "Marketing"],
        'verified_partner_names': verified_partner_names 
    })
    return render(request, 'internship/list.html', context)

@login_required
def profile_view(request):
    if PartnerProfile.objects.filter(user=request.user).exists():
        messages.warning(request, "أنت مسجل كشريك. تم توجيهك إلى لوحة الشريك.")
        return redirect('partner_dashboard')
    profile, created = StudentProfile.objects.get_or_create(user=request.user)
    if request.method == 'POST':
        form = ProfileForm(request.POST, request.FILES, instance=profile) 
        if form.is_valid():
            form.save()
            profile.calculate_completion() 
            messages.success(request, "تم تحديث ملفك الشخصي بنجاح!")
            return redirect('profile')
    else:
        form = ProfileForm(instance=profile)
    context = get_user_context(request)
    context.update({'profile': profile, 'form': form})
    return render(request, 'internship/profile.html', context)

@login_required
def apply_to_internship(request, internship_id):
    internship = get_object_or_404(Internship, id=internship_id)
    profile = get_object_or_404(StudentProfile, user=request.user)
    if profile.profile_completion_score < 100:
        return redirect('apply_error', internship_id=internship_id)
    try:
         Application.objects.create(internship=internship, applicant=request.user)
         messages.success(request, f"تم التقديم بنجاح على تدريب {internship.title}!")
    except IntegrityError:
         messages.warning(request, f"لقد قمت بالتقديم على تدريب {internship.title} مسبقاً.")
    return redirect('application_success')

def application_success(request):
    return render(request, 'internship/application_success.html', get_user_context(request))

def apply_error(request, internship_id):
    internship = get_object_or_404(Internship, id=internship_id)
    context = get_user_context(request)
    context.update({
        'message': 'يجب إكمال ملفك الشخصي بنسبة 100% للتقديم على هذه الفرصة. أكمل ملفك ثم عُد للمحاولة.', 
        'internship': internship,
        'internship_id': internship_id 
    })
    return render(request, 'internship/apply_error.html', context)

@login_required
def my_applications_view(request):
    if hasattr(request.user, 'partnerprofile'):
        messages.warning(request, "أنت مسجل كشريك. تم توجيهك إلى لوحة الشريك.")
        return redirect('partner_dashboard')
    applications = Application.objects.filter(applicant=request.user).order_by('-application_date')
    context = get_user_context(request)
    context['applications'] = applications
    return render(request, 'internship/my_applications.html', context)

@login_required
def partner_profile_view(request):
    try:
        partner_profile = PartnerProfile.objects.get(user=request.user)
    except PartnerProfile.DoesNotExist:
        messages.error(request, "أنت لست شريكاً مسجلاً.")
        return redirect('landing')
    if request.method == 'POST':
        form = PartnerProfileEditForm(request.POST, request.FILES, instance=partner_profile) 
        if form.is_valid():
            partner_profile = form.save()
            partner_profile.calculate_completion()
            messages.success(request, "تم تحديث بيانات الشريك بنجاح!")
            return redirect('partner_profile')
    else:
        form = PartnerProfileEditForm(instance=partner_profile)
    context = get_user_context(request)
    context.update({
        'partner_profile': partner_profile,
        'form': form,
    })
    return render(request, 'partner/profile.html', context)

@login_required
def partner_dashboard_view(request):
    try:
        partner_profile = PartnerProfile.objects.get(user=request.user)
    except PartnerProfile.DoesNotExist:
        messages.error(request, "أنت غير مصرح لك بالوصول إلى لوحة الشريك.")
        return redirect('landing')
    internship_submissions = PartnerInternshipSubmission.objects.filter(partner=partner_profile).order_by('-submission_date')
    course_submissions = PartnerCourseSubmission.objects.filter(partner=partner_profile).order_by('-submission_date')
    received_applicants = PartnerApplicantData.objects.filter(partner=partner_profile).select_related(
        'student', 'student__user', 'internship'
    ).order_by('-forwarded_on')
    context = get_user_context(request)
    context.update({
        'partner': partner_profile,
        'internship_submissions': internship_submissions,
        'course_submissions': course_submissions,
        'received_applicants': received_applicants
    })
    return render(request, 'partner/dashboard.html', context)

@login_required
def partner_submit_internship(request):
    partner_profile = get_object_or_404(PartnerProfile, user=request.user)
    if partner_profile.profile_completion_score < 100:
        messages.error(request, "يجب إكمال ملف الشريك بنسبة 100% لإرسال طلبات التدريب. يرجى مراجعة ملف الشريك.")
        return redirect('partner_profile')
    if request.method == 'POST':
        form = PartnerInternshipForm(request.POST)
        if form.is_valid():
            submission = form.save(commit=False)
            submission.partner = partner_profile
            submission.status = 'Pending' 
            submission.save()
            messages.success(request, "تم إرسال طلب التدريب للمراجعة بنجاح!")
            return redirect('partner_dashboard') 
    else:
        form = PartnerInternshipForm()
    return render(request, 'internship/submit_internship.html', {'form': form, **get_user_context(request)})

@login_required
def partner_submit_course(request):
    try:
        partner_profile = PartnerProfile.objects.get(user=request.user)
    except PartnerProfile.DoesNotExist:
        return redirect('landing')
    if partner_profile.profile_completion_score < 100:
        messages.error(request, "يجب إكمال ملف الشريك بنسبة 100% لإرسال طلبات الكورسات. يرجى مراجعة ملف الشريك.")
        return redirect('partner_profile')
    if request.method == 'POST':
        form = PartnerCourseForm(request.POST)
        if form.is_valid():
            submission = form.save(commit=False)
            submission.partner = partner_profile
            submission.status = 'Pending' 
            submission.save()
            messages.success(request, "تم إرسال طلب الكورس للمراجعة بنجاح!")
            return redirect('partner_dashboard') 
    else:
        form = PartnerCourseForm()
    return render(request, 'partner/submit_course.html', {'form': form, **get_user_context(request)})

@login_required
def partner_submit_choose_view(request):
    context = get_user_context(request)
    if not context.get('has_partner_profile', False):
         messages.error(request, "أنت غير مصرح لك بالوصول لهذه الصفحة.")
         return redirect('landing')
    return render(request, 'partner/submit_choose.html', context)

@login_required
def task_list_view(request):
    student_profile = request.user.studentprofile
    completed_task_ids = StudentTaskRecord.objects.filter(student=student_profile).values_list('task_id', flat=True)
    available_tasks = GamificationTask.objects.filter(is_active=True).exclude(id__in=completed_task_ids)
    context = get_user_context(request)
    context['tasks'] = available_tasks
    return render(request, 'internship/task_list.html', context) 

@login_required
def take_task_view(request, task_id):
    task = get_object_or_404(GamificationTask, id=task_id)
    student_profile = request.user.studentprofile
    if StudentTaskRecord.objects.filter(student=student_profile, task=task).exists():
        messages.warning(request, "لقد أكملت هذا التاسك من قبل.")
        return redirect('task_list')
    if request.method == 'POST':
        form = TaskQuizForm(request.POST, task=task)
        if form.is_valid():
            total_points = task.points_awarded
            score = 0
            correct_answers = set(TaskAnswer.objects.filter(question__task=task, is_correct=True).values_list('id', flat=True))
            for field_name, answer_id in form.cleaned_data.items():
                if int(answer_id) in correct_answers:
                    score += 1
            num_questions = task.questions.count()
            if num_questions > 0:
                points_earned = int((score / num_questions) * total_points)
            else:
                points_earned = 0
            student_profile.gamification_score = F('gamification_score') + points_earned
            student_profile.save()
            StudentTaskRecord.objects.create(student=student_profile, task=task)
            messages.success(request, f"أحسنت! لقد أكملت التاسك وحصلت على {points_earned} نقطة.")
            return redirect('task_result', task_id=task.id, score=points_earned, total_points=total_points)
    else:
        form = TaskQuizForm(task=task)
    context = get_user_context(request)
    context['task'] = task
    context['form'] = form
    return render(request, 'internship/task_detail.html', context)

@login_required
def task_result_view(request, task_id, score, total_points):
    task = get_object_or_404(GamificationTask, id=task_id)
    context = get_user_context(request)
    context['task'] = task
    context['score'] = score
    context['total_points'] = total_points
    return render(request, 'internship/task_result.html', context)

@login_required
def course_list_view(request):
    courses = PartnerCourseSubmission.objects.filter(status='Approved').order_by('-submission_date')
    student_profile = request.user.studentprofile
    enrolled_course_ids = StudentEnrollment.objects.filter(student=student_profile).values_list('course_id', flat=True)
    context = get_user_context(request)
    context['courses'] = courses
    context['enrolled_course_ids'] = set(enrolled_course_ids)
    return render(request, 'internship/course_list.html', context)

@login_required
def course_checkout_view(request, course_id):
    """(جديد) صفحة الدفع وتطبيق كود الخصم."""
    course = get_object_or_404(PartnerCourseSubmission, id=course_id)
    student_profile = request.user.studentprofile
    
    if StudentEnrollment.objects.filter(student=student_profile, course=course).exists():
        messages.warning(request, "لقد قمت بشراء هذا الكورس من قبل.")
        return redirect('course_list')

    original_price = course.price
    price_after_discount = original_price
    discount_code_str = ""
    discount_amount = Decimal('0.00') 

    if request.method == 'POST':
        if 'apply_discount' in request.POST:
            code_str = request.POST.get('discount_code')
            try:
                discount_code = DiscountCode.objects.get(code__iexact=code_str, is_active=True)
                discount_percentage = Decimal(discount_code.discount_percentage / 100)
                price_after_discount = original_price * (1 - discount_percentage)
                discount_code_str = code_str
                discount_amount = original_price - price_after_discount
                messages.success(request, f"تم تطبيق الخصم بنجاح! السعر الجديد {price_after_discount:.2f} ج.م")
            except DiscountCode.DoesNotExist:
                messages.error(request, "هذا الكود غير صالح أو منتهي الصلاحية.")
        
        elif 'confirm_purchase' in request.POST:
            
            # ---------------------------------
            # --- (✨ هـنـا الـتـعـديـل ✨) ---
            # ---------------------------------
            
            # (1. بنجيب السعر النصي وننضفه)
            final_price_str = request.POST.get('final_price', '0.00') # (e.g., '339,99' or '0,00')
            final_price_cleaned = final_price_str.replace(',', '.')  # (converts to '339.99' or '0.00')
            
            try:
                final_price = Decimal(final_price_cleaned) # (2. بنحول النص النظيف)
            except Exception:
                # (احتياطي لو حصل خطأ)
                final_price = original_price 
                messages.error(request, "حدث خطأ أثناء معالجة السعر، تم استخدام السعر الأصلي.")

            # (3. بنكمل اللوجيك عادي)
            enrollment = StudentEnrollment.objects.create(
                student=student_profile,
                course=course,
                final_price=final_price
            )
            
            student_profile.gamification_score = F('gamification_score') + course.points_awarded
            student_profile.save()
            
            messages.success(request, "تم شراء الكورس بنجاح! تمت إضافة النقاط لحسابك.")
            return redirect('purchase_success', enrollment_id=enrollment.id)

    context = get_user_context(request)
    context.update({
        'course': course,
        'original_price': original_price,
        'price_after_discount': price_after_discount,
        'discount_code_str': discount_code_str,
        'discount_amount': discount_amount, 
    })
    return render(request, 'internship/checkout.html', context)


@login_required
def purchase_success_view(request, enrollment_id):
    """(جديد) صفحة نجاح الشراء اللي بتعرض رابط الكورس."""
    enrollment = get_object_or_404(StudentEnrollment, id=enrollment_id, student=request.user.studentprofile)
    context = get_user_context(request)
    context['enrollment'] = enrollment
    return render(request, 'internship/purchase_success.html', context)