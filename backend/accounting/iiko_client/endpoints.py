"""Registry of iikoServer endpoints the accounting integration may call (ported from iiko_diagnostics).

Only the routes the integration reads are kept; the prototype's probes for suspect routes and presets are not.
Status of every route:
  OFFICIAL         - described in the official iikoServer API documentation
                     (https://ru.iiko.help/articles/#!api-documentations/iikoserver-api);
  SECONDARY_SOURCE - found only in third-party clients (pyiiko).
Behaviour on the test server is recorded in docs/wiki/integrations/iiko-capability-matrix.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from .exceptions import ErrorKind, IikoError

DOC_ROOT = "https://ru.iiko.help/articles/#!api-documentations/"
PYIIKO = "pyiiko (сторонний клиент, github.com/gadzhi/pyiiko)"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"
XML_ACCEPT: Mapping[str, str] = MappingProxyType({"Accept": "application/xml"})


def _official(article: str) -> str:
    return f"официальная документация iikoServer API: {DOC_ROOT}{article}"


class DocStatus(str, Enum):
    OFFICIAL = "official"
    SECONDARY_SOURCE = "secondary_source"


@dataclass(frozen=True)
class EndpointSpec:
    key: str
    path: str  # relative to the base URL (".../resto/")
    description: str
    response_format: str  # expected: "json" | "xml" | "text"; the real format is detected, not assumed
    requires_auth: bool = True
    method: str = "GET"
    params: Mapping[str, Any] = field(default_factory=dict)
    required_params: tuple[str, ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)
    doc_status: DocStatus = DocStatus.OFFICIAL
    source: str = ""
    size_bounded: bool = True  # False: the API has no documented limit, the response is capped by bytes
    notes: str = ""


_SPECS = (
    EndpointSpec(
        "server_info",
        "get_server_info.jsp",
        "Информация о сервере (версия, редакция)",
        "xml",
        requires_auth=False,
        params={"encoding": "UTF-8"},
        doc_status=DocStatus.SECONDARY_SOURCE,
        source=PYIIKO + ": server.py version()/server_info()",
        notes="Публичный метод; используются только версия и редакция.",
    ),
    EndpointSpec(
        "auth",
        "api/auth",
        "Авторизация (login + SHA-1 пароля -> токен)",
        "text",
        requires_auth=False,
        source=_official("avtorizatsiya"),
        notes="Занимает один слот лицензии до api/logout. Токен: параметр key или cookie key.",
    ),
    EndpointSpec("logout", "api/logout", "Завершение сессии (key=<токен>)", "text", source=_official("avtorizatsiya")),
    EndpointSpec(
        "departments",
        "/api/corporation/departments",
        "Подразделения (иерархия, все типы)",
        "xml",
        headers=XML_ACCEPT,
        source=_official("korporatsii"),
        notes="XML corporateItemDto. Без Accept: application/xml сервер отдаёт массив пустых JSON-объектов.",
    ),
    EndpointSpec(
        "stores",
        "/api/corporation/stores",
        "Склады",
        "xml",
        headers=XML_ACCEPT,
        source=_official("korporatsii"),
        notes="XML corporateItemDto, parentId — подразделение. Без Accept: application/xml — пустые объекты.",
    ),
    EndpointSpec(
        "products",
        "/api/v2/entities/products/list",
        "Номенклатура (товары)",
        "json",
        params={"includeDeleted": "false"},
        source=_official("elementy-nomenklatury"),
        size_bounded=False,
        notes="Нужно право B_EN (иначе 403). Ограничения размера нет; ответ усекается по max_response_bytes.",
    ),
    EndpointSpec(
        "product_groups",
        "/api/v2/entities/products/group/list",
        "Группы номенклатуры",
        "json",
        params={"includeDeleted": "false"},
        source=_official("elementy-nomenklatury"),
        notes="Только для названия группы товара (поле parent товара). Тот же вызов делал прототип (stock.py).",
    ),
    EndpointSpec(
        "measure_units",
        "/api/v2/entities/list",
        "Единицы измерения (rootType=MeasureUnit)",
        "json",
        params={"rootType": "MeasureUnit", "includeDeleted": "false"},
        source=_official("spravochniki"),
    ),
    EndpointSpec(
        "balances",
        "/api/v2/reports/balance/stores",
        "Остатки на складах",
        "json",
        required_params=("timestamp",),
        source=_official("otchety-vv2"),
        notes="С iiko 5.2. timestamp обязателен (yyyy-MM-ddTHH:mm:ss); store можно указать несколько раз. "
        "Нулевые остатки сервер не возвращает. Ответ: [{store, product, amount, sum}].",
    ),
    EndpointSpec(
        "olap_columns",
        "/api/v2/reports/olap/columns",
        "Поля OLAP-отчёта (reportType=TRANSACTIONS)",
        "json",
        required_params=("reportType",),
        source=_official("olap-otchety-v2"),
        notes="Только список полей; сам отчёт — POST /api/v2/reports/olap через olap.py.",
    ),
)

REGISTRY: dict[str, EndpointSpec] = {s.key: s for s in _SPECS}


def get_spec(key: str) -> EndpointSpec:
    try:
        return REGISTRY[key]
    except KeyError:
        raise IikoError(ErrorKind.API_NOT_SUPPORTED, f"Метод {key!r} не входит в реестр разрешённых") from None


def build_params(spec: EndpointSpec, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Merge default and caller params; refuse calls with missing required parameters."""
    params = {**spec.params, **(extra or {})}
    missing = [name for name in spec.required_params if params.get(name) in (None, "")]
    if missing:
        raise IikoError(
            ErrorKind.MISSING_PARAMETERS,
            f"Для {spec.path} не заданы обязательные параметры: {', '.join(missing)}",
            endpoint=spec.path,
        )
    return params
