"""
Django settings for Internest_Project project.
"""

from pathlib import Path
import os
# (✨ 1. التعديل: شيلنا dj_database_url مبقاش ليه لازمة)

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# --- (✨ 2. التعديل: رجعنا المفتاح السري مكانه مؤقتاً) ---
# (PA عنده طريقة تانية لإخفائه، بس خلينا نشغل الأول)
SECRET_KEY = 'django-insecure-default-key-for-development' # (حط المفتاح الأصلي بتاعك هنا)

# (خلينا DEBUG = True مؤقتاً لحد ما الموقع يشتغل)
DEBUG = True

# (✨ 3. التعديل: ده السطر الصح لـ PA)
# (استبدل 'youssefsameh' باليوزرنيم اللي هتعمله على PythonAnywhere)
ALLOWED_HOSTS = ['youssefsameh.pythonanywhere.com', '127.0.0.1']


# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    # 'whitenoise.runserver_nostatic', (✨ 4. اتمسح)
    'django.contrib.staticfiles',
    'internest_core.apps.InternestCoreConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # 'whitenoise.middleware.WhiteNoiseMiddleware', (✨ 5. اتمسح)
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'internest_app_project.urls'
WSGI_APPLICATION = 'internest_app_project.wsgi.application'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'internest_core', 'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

# --- (✨ 6. التعديل: رجعنا الداتا بيز بسيطة) ---
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',},
]

# Internationalization
LANGUAGE_CODE = 'ar' 
TIME_ZONE = 'Africa/Cairo' 
USE_I18N = True
USE_TZ = True

# --- (✨ 7. التعديل: إعدادات الـ Static لـ PA) ---
STATIC_URL = 'static/'
# (ده المسار اللي PA هيدور فيه على الـ static files بعد ما نجمعها)
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles') 
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, 'internest_core', 'static'),
]
# (شيلنا STATICFILES_STORAGE)

# إعدادات رفع الملفات (الشعارات والصور)
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- (تحديد صفحات الدخول والخروج) ---
LOGIN_REDIRECT_URL = 'list'
LOGIN_URL = 'login_or_signup'
LOGOUT_REDIRECT_URL = 'landing'