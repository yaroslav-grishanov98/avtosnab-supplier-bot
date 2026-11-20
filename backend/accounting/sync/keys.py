"""Matching external ids from an adapter to the ids we stored.

iiko GUIDs may come in a different letter case from different methods (see
``accounting/adapters/iiko.py``), so every join between an id from the adapter
and a stored ``external_id`` goes through ``id_key``. The key is used only for
lookups: ``external_id`` columns keep the spelling they were first stored with.
"""

from typing import Any


def id_key(value: Any) -> str:
    """Lookup key of an external id: stripped and lower-cased; "" for a missing or non-text id."""
    return value.strip().lower() if isinstance(value, str) else ""
