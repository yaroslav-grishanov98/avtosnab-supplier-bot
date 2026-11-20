from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("suppliers", "0047_supplierproduct_supplier_name_trgm_gin")]

    operations = [
        migrations.AddField(
            model_name="supplierproductimportitem",
            name="target_expected_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="supplierproductimportitem",
            name="imported_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
