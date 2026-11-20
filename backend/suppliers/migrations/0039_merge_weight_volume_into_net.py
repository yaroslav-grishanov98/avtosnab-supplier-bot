# 2026-08-15: по прямой просьбе пользователя — SupplierProduct.weight_value/
# weight_unit и volume_value/volume_unit объединены в одно net_value/net_unit
# (совпадает с макетом Павла: "Вес/объём нетто" — одно попап-поле с 2
# под-полями, не 4 отдельных поля модели). См.
# docs/wiki/logs/2026-08-15-net-value-unit-merge.md.
#
# Перед написанием проверено на прод-БД (SSH-доступ, read-only): из 3366
# товаров 21 имели ОБА значения заполненными одновременно и осмысленно —
# жидкие товары (молоко, шампиньоны в заливке, каперсы маринованные), где вес
# и объём не совпадают из-за плотности, не дубли/мусор. Пользователь явно
# подтвердил: при конфликте побеждает вес, объём для этих 21 строк теряется
# безвозвратно — RunPython ниже не реверсируем (noop назад).
import django.db.models.deletion
from django.db import migrations, models


def merge_weight_volume_into_net(apps, schema_editor):
    SupplierProduct = apps.get_model("suppliers", "SupplierProduct")
    # weight wins on conflict (both set) — user-confirmed rule.
    SupplierProduct.objects.filter(weight_value__isnull=False).update(
        net_value=models.F("weight_value"), net_unit=models.F("weight_unit")
    )
    SupplierProduct.objects.filter(weight_value__isnull=True, volume_value__isnull=False).update(
        net_value=models.F("volume_value"), net_unit=models.F("volume_unit")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0012_seed_extended_units"),
        ("suppliers", "0038_alter_supplierproductprice_supplier_product"),
    ]

    operations = [
        migrations.AddField(
            model_name="supplierproduct",
            name="net_value",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="supplierproduct",
            name="net_unit",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="supplier_products_by_net",
                to="core.unit",
            ),
        ),
        migrations.RunPython(merge_weight_volume_into_net, migrations.RunPython.noop),
        migrations.RemoveField(model_name="supplierproduct", name="weight_value"),
        migrations.RemoveField(model_name="supplierproduct", name="weight_unit"),
        migrations.RemoveField(model_name="supplierproduct", name="volume_value"),
        migrations.RemoveField(model_name="supplierproduct", name="volume_unit"),
    ]
