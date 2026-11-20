from django.db import migrations


def create_test_supplier(apps, schema_editor):
    Supplier = apps.get_model('suppliers', 'Supplier')
    Supplier.objects.create(
        display_name="Тестовый Поставщик",
        legal_name="Тестовый Поставщик",
        status="active",
        is_published=True,
    )


def remove_test_supplier(apps, schema_editor):
    Supplier = apps.get_model('suppliers', 'Supplier')
    Supplier.objects.filter(display_name="Тестовый Поставщик").delete()


class Migration(migrations.Migration):
    dependencies = [
        ('suppliers', '0031_supplierbotlinktoken_channel_and_more'),
    ]

    operations = [
        migrations.RunPython(create_test_supplier, remove_test_supplier),
    ]