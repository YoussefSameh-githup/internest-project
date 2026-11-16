from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.generic import RedirectView
from django.views.i18n import set_language

from internest_core import social_views, views as core_views

urlpatterns = [
    path('admin/', admin.site.urls),

    # Built-in auth (login, logout, password reset)
    path('accounts/', include('django.contrib.auth.urls')),

    # Signup is defined here so it wins over any nested include.
    path('accounts/signup/', core_views.signup, name='signup'),

    # Social login. allauth lives under /oauth/ so its provider callbacks (/oauth/<provider>/login/callback/) cannot clash with
    # Django's /accounts/ pages; every allauth *account* page (its own login/signup/password UI) is sent to Internest's pages.
    path('oauth-start/<str:role>/<str:provider>/', social_views.social_start, name='social_start'),
    re_path(r'^oauth/(?:login|signup|logout|password|email|confirm-email|reauthenticate|inactive|reset|3rd_party|code|phone|_allauth)(?:/|$)',
            RedirectView.as_view(pattern_name='login_or_signup')),
    path('oauth/', include('allauth.urls')),

    # Language switcher: POST to /i18n/setlang/ persists user's language choice.
    path('i18n/setlang/', set_language, name='set_language'),

    # Protected media serving (works even when DEBUG=False).
    re_path(r'^media/(?P<filepath>.+)$', core_views.serve_protected_media, name='serve_media'),

    path('', include('internest_skills.urls')),
    path('', include('internest_lounge.urls')),
    path('', include('internest_campus.urls')),
    path('', include('internest_startups.urls')),
    path('', include('internest_students.urls')),
    path('', include('internest_core.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
