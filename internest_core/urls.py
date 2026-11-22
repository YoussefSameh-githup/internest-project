from django.urls import path
from . import legal_views, site_views, task_views, university_views, views, views_privacy

urlpatterns = [
    path('api/tasks/<uuid:task_id>/status/', task_views.task_status, name='async_task_status'),
    path('api/tasks/<uuid:task_id>/cancel/', task_views.task_cancel, name='async_task_cancel'),
    path('universities/', university_views.university_landing, name='university_landing'),
    path('universities/inquiry/', university_views.university_inquiry, name='university_inquiry'),
    path('robots.txt', site_views.robots_txt, name='robots'),
    path('sitemap.xml', site_views.sitemap_xml, name='sitemap'),
    path('healthz/', site_views.healthz, name='healthz'),
    path('ops/sentry-check/', site_views.sentry_check, name='sentry_check'),
    path('contact/', legal_views.contact, name='contact'),
    path('privacy/', legal_views.privacy, name='privacy'),
    path('trust/', legal_views.trust_page, name='trust'),
    path('notices/<slug:key>/', views.student_notice_view, name='student_notice'),
    path('terms/', legal_views.terms, name='terms'),
    path('refund-policy/', legal_views.refund_policy, name='refund_policy'),
    # 1. Landing & smart redirect
    path('home/', views.home_redirect_view, name='home_redirect'),
    path('', views.landing_view, name='landing'),

    # 2. Student auth (signup is mounted at the project level)
    path('accounts/login-or-signup/', views.login_or_signup_view, name='login_or_signup'),

    # Email verification
    path('accounts/verify-email/', views.verify_email_view, name='verify_email'),

    # Privacy self-service (students): a copy of my data, delete my account
    path('accounts/my-data/', views_privacy.account_export, name='account_export'),
    path('accounts/delete/', views_privacy.account_delete, name='account_delete'),
    path('accounts/verify-email/resend/<str:email_type>/', views.resend_verification_view, name='resend_verification'),

    # 3. صفحات الطلاب
    path('list/', views.internship_list, name='list'),
    
    # 🚀 النمط الجديد: عرض تفاصيل فرصة تدريب واحدة
    path('internship/<int:pk>/', views.internship_detail_view, name='internship_detail'),
    
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
    path('partner/dashboard/<slug:section>/', views.partner_dashboard_view, name='partner_dashboard_section'),
    path('partner/applicants/<int:app_id>/', views.partner_applicant_view, name='partner_applicant'),
    path('partner/applicants/<int:app_id>/cv/', views.partner_applicant_cv, name='partner_applicant_cv'),
    path('partner/applicants/<int:app_id>/decide/', views.partner_applicant_decide, name='partner_applicant_decide'),
    path('partner/applicants/<int:app_id>/complete/', views.partner_applicant_complete, name='partner_applicant_complete'),
    path('verify/<uuid:uuid>/', views.verify_experience, name='verify_experience'),
    path('verify/<uuid:uuid>/letter.pdf', views.experience_letter, name='experience_letter'),
    path('partner/opportunities/<int:internship_id>/close/', views.partner_close_opportunity, name='partner_close_opportunity'),
    path('partner/profile/', views.partner_profile_view, name='partner_profile'), 
    path('partner/submit/choose/', views.partner_submit_choose_view, name='partner_submit_choose'),
    path('partner/submit/internship/', views.partner_submit_internship, name='partner_submit_internship'),
    path('partner/submit/course/', views.partner_submit_course, name='partner_submit_course'),

    # 6. لينكات التاسكات
    path('app/tasks/', views.task_list_view, name='task_list'),
    path('app/courses/', views.course_list_view, name='course_list'),
    path('app/task/<int:task_id>/', views.take_task_view, name='take_task'),
    path('app/task/result/<int:task_id>/', views.task_result_view, name='task_result'),
    path('partner/courses/enrollments/<int:enrollment_id>/complete/', views.partner_course_complete, name='partner_course_complete'),
    path('courses/certificate/<uuid:certificate_id>/', views.course_certificate, name='course_certificate'),

    # 7. لينكات الدفع الجديدة
    path('app/course/<int:course_id>/checkout/', views.course_checkout_view, name='course_checkout'),
    path('app/course/purchase-success/<int:enrollment_id>/', views.purchase_success_view, name='purchase_success'),
]