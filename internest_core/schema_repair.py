"""Idempotent schema helpers for databases where a migration failed half-way or was faked.

Used by migrations 0006/0007 and the `repair_schema` command; every step checks the live schema first,
so running it on a healthy database is a no-op.
"""
from django.db import models

APPLICATION_TABLE = "internest_core_application"
APPLICATION_REVIEW_COLUMNS = ("reviewed_at", "decided_at")

# (old name, new name, columns) for EmailVerification's indexes.
EMAIL_VERIFICATION_INDEXES = (
    ("intnst_evrf_user_type_verified_idx", "internest_c_user_id_5b72d0_idx", ("user_id", "email_type", "verified_at")),
    ("intnst_evrf_code_idx", "internest_c_code_474897_idx", ("code",)),
)


def _columns(connection, table):
    with connection.cursor() as cursor:
        return {c.name for c in connection.introspection.get_table_description(cursor, table)}


def _indexes(connection, table):
    with connection.cursor() as cursor:
        return {name for name, info in connection.introspection.get_constraints(cursor, table).items() if info["index"]}


def ensure_application_review_columns(schema_editor, application_model) -> list[str]:
    """Add Application.reviewed_at / decided_at (nullable datetimes) if the table lacks them."""
    existing = _columns(schema_editor.connection, APPLICATION_TABLE)
    added = []
    for name in APPLICATION_REVIEW_COLUMNS:
        if name not in existing:
            field = models.DateTimeField(null=True, blank=True)
            field.set_attributes_from_name(name)
            schema_editor.add_field(application_model, field)  # ALTER TABLE ... ADD COLUMN <name> datetime NULL
            added.append(name)
    return added


def ensure_email_verification_indexes(schema_editor) -> list[str]:
    """Rename the EmailVerification indexes, tolerating missing or already-renamed ones."""
    table = "internest_core_emailverification"
    qn = schema_editor.quote_name
    existing = _indexes(schema_editor.connection, table)
    done = []
    for old, new, columns in EMAIL_VERIFICATION_INDEXES:
        if new in existing:
            continue
        if old in existing:
            schema_editor.execute(f"DROP INDEX {qn(old)}")
        schema_editor.execute(f"CREATE INDEX {qn(new)} ON {qn(table)} ({', '.join(qn(c) for c in columns)})")
        done.append(new)
    return done
