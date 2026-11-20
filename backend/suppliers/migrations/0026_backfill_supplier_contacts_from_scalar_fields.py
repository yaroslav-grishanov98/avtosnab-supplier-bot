# Data-only migration, no schema change.
#
# Витя (frontend): "phone/email/website — эти поля не нужны, я не вижу их в Figma, кроме как
# в разделе Контакты — какая-то лишняя дубликация". Он прав — Supplier.phone/email/website
# больше не принимаются/не отдаются через POST /suppliers/mine, PATCH /suppliers/cabinet/me,
# GET-детализацию компании (см. docs/wiki/logs/2026-07-28-remove-duplicate-contact-fields.md) —
# теперь единственный источник контактов в API это SupplierContact (contact_type=email/phone/link).
#
# Supplier.phone/email/website сами по себе НЕ удалены — их всё ещё пишет отдельный легаси
# HTML-кабинет (api/v1/views.py::cabinet_update_proxy, templates/cabinet.html), не связанный
# с этой формой. Но если какой-то поставщик успел задать эти поля через старый DRF-путь до
# этого фикса, их значения станут невидимы в новом contacts-based UI, если не перенести —
# бэкофиллим в SupplierContact (без дублей, если такой контакт уже есть).

from django.db import migrations


def backfill_contacts_from_scalar_fields(apps, schema_editor):
    Supplier = apps.get_model("suppliers", "Supplier")
    SupplierContact = apps.get_model("suppliers", "SupplierContact")

    field_to_contact_type = {
        "phone": "phone",
        "email": "email",
        "website": "link",
    }

    for supplier in Supplier.objects.all():
        for field, contact_type in field_to_contact_type.items():
            value = (getattr(supplier, field) or "").strip()
            if not value:
                continue
            already_exists = SupplierContact.objects.filter(
                supplier=supplier, contact_type=contact_type, value=value
            ).exists()
            if not already_exists:
                SupplierContact.objects.create(supplier=supplier, contact_type=contact_type, value=value)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("suppliers", "0025_backfill_delivery_currency_from_metadata"),
    ]

    operations = [
        migrations.RunPython(backfill_contacts_from_scalar_fields, noop_reverse),
    ]
