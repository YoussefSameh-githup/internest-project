
# views.py - داخل تطبيق internest_core

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from .models import Internship, StudentProfile, Application
# سنفترض أنك قمت بإنشاء ملف forms.py ولديه كلاس ProfileForm
from .forms import ProfileForm 
from django.db import IntegrityError # لاستخدامه في معالجة محاولات التقديم المكررة

# === الدوال الأساسية للـ MVP ===

# 1. عرض قائمة الفرص (صفحة البحث)
def internship_list(request):
    # وظيفة البحث والتصفية (MVP)
    query = request.GET.get('q')
    major = request.GET.get('major')
    
    internships = Internship.objects.all().order_by('-deadline')
    
    if query:
        internships = internships.filter(title__icontains=query)
    if major:
        internships = internships.filter(required_majors__icontains=major)
        
    # ملاحظة: قم بتحديث available_majors لتشمل التخصصات التي تستهدفها
    context = {'internships': internships, 'available_majors': ["CS", "Engineering", "Finance", "Marketing"]}
    return render(request, 'internship/list.html', context)

# 2. عرض الملف الشخصي والتعديل (Gamification MVP)
@login_required
def profile_view(request):
    # استخدام get_or_create لضمان وجود ملف لكل مستخدم مسجل الدخول
    profile, created = StudentProfile.objects.get_or_create(user=request.user)
    
    if request.method == 'POST':
        form = ProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            form.save()
            profile.calculate_completion() # تحديث شريط التقدم
            return redirect('profile')
    else:
        form = ProfileForm(instance=profile)
        
    context = {'profile': profile, 'form': form}
    return render(request, 'internship/profile.html', context)

# 3. وظيفة التقديم الموحد (One-Click Apply)
@login_required
def apply_to_internship(request, internship_id):
    internship = get_object_or_404(Internship, id=internship_id)
    profile = get_object_or_404(StudentProfile, user=request.user)
    
    # التحقق من اكتمال الملف (شرط Gamification الأساسي)
    if profile.profile_completion_score < 100:
        # توجيه إلى صفحة الخطأ إذا كان الملف غير مكتمل
        return redirect('apply_error', internship_id=internship_id)

    # إنشاء طلب التقديم
    try:
         # محاولة إنشاء الطلب. إذا كان الطلب موجوداً سيحدث خطأ IntegrityError
         Application.objects.create(internship=internship, applicant=request.user)
    except IntegrityError:
         # إذا كان الطالب قد قدم مسبقاً، نعتبره نجاحاً ونستمر
         pass 
    
    return redirect('application_success')

# === الدوال الجديدة لصفحات التنبيهات ===

# 4. صفحة نجاح التقديم
def application_success(request):
    # يمكن أن نضيف هنا رسالة لطيفة للطالب
    return render(request, 'internship/application_success.html')

# 5. صفحة خطأ التقديم (الملف غير مكتمل)
def apply_error(request, internship_id):
    internship = get_object_or_404(Internship, id=internship_id)
    
    context = {
        'message': 'يجب إكمال ملفك الشخصي بنسبة 100% للتقديم على هذه الفرصة. أكمل ملفك ثم عُد للمحاولة.', 
        'internship': internship,
        'internship_id': internship_id # للمساعدة في العودة للصفحة الصحيحة
    }
    return render(request, 'internship/apply_error.html', context)