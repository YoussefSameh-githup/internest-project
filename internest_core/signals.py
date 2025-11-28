from django.db.models.signals import post_save
from django.dispatch import receiver
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.contrib.sites.models import Site
from django.conf import settings
import logging # 🚀 إضافة مكتبة التسجيل لتسجيل الأخطاء

from .models import Internship, StudentProfile # استيراد النماذج

# إعداد logging بسيط لتسجيل الأحداث
logger = logging.getLogger(__name__)

def get_absolute_url(path):
    """
    دالة مساعدة لإنشاء رابط مطلق (بما في ذلك الدومين)
    """
    try:
        current_site = Site.objects.get_current()
        # نستخدم إعدادات SSL لتحديد البروتوكول
        protocol = 'https' if settings.SECURE_SSL_REDIRECT else 'http'
        return f"{protocol}://{current_site.domain}{path}"
    except Site.DoesNotExist:
        # حل احتياطي في بيئة التطوير
        return f"http://127.0.0.1:8000{path}"


@receiver(post_save, sender=Internship)
def send_new_internship_notification(sender, instance, created, **kwargs):
    """
    تُطلق هذه الدالة بعد حفظ (إنشاء) نموذج فرصة التدريب.
    """
    # 1. تحقق مما إذا كانت الفرصة جديدة (created=True)
    if created:
        try:
            internship = instance
            partner_name = internship.partner.company_name
            
            # 2. توليد الرابط المطلق لصفحة التدريب
            detail_url_path = reverse('internship_detail', args=[internship.pk])
            absolute_url = get_absolute_url(detail_url_path)
            
            # 3. تجميع قائمة بإيميلات الطلاب (البريد الشخصي فقط)
            # 🚀 التعديل: تصفية الإيميلات التي ليست فارغة أو None
            student_emails = StudentProfile.objects.filter(
                personal_email__isnull=False, # الإيميل ليس None
                personal_email__gt=''         # الإيميل ليس سلسلة فارغة
            ).values_list('personal_email', flat=True)
            
            student_emails = [email for email in student_emails if email]

            # 💡 طباعة للتصحيح:
            print("\n--- SIGNAL EXECUTION LOG ---")
            print(f"Target Internship: {internship.title}")
            print(f"Recipients count: {len(student_emails)}")
            # print(f"Recipient List: {student_emails}") # يمكن إلغاء التعليق لرؤية الإيميلات

            if not student_emails:
                print("SIGNAL WARNING: No valid personal emails found. Skipping notification.")
                return 
            
            # 4. بناء محتوى الإيميل
            subject = f"فرصة تدريب جديدة متاحة: {internship.title} من {partner_name}"
            from_email = settings.DEFAULT_FROM_EMAIL
            
            html_content = render_to_string('emails/new_internship_notification.html', {
                'internship_title': internship.title,
                'partner_name': partner_name,
                'internship_url': absolute_url,
            })
            
            # 5. إرسال الإيميلات
            msg = EmailMultiAlternatives(subject, html_content, from_email, bcc=student_emails)
            msg.attach_alternative(html_content, "text/html")
            
            # 📝 ملاحظة: بما أنك تستخدم Console Backend، هذا سيطبع الرسالة في الطرفية.
            msg.send(fail_silently=False)
            print("--- SIGNAL SUCCESS: Email content printed to console. ---")
            
        except Site.DoesNotExist:
            print("SIGNAL ERROR: Django Sites framework not configured correctly.")
        except Exception as e:
            # 💥 التقاط أي خطأ عام لمنع توقف إجراء Admin
            print(f"--- 💥 CRITICAL ERROR IN SIGNAL EXECUTION: {e.__class__.__name__}: {e} ---")
            logger.error(f"Error sending internship notification: {e}")