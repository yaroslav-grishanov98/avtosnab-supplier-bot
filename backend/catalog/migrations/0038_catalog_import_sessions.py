import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0037_venueproductmatrixitem_unit"),
        ("core", "0040_order_item_unresolved_dismissed"),
    ]

    operations = [
        migrations.CreateModel(
            name="CatalogImportSession",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "domain",
                    models.CharField(
                        choices=[("master", "Мастер-справочник"), ("venue", "Справочник заведения")], max_length=16
                    ),
                ),
                ("source_file_name", models.TextField(blank=True)),
                (
                    "status",
                    models.CharField(
                        choices=[("open", "Открыта"), ("committed", "Завершена")], default="open", max_length=16
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="catalog_import_sessions",
                        to="core.user",
                    ),
                ),
                (
                    "venue",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="catalog_import_sessions",
                        to="core.venue",
                    ),
                ),
            ],
            options={
                "db_table": "catalog_import_sessions",
                "indexes": [
                    models.Index(fields=["owner", "domain", "status"], name="cis_owner_domain_status_idx"),
                    models.Index(fields=["venue", "status"], name="cis_venue_status_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="CatalogImportItem",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("row_number", models.PositiveIntegerField()),
                ("raw_values", models.JSONField(default=dict)),
                ("normalized_values", models.JSONField(default=dict)),
                ("field_sources", models.JSONField(blank=True, default=dict)),
                ("candidates", models.JSONField(blank=True, default=list)),
                ("validation_errors", models.JSONField(blank=True, default=list)),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("review", "Требует проверки"),
                            ("create_new", "Создать новый"),
                            ("update_existing", "Обновить существующий"),
                            ("skip", "Не добавлять"),
                        ],
                        default="review",
                        max_length=24,
                    ),
                ),
                ("target_id", models.UUIDField(blank=True, null=True)),
                ("target_expected_updated_at", models.DateTimeField(blank=True, null=True)),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="catalog.catalogimportsession",
                    ),
                ),
            ],
            options={
                "db_table": "catalog_import_items",
                "ordering": ["row_number"],
                "indexes": [models.Index(fields=["session", "action"], name="cii_session_action_idx")],
            },
        ),
    ]
