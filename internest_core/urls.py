# C:\Internest\internest_core\urls.py

from django.urls import path
from . import views

urlpatterns = [
    # 1. الصفحة الرئيسية
    path('', views.landing_view, name='landing'), 
    
    # 2. مصادقة الطلاب
    path('accounts/login-or-signup/', views.login_or_signup_view, name='login_or_signup'),
    path('accounts/signup/', views.signup, name='signup'), 

    # 3. صفحات الطلاب
    path('list/', views.internship_list, name='list'),
    path('profile/', views.profile_view, name='profile'),
    path('apply/<int:internship_id>/', views.apply_to_internship, name='apply'),
    path('apply/success/', views.application_success, name='application_success'),
    path('apply/error/<int:internship_id>/', views.apply_error, name='apply_error'),
    path('app/my-applications/', views.my_applications_view, name='my_applications'),

    # 4. مصادقة الشركاء
    path('partner/login/', views.partner_code_login, name='partner_login'),
    path('partner/contact/', views.contact_partner_view, name='contact_partner'), 
    
    # 5. صفحات الشركاء
    path('partner/dashboard/', views.partner_dashboard_view, name='partner_dashboard'),
    path('partner/profile/', views.partner_profile_view, name='partner_profile'), 
    path('partner/submit/choose/', views.partner_submit_choose_view, name='partner_submit_choose'),
    path('partner/submit/internship/', views.partner_submit_internship, name='partner_submit_internship'),
    path('partner/submit/course/', views.partner_submit_course, name='partner_submit_course'),

    # ---------------------------------
    # --- (✨ 6. لـيـنـكـات الـتـاسـكـات الـجـديـدة ✨) ---
    # ---------------------------------
    path('app/tasks/', views.task_list_view, name='task_list'), # (ده هيعرض قايمة التاسكات)
    path('app/courses/', views.course_list_view, name='course_list'),
    path('app/task/<int:task_id>/', views.take_task_view, name='take_task'), # (ده هيعرض الكويز نفسه)
    path('app/task/result/<int:task_id>/<int:score>/<int:total_points>/', views.task_result_view, name='task_result'), # (ده هيعرض النتيجة)


# ---------------------------------
    # --- (✨ 7. لـيـنـكـات الـدفـع الـجـديـدة ✨) ---
    # ---------------------------------
    path('app/course/<int:course_id>/checkout/', views.course_checkout_view, name='course_checkout'),
    path('app/course/purchase-success/<int:enrollment_id>/', views.purchase_success_view, name='purchase_success'),
]