"""deploy/azure/configure_env.py: points the PythonAnywhere .env at the new server without touching anything else.
(The scripts themselves are exercised end to end on Ubuntu 24.04 by .github/workflows/server-setup.yml.)"""
import importlib.util

from django.conf import settings
from django.test import SimpleTestCase

_spec = importlib.util.spec_from_file_location("configure_env", settings.BASE_DIR / "deploy" / "azure" / "configure_env.py")
configure_env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(configure_env)

PYTHONANYWHERE_ENV = """# --- Django core ---
DJANGO_SECRET_KEY=keep-this-exact-secret
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=youssefsameh.pythonanywhere.com,127.0.0.1,localhost
DJANGO_CSRF_TRUSTED_ORIGINS=https://youssefsameh.pythonanywhere.com
DJANGO_SECURE_SSL_REDIRECT=False
BACKUP_DIR=/home/youssefsameh/backups/internest
ANYMAIL_SENDGRID_API_KEY=SG.keep-me
SENTRY_DSN=https://abc@o1.ingest.sentry.io/2
"""


class ConfigureEnvTests(SimpleTestCase):
    def setUp(self):
        self.out = configure_env.configure(PYTHONANYWHERE_ENV, "internest.app", True, "/srv/internest/backups/internest")
        self.values = dict(line.split("=", 1) for line in self.out.splitlines() if "=" in line and not line.startswith("#"))

    def test_points_hosts_csrf_redirect_and_backups_at_the_new_server(self):
        self.assertEqual(self.values["DJANGO_ALLOWED_HOSTS"].split(",")[0], "internest.app")  # first = the health check host
        self.assertIn("youssefsameh.pythonanywhere.com", self.values["DJANGO_ALLOWED_HOSTS"])  # old host still accepted
        self.assertEqual(self.values["DJANGO_CSRF_TRUSTED_ORIGINS"].split(",")[0], "https://internest.app")
        self.assertEqual(self.values["DJANGO_SECURE_SSL_REDIRECT"], "True")
        self.assertEqual(self.values["BACKUP_DIR"], "/srv/internest/backups/internest")

    def test_secrets_and_comments_are_kept_exactly(self):
        for line in ("DJANGO_SECRET_KEY=keep-this-exact-secret", "ANYMAIL_SENDGRID_API_KEY=SG.keep-me",
                     "SENTRY_DSN=https://abc@o1.ingest.sentry.io/2", "# --- Django core ---", "DJANGO_DEBUG=False"):
            self.assertIn(line, self.out.splitlines())

    def test_running_twice_changes_nothing_and_never_duplicates(self):
        again = configure_env.configure(self.out, "internest.app", True, "/srv/internest/backups/internest")
        self.assertEqual(again, self.out)
        self.assertEqual(self.out.count("DJANGO_ALLOWED_HOSTS="), 1)

    def test_missing_keys_are_added_and_no_tls_turns_the_redirect_off(self):
        out = configure_env.configure("DJANGO_SECRET_KEY=x\n", "ci.internest.test", False, "/b")
        values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line and not line.startswith("#"))
        self.assertEqual(values["DJANGO_ALLOWED_HOSTS"], "ci.internest.test,127.0.0.1,localhost")
        self.assertEqual(values["DJANGO_SECURE_SSL_REDIRECT"], "False")
        self.assertEqual(values["DJANGO_SECRET_KEY"], "x")
