from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from accounting.adapters.base import AccountingAdapter
from accounting.adapters.fake import FakeAdapter
from accounting.adapters.storehouse import StoreHouseAdapter
from accounting.dto import AdapterError
from accounting.models.connection import Provider

if TYPE_CHECKING:
    from accounting.models import AccountingConnection

__all__ = ["AccountingAdapter", "FakeAdapter", "StoreHouseAdapter", "get_adapter"]

_IIKO_MODULE = "accounting.adapters.iiko"


@contextmanager
def get_adapter(connection: "AccountingConnection", **adapter_options: Any) -> Iterator[AccountingAdapter]:
    """Yield the adapter for the connection's provider; it is closed (logged out) when the block ends.

    ``adapter_options`` are provider-specific keywords for the provider's adapter (iiko: ``product_units`` and
    ``source_version``, see ``IikoAdapter``); providers without options ignore them.
    """
    if connection.provider == Provider.STOREHOUSE:
        yield StoreHouseAdapter()
        return
    if connection.provider == Provider.IIKO:
        # Imported on demand: the module pulls in the HTTP client and is not needed for other providers.
        try:
            from accounting.adapters.iiko import IikoAdapter
        except ModuleNotFoundError as exc:
            # Only "the adapter module itself is missing" is translated; a missing dependency is a real bug.
            if exc.name != _IIKO_MODULE:
                raise
            raise AdapterError(
                code="adapter_not_available",
                message="Адаптер iiko пока недоступен в этой версии системы.",
                retryable=False,
            ) from None
        with IikoAdapter.for_connection(connection, **adapter_options) as adapter:
            yield adapter
        return
    raise AdapterError(
        code="unknown_provider",
        message="Неизвестный тип учётной системы.",
        retryable=False,
    )
