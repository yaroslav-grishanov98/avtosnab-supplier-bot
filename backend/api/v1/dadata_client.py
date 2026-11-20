"""dadata integration — pure external-API wrapper, no Supplier/Venue model
coupling, so it lives at the shared api/v1/ level (like api/v1/validators.py)
rather than under api/v1/suppliers/. Moved here 2026-08-19 when venues grew
its own inn-lookup/address-lookup views that needed the same client without
venues importing from suppliers (this project's peer-app convention — see
docs/wiki/logs/2026-08-15-venue-cabinet-mirror.md)."""

import datetime
import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger("autosnab.dadata_client")

_FIND_BY_ID_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
_SUGGEST_ADDRESS_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address"


class INNLookupError(Exception):
    """Raised when dadata isn't configured, finds nothing, or fails."""


class AddressLookupError(Exception):
    """Raised when dadata isn't configured or fails — mirrors INNLookupError,
    but an empty result is NOT an error here (empty suggestions list is a
    normal "nothing matched yet" response while the user is still typing)."""


def _registration_date_from_dadata(state):
    # dadata отдаёт registration_date как unix-время в миллисекундах.
    raw = state.get("registration_date")
    if not raw:
        return None
    try:
        return datetime.datetime.fromtimestamp(raw / 1000, tz=datetime.timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


_ENTITY_TYPE_BY_DADATA_TYPE = {
    # dadata's party lookup only covers ЕГРЮЛ/ЕГРИП (LEGAL/INDIVIDUAL) — a plain
    # физ.лицо without ИП status has no INN registry entry this endpoint can find,
    # so SupplierEntityType.INDIVIDUAL has no dadata counterpart here.
    "LEGAL": "organization",
    "INDIVIDUAL": "individual_entrepreneur",
}


def _entity_type_from_dadata(dadata_type):
    return _ENTITY_TYPE_BY_DADATA_TYPE.get(dadata_type)


def _street_line_from_dadata(address_data):
    # Улица/дом/корпус одной строкой — то, что в SupplierAddress.street, без
    # региона/города/индекса/квартиры-офиса (те приходят отдельными полями —
    # квартира/офис пришлось вынести отдельно 2026-08-03, раньше была
    # задублирована и тут, и в apartment_office).
    parts = [
        address_data.get("street_with_type"),
        " ".join(filter(None, [address_data.get("house_type"), address_data.get("house")])),
        " ".join(filter(None, [address_data.get("block_type"), address_data.get("block")])),
    ]
    line = ", ".join(part for part in parts if part)
    return line or None


def _apartment_office_from_dadata(address_data):
    # SupplierAddress.apartment_office — квартира/офис/помещение/комната
    # одной строкой. dadata's flat_type уже покрывает все эти варианты
    # ("кв", "оф", "помещ", "ком" и т.п.) под одним полем; отдельного поля
    # под этаж dadata не отдаёт вообще (не входит в ФИАС/КЛАДР).
    return " ".join(filter(None, [address_data.get("flat_type"), address_data.get("flat")])) or None


def lookup_company_by_inn(inn):
    """Look up a company's registry data by INN via dadata's findById/party.

    Returns a dict with legal_name/address/status/kpp/management, or raises
    INNLookupError with a message safe to show to staff (not found, not
    configured, or upstream error). Alongside the flat `address` string, also
    returns it split into address_type/region/district/city_type/city/street/
    apartment_office/postal_code — matching SupplierAddress's structured
    fields — so the frontend can prefill a typed address record without
    parsing the string itself.
    """
    api_key = str(getattr(settings, "DADATA_API_KEY", "") or "").strip()
    secret_key = str(getattr(settings, "DADATA_SECRET_KEY", "") or "").strip()
    if not api_key or not secret_key:
        raise INNLookupError("Проверка по ИНН не настроена: нет ключей dadata.")

    request = urllib.request.Request(
        _FIND_BY_ID_URL,
        data=json.dumps({"query": inn}).encode("utf-8"),
        headers={
            "Authorization": f"Token {api_key}",
            "X-Secret": secret_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace") if error.fp else ""
        logger.warning("dadata.http_error inn=%s status=%s body=%s", inn, error.code, body)
        raise INNLookupError(f"dadata вернула ошибку {error.code}.") from error
    except urllib.error.URLError as error:
        logger.warning("dadata.network_error inn=%s error=%s", inn, error)
        raise INNLookupError("dadata недоступна (сетевая ошибка).") from error

    suggestions = payload.get("suggestions") or []
    if not suggestions:
        raise INNLookupError("Компания с таким ИНН не найдена в dadata.")

    data = suggestions[0].get("data") or {}
    management = data.get("management") or {}
    address = data.get("address") or {}
    address_data = address.get("data") or {}
    state = data.get("state") or {}

    name = data.get("name") or {}
    logger.info("dadata.lookup.finish inn=%s status=%s", inn, state.get("status"))
    return {
        "inn": data.get("inn"),
        "kpp": data.get("kpp"),
        "ogrn": data.get("ogrn"),
        "legal_name": name.get("full_with_opf"),
        "short_name": name.get("short_with_opf"),
        "entity_type": _entity_type_from_dadata(data.get("type")),
        "management_name": management.get("name"),
        "management_post": management.get("post"),
        "address": address.get("unrestricted_value") or address.get("value"),
        "address_type": "legal",
        "region": address_data.get("region_with_type"),
        "district": address_data.get("area_with_type") or address_data.get("city_district_with_type"),
        "city_type": address_data.get("city_type_full") or address_data.get("settlement_type_full"),
        "city": address_data.get("city") or address_data.get("settlement"),
        "street": _street_line_from_dadata(address_data),
        "apartment_office": _apartment_office_from_dadata(address_data),
        "postal_code": address_data.get("postal_code"),
        "status": state.get("status"),
        "okved": data.get("okved"),
        "okpo": data.get("okpo"),
        "registration_date": _registration_date_from_dadata(state),
    }


def suggest_addresses(query, *, count=10):
    """Address autocomplete via dadata's suggest/address — same integration
    shape as lookup_company_by_inn (settings check, request/error handling,
    _street_line_from_dadata/_apartment_office_from_dadata reused as-is,
    since suggest/address returns the same "data" structure as the address
    embedded in findById/party's response). Unlike INN lookup, this is a
    live-typing autocomplete (dadata itself ranks/limits by `count`) — no
    checksum to validate upfront, and no result is a normal state (user
    hasn't typed enough yet), not an error.

    2026-08-09: "Карточка компании — поиск по адресу по тому же принципу,
    что по ИНН" (явный запрос пользователя) — suggest/address only needs
    the Token header, not X-Secret (that's only required by dadata's
    "standalone" methods like findById) — kept the same api_key/secret_key
    settings gate as INN lookup anyway, so one missing/wrong dadata
    credential disables both features together rather than one silently
    working and the other not.
    """
    api_key = str(getattr(settings, "DADATA_API_KEY", "") or "").strip()
    secret_key = str(getattr(settings, "DADATA_SECRET_KEY", "") or "").strip()
    if not api_key or not secret_key:
        raise AddressLookupError("Поиск по адресу не настроен: нет ключей dadata.")

    request = urllib.request.Request(
        _SUGGEST_ADDRESS_URL,
        data=json.dumps({"query": query, "count": count}).encode("utf-8"),
        headers={
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace") if error.fp else ""
        logger.warning("dadata.address_suggest.http_error query=%s status=%s body=%s", query, error.code, body)
        raise AddressLookupError(f"dadata вернула ошибку {error.code}.") from error
    except urllib.error.URLError as error:
        logger.warning("dadata.address_suggest.network_error query=%s error=%s", query, error)
        raise AddressLookupError("dadata недоступна (сетевая ошибка).") from error

    results = []
    for suggestion in payload.get("suggestions") or []:
        address_data = suggestion.get("data") or {}
        results.append(
            {
                "value": suggestion.get("unrestricted_value") or suggestion.get("value"),
                "country": address_data.get("country"),
                "region": address_data.get("region_with_type"),
                "district": address_data.get("area_with_type") or address_data.get("city_district_with_type"),
                "locality": address_data.get("city_with_type") or address_data.get("settlement_with_type"),
                "street": _street_line_from_dadata(address_data),
                "apartment_office": _apartment_office_from_dadata(address_data),
                "postal_code": address_data.get("postal_code"),
            }
        )
    logger.info("dadata.address_suggest.finish query=%s results=%s", query, len(results))
    return results
