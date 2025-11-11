# internest_app_project/urls.py (النهائي)

from django.contrib import admin
from django.urls import path, include 
from django.conf import settings
from django.conf.urls.static import static

# === الاستيراد الجديد هنا ===
from internest_core import views as core_views # نستورد الدوال من internest_core/views.py
# ===========================

urlpatterns = [
    path('admin/', admin.site.urls),
    
    # رابط Django Authentication (للدخول والخروج)
    path('accounts/', include('django.contrib.auth.urls')), 
    
    # === رابط التسجيل الجديد (Signup) ===
    path('accounts/signup/', core_views.signup, name='signup'), 
    
    # رابط التطبيق الأساسي (Internest Core)
    path('', include('internest_core.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)