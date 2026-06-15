from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from internest_core import views as core_views

urlpatterns = [
    path('admin/', admin.site.urls),

    # Built-in auth (login, logout, password reset)
    path('accounts/', include('django.contrib.auth.urls')),

    # Signup is defined here so it wins over any nested include.
    path('accounts/signup/', core_views.signup, name='signup'),

    path('', include('internest_core.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
