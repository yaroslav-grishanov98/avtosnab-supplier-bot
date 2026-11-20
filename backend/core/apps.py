from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"

    def ready(self):
        from django.contrib.postgres.indexes import OpClass
        from django.contrib.postgres.lookups import TrigramWordSimilar
        from django.db.models import TextField
        from django.db.models.indexes import IndexExpression

        # Register the SQL lookup/index wrapper without contrib.postgres's type
        # handlers: import workers use dedicated, unregistered DB connection aliases.
        TextField.register_lookup(TrigramWordSimilar)
        if OpClass not in IndexExpression.wrapper_classes:
            IndexExpression.register_wrappers(*IndexExpression.wrapper_classes, OpClass)
