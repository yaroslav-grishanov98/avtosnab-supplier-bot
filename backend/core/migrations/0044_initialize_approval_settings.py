from django.db import migrations
from django.db.models import F


def initialize(apps, schema_editor):
    Venue = apps.get_model('core', 'Venue')
    Settings = apps.get_model('core', 'CompanyApprovalSettings')
    Order = apps.get_model('core', 'Order')
    alias = schema_editor.connection.alias
    batch = []
    for venue_id in Venue.objects.using(alias).values_list('id', flat=True).iterator():
        batch.append(Settings(venue_id=venue_id))
        if len(batch) == 1000:
            Settings.objects.using(alias).bulk_create(batch, ignore_conflicts=True)
            batch = []
    Settings.objects.using(alias).bulk_create(batch, ignore_conflicts=True)
    Order.objects.using(alias).filter(business_updated_at__isnull=True).update(business_updated_at=F('updated_at'))


class Migration(migrations.Migration):
    dependencies = [('core', '0043_orderitem_applied_currency')]
    operations = [migrations.RunPython(initialize, migrations.RunPython.noop)]
