from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.i18n import set_language

from internest_core import views as core_views

urlpatterns = [
    path('admin/', admin.site.urls),

    # Built-in auth (login, logout, password reset)
    path('accounts/', include('django.contrib.auth.urls')),

    # Signup is defined here so it wins over any nested include.
    path('accounts/signup/', core_views.signup, name='signup'),

    # Language switcher: POST to /i18n/setlang/ persists user's language choice.
    path('i18n/setlang/', set_language, name='set_language'),

    # Protected media serving (works even when DEBUG=False).
    re_path(r'^media/(?P<filepath>.+)$', core_views.serve_protected_media, name='serve_media'),

    path('', include('internest_core.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
