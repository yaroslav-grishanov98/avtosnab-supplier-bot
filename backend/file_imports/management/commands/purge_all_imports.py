"""Полная очистка импортов: сессии импорта (поставщик / каталог / Мастер), записи ImportFile и объекты import_files/
в хранилище (S3). Товары (SupplierProduct, MasterProduct, матрица заведений) не трогаются: на сессии они не ссылаются,
строки сессий удаляются каскадом.

По умолчанию сухой прогон; удаление -- только с --yes."""

from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand

from catalog.models import CatalogImportSession, MasterProductImportUpload
from file_imports.models import ImportFile
from suppliers.models import SupplierProductImportSession

STORAGE_PREFIX = "import_files"


def _storage_keys(prefix: str):
    """Рекурсивно обходит префикс в хранилище и отдаёт ключи файлов."""
    try:
        dirs, files = default_storage.listdir(prefix)
    except FileNotFoundError:
        return
    for name in files:
        yield f"{prefix}/{name}"
    for name in dirs:
        yield from _storage_keys(f"{prefix}/{name}")


class Command(BaseCommand):
    help = "Удаляет все сессии импорта, записи ImportFile и файлы import_files/ из хранилища (сухой прогон без --yes)."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Действительно удалить (без флага -- только счётчики).")

    def handle(self, *args, **options):
        querysets = [
            ("сессии импорта поставщика", SupplierProductImportSession.objects.all()),
            ("сессии импорта каталога", CatalogImportSession.objects.all()),
            ("загрузки Мастера", MasterProductImportUpload.objects.all()),
            ("ImportFile", ImportFile.objects.all()),
        ]
        keys = list(_storage_keys(STORAGE_PREFIX))
        for label, queryset in querysets:
            self.stdout.write(f"{label}: {queryset.count()}")
        self.stdout.write(f"файлов в хранилище ({STORAGE_PREFIX}/): {len(keys)}")

        if not options["yes"]:
            self.stdout.write(self.style.WARNING("Сухой прогон. Для удаления добавьте --yes."))
            return

        failed = 0
        for key in keys:
            try:
                default_storage.delete(key)
            except Exception as error:  # noqa: BLE001
                failed += 1
                self.stderr.write(f"не удалось удалить {key}: {error}")
        for label, queryset in querysets:
            queryset.delete()
        self.stdout.write(self.style.SUCCESS(f"Удалено. Файлов: {len(keys) - failed}, ошибок: {failed}."))
