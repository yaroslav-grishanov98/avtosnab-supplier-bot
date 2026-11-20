from django.db import migrations, models


def convert_operator_to_manager(apps, schema_editor):
    SupplierUser = apps.get_model("suppliers", "SupplierUser")
    SupplierUserInvite = apps.get_model("suppliers", "SupplierUserInvite")
    SupplierInviteLink = apps.get_model("suppliers", "SupplierInviteLink")

    SupplierUser.objects.filter(role="operator").update(role="manager")
    SupplierUserInvite.objects.filter(role="operator").update(role="manager")
    SupplierInviteLink.objects.filter(role="operator").update(role="manager")


def noop_reverse(apps, schema_editor):
    # operator больше не существует как понятие — обратной миграции для этих
    # строк нет (данные о том, что запись раньше была operator, не хранились).
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("suppliers", "0034_remove_seed_test_supplier"),
    ]

    operations = [
        migrations.RunPython(convert_operator_to_manager, noop_reverse),
        migrations.AlterField(
            model_name="supplieruser",
            name="role",
            field=models.CharField(
                choices=[("owner", "Owner"), ("manager", "Manager")],
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="supplieruserinvite",
            name="role",
            field=models.CharField(
                choices=[("owner", "Owner"), ("manager", "Manager")],
                default="manager",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="supplierinvitelink",
            name="role",
            field=models.CharField(
                choices=[("owner", "Owner"), ("manager", "Manager")],
                default="manager",
                max_length=16,
            ),
        ),
    ]
