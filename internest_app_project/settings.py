"""
Django settings for Internest_Project project.
"""

from pathlib import Path
import os
# (مبنحتجش dj_database_url هنا)

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# --- (1. المفتاح السري) ---
# (يفضل تحطه كـ Environment Variable في PythonAnywhere)
SECRET_KEY = os.environ.get(
    'SECRET_KEY', 
    'django-insecure-default-key-for-development' # (ده مفتاح وهمي)
)


# --- (✨ 2. التعديل الأهم ✨) ---
# (لازم نقفل الـ DEBUG أونلاين علشان الـ static يشتغل)
DEBUG = False


# --- (3. الـ HOSTS) ---
# (اتأكد إن ده اليوزرنيم بتاعك)
ALLOWED_HOSTS = ['youssefsameh.pythonanywhere.com', '127.0.0.1', 'localhost']


# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    # (شيلنا whitenoise)
    'django.contrib.staticfiles',
    'internest_core.apps.InternestCoreConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # (شيلنا whitenoise)
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

# --- (4. قاعدة البيانات) ---
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

# --- (✨ 5. التعديل الأهم التاني ✨) ---
STATIC_URL = '/static/' # (لازم الـ / في الأول)
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles') 
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, 'internest_core', 'static'),
]

# إعدادات رفع الملفات (الشعارات والصور)
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- (تحديد صفحات الدخول والخروج) ---
LOGIN_REDIRECT_URL = 'list'
LOGIN_URL = 'login_or_signup'
LOGOUT_REDIRECT_URL = 'landing'