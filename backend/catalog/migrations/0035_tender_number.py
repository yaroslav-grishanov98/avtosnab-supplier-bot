from django.db import migrations, models

SEQUENCE = "tenders_number_seq"


class Migration(migrations.Migration):
    """docs/adr/0012: a platform-wide tender number from a database sequence.
    Existing tenders are numbered by creation date, then the sequence
    continues after the largest number."""

    dependencies = [
        ("catalog", "0034_masterproductimportupload"),
    ]

    operations = [
        migrations.RunSQL(f"CREATE SEQUENCE {SEQUENCE};", reverse_sql=f"DROP SEQUENCE {SEQUENCE};"),
        migrations.AddField(
            model_name="tender",
            name="number",
            field=models.PositiveIntegerField(editable=False, null=True),
        ),
        migrations.RunSQL(
            f"""
            UPDATE tenders SET number = ordered.position
            FROM (SELECT id, row_number() OVER (ORDER BY created_at, id) AS position FROM tenders) AS ordered
            WHERE tenders.id = ordered.id;
            SELECT setval('{SEQUENCE}', COALESCE((SELECT max(number) FROM tenders), 0) + 1, false);
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name="tender",
            name="number",
            field=models.PositiveIntegerField(
                db_default=models.Func(
                    models.Value(SEQUENCE), function="nextval", output_field=models.BigIntegerField()
                ),
                editable=False,
                unique=True,
            ),
        ),
    ]
