from django.db import migrations


def remove_test_supplier(apps, schema_editor):
    Supplier = apps.get_model('suppliers', 'Supplier')
    Supplier.objects.filter(display_name="Тестовый Поставщик").delete()


def create_test_supplier(apps, schema_editor):
    Supplier = apps.get_model('suppliers', 'Supplier')
    Supplier.objects.create(
        display_name="Тестовый Поставщик",
        legal_name="Тестовый Поставщик",
        status="active",
        is_published=True,
    )


class Migration(migrations.Migration):
    """
    Откатывает 0032_seed_test_supplier: миграция безусловно сеяла фиктивную
    компанию в любую БД (dev/staging/prod/CI), ломая тесты (5 падений в CI)
    и засоряя реальные окружения. Новая миграция вместо правки уже применённой
    0032 — безопаснее для БД, где та миграция уже накатана.
    """

    dependencies = [
        ('suppliers', '0033_supplierinvitelink'),
    ]

    operations = [
        migrations.RunPython(remove_test_supplier, create_test_supplier),
    ]
