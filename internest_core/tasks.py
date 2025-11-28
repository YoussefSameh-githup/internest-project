# C:\Internest\internest_core\tasks.py

from django.utils import timezone
from .models import Internship, PartnerCourseSubmission # استيراد النماذج الضرورية

def clean_expired_opportunities():
    """
    مهمة تنظيف تُنفذ بشكل دوري (يومي) للتحقق من الفرص التي تجاوزت موعدها النهائي.
    تقوم بتحديث is_active=False في Internship model.
    """
    today = timezone.now().date()
    
    # 1. تحديث التدريبات المنشورة (Internship)
    # تصفية التدريبات التي انتهى ديدلاينها وهي ما زالت نشطة (is_active=True)
    expired_internships = Internship.objects.filter(
        deadline__lt=today, 
        is_active=True
    )
    
    count = expired_internships.count()
    
    if count > 0:
        # إلغاء تنشيط التدريب بدلاً من حذفه
        expired_internships.update(is_active=False)
        print(f"--- INFO: Deactivated {count} expired internships (Deadline passed).")
    else:
        # 🟢 طباعة رسالة إذا لم يتم العثور على شيء
        print("--- INFO: No expired internships found today.")
        
    # 2. تحديث الكورسات (PartnerCourseSubmission)
    # المنطق الحالي يركز على Internship، لذلك نترك هذا القسم كما هو
    
    print("--- SUCCESS: Expiration check complete.")

# ⚠️ تذكير: يجب جدولة هذه الدالة لتُنفذ يومياً (Cron Job) على خادم الإنتاج.