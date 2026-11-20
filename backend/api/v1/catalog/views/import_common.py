"""Errors shared by the catalog import views."""

from rest_framework.exceptions import APIException


class ImportSessionBusy(APIException):
    status_code = 409
    default_code = "import_session_busy"


class ImportQueueUnavailableError(APIException):
    status_code = 503
    default_detail = "Не удалось поставить обработку импорта в очередь. Загрузите файл ещё раз чуть позже."
    default_code = "queue_unavailable"


class ImportFileStorageUnavailableError(APIException):
    status_code = 503
    default_detail = "Не удалось сохранить исходный файл. Загрузите файл ещё раз чуть позже."
    default_code = "storage_unavailable"
