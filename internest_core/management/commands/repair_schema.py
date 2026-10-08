"""Emergency fix for `no such column: internest_core_application.reviewed_at` when `migrate` can't be run or was faked."""
from django.core.management.base import BaseCommand
from django.db import connection

from internest_core.models import Application
from internest_core.schema_repair import ensure_application_review_columns


class Command(BaseCommand):
    help = "Add missing Application.reviewed_at / decided_at columns (safe to run any number of times)."

    def handle(self, *args, **options):
        with connection.schema_editor() as editor:
            added = ensure_application_review_columns(editor, Application)
        self.stdout.write(self.style.SUCCESS(f"Added columns: {', '.join(added)}" if added else "Schema already up to date."))
