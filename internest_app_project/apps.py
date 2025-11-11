# apps.py - داخل مجلد internest_core

from django.apps import AppConfig


class InternestCoreConfig(AppConfig):
    # استخدام اسم فئة يطابق ما ورد في settings.py
    default_auto_field = 'django.db.models.BigAutoField'
    # اسم التطبيق الفعلي (اسم المجلد)
    name = 'internest_core'
