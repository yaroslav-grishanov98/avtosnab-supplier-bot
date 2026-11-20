from django.apps import AppConfig


class SuppliersConfig(AppConfig):
    name = "suppliers"

    def ready(self):
        from suppliers import signals  # noqa: F401
