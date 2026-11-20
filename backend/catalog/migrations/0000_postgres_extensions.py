# Generated manually for PostgreSQL extensions required by the database architecture.

from django.db import migrations


def run_extensions(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Extension name differs by provider ('pgvector' on some managed Postgres
    # builds, 'vector' upstream) — check availability first instead of
    # try/except: a failed CREATE EXTENSION aborts the whole migration
    # transaction, so the except-branch fallback statement fails too
    # ("current transaction is aborted") and neither ever gets created.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT name FROM pg_available_extensions WHERE name IN ('vector', 'pgvector')"
        )
        available = {row[0] for row in cursor.fetchall()}

    if 'vector' in available:
        schema_editor.execute("CREATE EXTENSION IF NOT EXISTS vector")
    elif 'pgvector' in available:
        schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pgvector")


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.RunPython(run_extensions, migrations.RunPython.noop),
    ]
