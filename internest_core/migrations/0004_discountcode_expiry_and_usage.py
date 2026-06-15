from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('internest_core', '0003_studentprofile_personal_email_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='discountcode',
            name='valid_until',
            field=models.DateField(blank=True, null=True, verbose_name='ينتهي في'),
        ),
        migrations.AddField(
            model_name='discountcode',
            name='max_uses',
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name='أقصى عدد استخدامات'),
        ),
        migrations.AddField(
            model_name='discountcode',
            name='times_used',
            field=models.PositiveIntegerField(default=0, verbose_name='عدد مرات الاستخدام'),
        ),
        migrations.AlterField(
            model_name='discountcode',
            name='discount_percentage',
            field=models.PositiveIntegerField(
                help_text='رقم من 1 إلى 100',
                validators=[MinValueValidator(1), MaxValueValidator(100)],
                verbose_name='نسبة الخصم (%)',
            ),
        ),
    ]
