"""API tests on isolated native PostgreSQL; no .env or external services.

The unrelated embedding field uses text storage because native PG lacks pgvector.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch


def main():
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root))
    os.environ.update(
        DJANGO_SETTINGS_MODULE="config.settings",
        POSTGRES_HOST="127.0.0.1",
        POSTGRES_PORT="55461",
        POSTGRES_DB="bot_work_isolated",
        POSTGRES_USER="bot_test",
        POSTGRES_PASSWORD="",
        PGSSLMODE="disable",
        SERVICE_API_TOKEN="offline-test-token",
        DJANGO_ALLOWED_HOSTS="testserver,localhost,127.0.0.1",
        LANGFUSE_ENABLED="false",
        QDRANT_ENABLED="false",
        QDRANT_SEARCH_ENABLED="false",
    )
    with patch("dotenv.load_dotenv", return_value=False):
        import django

        django.setup()
    from django.conf import settings
    from django.core.management import call_command

    from catalog.models import VectorField

    VectorField.db_type = lambda self, connection: "text"
    # Windows' C locale does not case-fold Cyrillic; callers can select an isolated ICU template.
    test_template = os.environ.get("BOT_TEST_DB_TEMPLATE", "")
    if test_template:
        if not test_template.startswith("bot_work_") or not test_template.replace("_", "").isalnum():
            raise ValueError("BOT_TEST_DB_TEMPLATE must name an isolated bot_work_* test template")
        settings.DATABASES["default"].setdefault("TEST", {})["TEMPLATE"] = test_template
    with patch("urllib.request.urlopen", side_effect=AssertionError("No external requests in backend tests")):
        if sys.argv[1:] == ["--checks"]:
            call_command("check")
            call_command("makemigrations", check=True, dry_run=True, interactive=False)
            return
        call_command(
            "test",
            *(
                sys.argv[1:]
                or [
                    "api.v1.suppliers.test_bot_orders",
                    "api.v1.suppliers.test_bot_company_flow",
                    "api.v1.suppliers.test_bot_notification_flow",
                    "notifications.tests",
                    "api.v1.catalog.test_approval",
                    "api.v1.catalog.test_orders_openapi",
                    "api.v1.suppliers.test_import_reupload_safety",
                ]
            ),
            interactive=False,
            verbosity=1,
        )


if __name__ == "__main__":
    main()
