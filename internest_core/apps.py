# apps.py - داخل مجلد internest_core

from django.apps import AppConfig


class InternestCoreConfig(AppConfig):
    # استخدام اسم فئة يطابق ما ورد في settings.py
    default_auto_field = 'django.db.models.BigAutoField'
    # اسم التطبيق الفعلي (اسم المجلد)
    name = 'internest_core'
    
    # 💡 الدالة التي يتم تشغيلها عند بدء التطبيق
    def ready(self):
        # استيراد ملف signals.py لتفعيل الإشارات (signals)
        # هذا يضمن ربط دالة إرسال الإيميل بحدث post_save لنموذج Internship
        import internest_core.signals
        from django.db.models.signals import post_migrate

        post_migrate.connect(_sync_api_registry, sender=self)
        from .async_tasks import autodiscover

        autodiscover()  # registers the <app>/jobs.py async tasks

def _sync_api_registry(sender, **kwargs):
    """After every migrate: make sure each API in internest_core/api_registry.py has its admin row."""
    from .api_monitor import sync_registry

    try:
        sync_registry()
    except Exception:  # never fail a deploy because of monitoring bookkeeping
        import logging

        logging.getLogger(__name__).exception("Could not sync the API registry")
