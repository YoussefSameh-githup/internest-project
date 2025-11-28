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