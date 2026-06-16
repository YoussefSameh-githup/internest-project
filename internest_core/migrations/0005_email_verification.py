import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('internest_core', '0004_discountcode_expiry_and_usage'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name='studentprofile',
            name='university_email',
            field=models.EmailField(
                blank=True,
                max_length=254,
                null=True,
                unique=True,
                verbose_name='البريد الإلكتروني الجامعي',
            ),
        ),
        migrations.AddField(
            model_name='studentprofile',
            name='personal_email_verified_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='تاريخ توثيق البريد الشخصي'),
        ),
        migrations.AddField(
            model_name='studentprofile',
            name='university_email_verified_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='تاريخ توثيق البريد الجامعي'),
        ),
        migrations.CreateModel(
            name='EmailVerification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email', models.EmailField(max_length=254)),
                ('email_type', models.CharField(
                    choices=[('personal', 'Personal email'), ('university', 'University email')],
                    max_length=20,
                )),
                ('code', models.CharField(max_length=6)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('expires_at', models.DateTimeField()),
                ('verified_at', models.DateTimeField(blank=True, null=True)),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='email_verifications',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'verbose_name': 'Email verification',
                'verbose_name_plural': 'Email verifications',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='emailverification',
            index=models.Index(
                fields=['user', 'email_type', 'verified_at'],
                name='intnst_evrf_user_type_verified_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='emailverification',
            index=models.Index(fields=['code'], name='intnst_evrf_code_idx'),
        ),
    ]
