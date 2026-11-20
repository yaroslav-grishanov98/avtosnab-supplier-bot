from django.contrib.postgres.indexes import GinIndex, OpClass
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations

from core.search_expressions import SearchNameKey


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("suppliers", "0061_supplier_import_commit_status")]
    operations = [
        AddIndexConcurrently(
            model_name="supplierproduct",
            index=GinIndex(
                OpClass(SearchNameKey("supplier_name"), name="gin_trgm_ops"), name="sp_supplier_search_key_gin"
            ),
        ),
        AddIndexConcurrently(
            model_name="supplierproduct",
            index=GinIndex(
                OpClass(SearchNameKey("normalized_name"), name="gin_trgm_ops"), name="sp_normalized_search_key_gin"
            ),
        ),
    ]
