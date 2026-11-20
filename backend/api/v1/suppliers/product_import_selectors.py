"""ТЗ §20: ANALYZE (parse) -> ENRICH (safe autofill) -> MATCH (duplicates) in
one pass, building SupplierProductImportItem rows. Auto action: no
candidates -> new; candidates found -> review."""

import hashlib
import logging
import re
import time
from collections import Counter
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Min, Q
from django.db.models.functions import Lower
from django.utils import timezone

from api.v1.measure import round_net_value
from api.v1.suppliers.normalization import (
    active_unit_codes_and_names,
    normalize_unit_code,
    unit_code_aliases_from_units,
)
from api.v1.suppliers.package_composition import parse_package_composition
from core.models import EntityStatus, Unit, UnitKind
from file_imports.extraction import extractor_name_for_file
from file_imports.models import ImportFileScope
from file_imports.quality import (
    ISSUE_NUMERIC_NAME,
    NUMERIC_NAME_MESSAGE,
    WARNING_SHEET_SKIPPED,
    is_numeric_name,
    price_share_warning,
)
from file_imports.services import (
    ImportFileUnavailable,
    import_file_as_upload,
    import_file_storage_enabled,
    store_import_file,
)
from file_imports.versions import is_canonical, parser_version_for
from suppliers.models import (
    ProductAvailability,
    Supplier,
    SupplierProduct,
    SupplierProductImportAction,
    SupplierProductImportItem,
    SupplierProductImportProcessingStatus,
    SupplierProductImportSession,
    SupplierProductImportSessionStatus,
)

from .product_import_hints import has_country_hint
from .product_import_inherit import drop_inherited_fields, inherit_target_fields
from .product_import_llm import (
    SupplierProductEnrichmentError,
    _chunk_size,
    _max_workers,
    enrich_supplier_product_import_rows,
)
from .product_import_matching import (
    find_hidden_sku_holders,
    find_supplier_product_import_candidates,
    find_supplier_products_by_sku,
    hidden_sku_holder_message,
    same_product_kind,
)
from .product_import_parsing import SupplierProductImportParseError, parse_supplier_product_import_file

_AVAILABILITY_LABELS = {
    "в наличии": ProductAvailability.IN_STOCK,
    "под заказ": ProductAvailability.ON_ORDER,
    "по заказу": ProductAvailability.ON_ORDER,
    "нет в наличии": ProductAvailability.OUT_OF_STOCK,
    "нет": ProductAvailability.OUT_OF_STOCK,
    "отсутствует": ProductAvailability.OUT_OF_STOCK,
    "закончился": ProductAvailability.OUT_OF_STOCK,
}


logger = logging.getLogger("autosnab.supplier_import")


def _availability_from_text(value):
    """Строка целиком (не подстрока, чтобы не ловить «нет» внутри названий) -> статус наличия или None."""
    if value is None:
        return None
    return _AVAILABILITY_LABELS.get(" ".join(str(value).lower().replace(".", " ").split()))


def _to_decimal(value):
    if value is None:
        return None
    try:
        text = re.sub(r"\s+", "", str(value)).replace(",", ".")
        text = re.sub(r"(?:₽|руб\.?|р\.|RUB)$", "", text, flags=re.IGNORECASE)
        parsed = Decimal(text)
        return parsed if parsed.is_finite() else None
    except InvalidOperation:
        return None


def _resolve_unit_label(label, unit_cache):
    if not label:
        return None
    key = label.strip().lower()
    if key in unit_cache:
        return unit_cache[key]
    unit = (
        Unit.objects.filter(status=EntityStatus.ACTIVE, code__iexact=key).first()
        or Unit.objects.filter(status=EntityStatus.ACTIVE, name__iexact=key).first()
    )
    if unit is None:
        aliases = unit_code_aliases_from_units(active_unit_codes_and_names())
        code = normalize_unit_code(label, aliases=aliases)
        if code:
            unit = Unit.objects.filter(status=EntityStatus.ACTIVE, code=code).first()
    unit_cache[key] = unit
    return unit


def _pack_fields_from_text(text, unit_cache, aliases_holder):
    """«8 шт. × 2 кг = 16 кг/кор.» -> поля состава строки сессии. Пустой dict, если текст пуст, не разобран или
    единица не из справочника (строку из-за этого не бракуем). Единица штуки — не тара, тара — только kind=package."""
    if not text:
        return {}
    if "aliases" not in aliases_holder:
        aliases_holder["aliases"] = unit_code_aliases_from_units(active_unit_codes_and_names())
    parsed = parse_package_composition(
        text, unit_aliases=aliases_holder["aliases"], normalize_unit_code=normalize_unit_code
    )
    if parsed is None:
        return {}
    item_unit = _resolve_unit_label(parsed["pack_item_unit_code"], unit_cache)
    if item_unit is None or item_unit.kind == UnitKind.PACKAGE:
        return {}
    pack_type = _resolve_unit_label(parsed["pack_type_code"], unit_cache) if parsed["pack_type_code"] else None
    if pack_type is not None and pack_type.kind != UnitKind.PACKAGE:
        pack_type = None
    return {
        "pack_items_count": parsed["pack_items_count"],
        "pack_item_value": parsed["pack_item_value"],
        "pack_item_unit": item_unit,
        "pack_type": pack_type,
    }


def _batch_size():
    # По умолчанию = ENRICHMENT_CHUNK_SIZE x ENRICHMENT_MAX_WORKERS: пачка занимает все потоки одной «волной», и
    # готовые строки появляются в GET сессии чаще. Явный SUPPLIER_PRODUCT_IMPORT_BATCH_SIZE главнее.
    explicit = getattr(settings, "SUPPLIER_PRODUCT_IMPORT_BATCH_SIZE", None)
    try:
        explicit = int(explicit) if explicit else 0
    except (TypeError, ValueError):
        explicit = 0
    if explicit > 0:
        return explicit
    return _chunk_size() * _max_workers()


def supplier_product_import_file_sha256(file):
    """sha256 содержимого загруженного файла; указатель возвращается в начало, чтобы парсер читал файл целиком."""
    digest = hashlib.sha256()
    file.seek(0)
    for chunk in iter(lambda: file.read(1024 * 1024), b""):
        digest.update(chunk)
    file.seek(0)
    return digest.hexdigest()


def find_reusable_supplier_product_import_session(*, supplier, content_sha256):
    """TASK-133: незавершённая (open) и не упавшая сессия этого поставщика с тем же файлом. Завершённую
    (committed) или упавшую (failed) не возвращаем: повторная загрузка создаст новую сессию, как раньше."""
    if not content_sha256:
        return None
    return (
        SupplierProductImportSession.objects.filter(
            supplier=supplier,
            content_sha256=content_sha256,
            status=SupplierProductImportSessionStatus.OPEN,
        )
        .exclude(processing_status=SupplierProductImportProcessingStatus.FAILED)
        .order_by("-created_at")
        .first()
    )


class SupplierProductImportQueueUnavailable(Exception):
    """Не удалось поставить обработку сессии в очередь (брокер недоступен)."""


# Откуда канонический парсер мог взять страну, если в файле нет колонки: слово в названии, заголовок раздела, префикс
# штрихкода, LLM-кандидат.
_INFERRED_COUNTRY_SOURCES = frozenset({"name", "section", "gs1_prefix", "llm"})


def _create_import_items(session, parsed_rows):
    unit_cache = {}
    aliases_holder = {}
    items = []
    for row in parsed_rows:
        if row.get("_parsed") and is_numeric_name(row.get("_raw_name", row["name"])):
            row["_parsed"].setdefault("parse_issues", []).append(
                {
                    "code": ISSUE_NUMERIC_NAME,
                    "field": "name",
                    "level": "warning",
                    "review_required": True,
                    "message": NUMERIC_NAME_MESSAGE,
                }
            )
        unit = _resolve_unit_label(row["unit_label"], unit_cache)
        net_unit = _resolve_unit_label(row["net_unit_label"], unit_cache)
        price = _to_decimal(row["price"])
        availability = _availability_from_text(row["availability_label"])
        if availability is None and price is None:
            # TASK-127: в колонке цены вместо числа бывает «под заказ»/«нет» -- не теряем эту информацию. Явная колонка
            # «наличие» главнее. «По запросу» и любой другой текст дают просто пустую цену, как и раньше.
            availability = _availability_from_text(row["price"])
        package_fields = _pack_fields_from_text(row.get("package_composition"), unit_cache, aliases_holder)
        field_sources = {}
        if row.get("_parsed"):
            fields = row["_parsed"]["fields"]

            def selected(key):
                f = fields.get(key, {})
                return f.get("value") if f.get("status") not in ("ambiguous", "invalid") else None

            count, value = selected("pack_items_count"), selected("pack_item_value")
            item_unit = _resolve_unit_label(selected("pack_item_unit_label"), unit_cache)
            pack_type = _resolve_unit_label(selected("pack_type_label"), unit_cache)
            if count and value and item_unit and item_unit.kind != UnitKind.PACKAGE:
                package_fields = {
                    "pack_items_count": count,
                    "pack_item_value": Decimal(value),
                    "pack_item_unit": item_unit,
                    "pack_type": pack_type if pack_type and pack_type.kind == UnitKind.PACKAGE else None,
                }
            country_source = fields.get("country_of_origin", {}).get("source")
            if row["country_of_origin"] and country_source in _INFERRED_COUNTRY_SOURCES:
                # Страна взята не из колонки файла: показываем откуда (колонка = отсутствие ключа, user ставится позже).
                field_sources["country_of_origin"] = country_source
        items.append(
            SupplierProductImportItem(
                session=session,
                row_number=row["row_number"],
                raw_name=row.get("_raw_name", row["name"]),
                parsed_snapshot=row.get("_parsed", {}),
                name=row["name"],
                country_of_origin=row["country_of_origin"],
                unit=unit,
                price=price,
                delivery_multiple=_to_decimal(row["delivery_multiple"]),
                net_value=round_net_value(_to_decimal(row["net_value"])),
                net_unit=net_unit,
                min_order_qty=_to_decimal(row["min_order_qty"]),
                availability=availability,
                supplier_sku=row["supplier_sku"],
                field_sources=field_sources,
                **package_fields,
            )
        )
    SupplierProductImportItem.objects.bulk_create(items)
    warnings = [{"code": WARNING_SHEET_SKIPPED, "message": text} for text in getattr(parsed_rows, "warnings", [])]
    price_warning = price_share_warning(len(items), sum(item.price is not None for item in items))
    if price_warning:
        warnings.append(price_warning)
    if warnings:
        session.warnings = warnings
        session.save(update_fields=["warnings", "updated_at"])
    if items and items[0].parsed_snapshot:
        session.parser_version = items[0].parsed_snapshot["parser_version"]
        session.save(update_fields=["parser_version", "updated_at"])


def _enqueue_session_task(session, task):
    try:
        task.delay(session_id=str(session.id))
    except Exception as error:
        # Брокер недоступен: обработка не началась, показывать пользователю нечего. Сессию не оставляем (раньше она
        # висела в списке как «Ошибка обработки» со всеми разобранными строками); повторная загрузка создаст новую.
        SupplierProductImportSession.objects.filter(pk=session.pk).delete()
        raise SupplierProductImportQueueUnavailable("Не удалось поставить обработку в очередь.") from error


# Форматы, для которых извлечение тяжёлое независимо от размера файла (спека §9.6): их разбирает воркер.
# CSV/XLSX тоже: разбор 2100-строчного прайса занимает ~10 с даже без LLM (блокирует gunicorn-воркер, фронт не видит
# прогресса «читаем файл»), а с LLM-кандидатами полей добавляются очередные вызовы (20-40 с на файл).
_ASYNC_EXTRACTORS = {"docling_pdf", "docx", "txt", "image_ocr", "csv", "xlsx"}


def _async_extraction_applies(file_name):
    if not (
        bool(getattr(settings, "SUPPLIER_IMPORT_ASYNC_EXTRACTION_ENABLED", False)) and import_file_storage_enabled()
    ):
        return False
    return extractor_name_for_file(file_name) in _ASYNC_EXTRACTORS


def analyze_supplier_product_import(*, supplier, file, source_file_name, content_sha256=None):
    session, task = _analyze_supplier_product_import(
        supplier=supplier, file=file, source_file_name=source_file_name, content_sha256=content_sha256
    )
    # Publish after the transaction commits, so the worker can see its session.
    if task is not None:
        _enqueue_session_task(session, task)
    return session


@transaction.atomic
def _analyze_supplier_product_import(*, supplier, file, source_file_name, content_sha256=None):
    """Разбор файла и создание строк сессии -- быстрая часть (секунды для табличных файлов). Дозаполнение LLM и
    сопоставление (медленная часть) для любого размера файла идут в Celery: сессия создаётся в статусе pending,
    фронт опрашивает GET сессии (pending -> processing -> ready | failed), готовые строки появляются по ходу.

    PDF/DOCX/DOC/TXT/изображения (при включённых SUPPLIER_IMPORT_ASYNC_EXTRACTION_ENABLED и сохранении файла)
    в запросе не разбираются: файл только сохраняется, извлечение делает воркер (extract_supplier_product_import_session)."""
    if getattr(file, "size", 0) > 10 * 1024 * 1024:
        raise SupplierProductImportParseError("Файл слишком большой. Максимум — 10 МБ.")
    if content_sha256 is None:
        content_sha256 = supplier_product_import_file_sha256(file)

    Supplier.objects.select_for_update().get(pk=supplier.pk)
    reusable = find_reusable_supplier_product_import_session(supplier=supplier, content_sha256=content_sha256)
    if reusable is not None:
        return reusable, None
    if _async_extraction_applies(getattr(file, "name", "") or source_file_name):
        # ImportFileUnavailable пробрасывается наверх: view отвечает 503, сессия не создаётся.
        import_file = store_import_file(
            uploaded_file=file, scope_type=ImportFileScope.SUPPLIER, scope_id=supplier.id, content_sha256=content_sha256
        )
        session = SupplierProductImportSession.objects.create(
            supplier=supplier,
            source_file_name=source_file_name,
            content_sha256=content_sha256,
            rows_total=0,
            parser_version=parser_version_for(getattr(settings, "CANONICAL_SUPPLIER_PARSER_ENABLED", False)),
            processing_status=SupplierProductImportProcessingStatus.PENDING,
            import_file=import_file,
        )
        from .tasks import extract_supplier_product_import_session_task

        return session, extract_supplier_product_import_session_task

    parse_started = time.monotonic()
    parsed_rows = parse_supplier_product_import_file(file)
    parse_s = time.monotonic() - parse_started

    import_file = None
    if import_file_storage_enabled():
        # ImportFileUnavailable пробрасывается наверх: view отвечает 503, сессия не создаётся.
        import_file = store_import_file(
            uploaded_file=file, scope_type=ImportFileScope.SUPPLIER, scope_id=supplier.id, content_sha256=content_sha256
        )

    session = SupplierProductImportSession.objects.create(
        supplier=supplier,
        source_file_name=source_file_name,
        content_sha256=content_sha256,
        rows_total=len(parsed_rows),
        processing_status=SupplierProductImportProcessingStatus.PENDING,
        import_file=import_file,
    )
    _create_import_items(session, parsed_rows)
    logger.info(
        "import.analyze.done session=%s file=%s rows=%s parse_s=%.2f total_s=%.2f",
        session.pk,
        extractor_name_for_file((source_file_name or "").lower()),
        len(parsed_rows),
        parse_s,
        time.monotonic() - parse_started,
    )

    from .tasks import process_supplier_product_import_session_task

    return session, process_supplier_product_import_session_task


def _fail_import_session(session_pk, message):
    logger.error("import.failed session=%s stage=processing reason=%s", session_pk, message[:1000])
    SupplierProductImportSession.objects.filter(pk=session_pk).update(
        processing_status=SupplierProductImportProcessingStatus.FAILED,
        error_message=message[:1000],
        updated_at=timezone.now(),
    )


def extract_supplier_product_import_session(*, session_id):
    """Тяжёлая часть для PDF/DOCX/DOC/TXT/image: файл читается из хранилища ВНЕ HTTP-запроса, строки создаются здесь,
    затем идёт обычная обработка (дозаполнение + сопоставление). Идемпотентна: если строки уже созданы (ретрай),
    извлечение не повторяется. Ошибка разбора -> failed с читаемым сообщением."""
    session = SupplierProductImportSession.objects.select_related("supplier", "import_file").get(pk=session_id)
    if session.processing_status not in (
        SupplierProductImportProcessingStatus.PENDING,
        SupplierProductImportProcessingStatus.PROCESSING,
    ):
        return session
    SupplierProductImportSession.objects.filter(pk=session.pk).update(
        processing_status=SupplierProductImportProcessingStatus.PROCESSING, updated_at=timezone.now()
    )
    extract_started = time.monotonic()
    if not session.items.exists():
        try:
            if session.import_file is None:
                raise ImportFileUnavailable("Исходный файл не сохранён.")
            parsed_rows = parse_supplier_product_import_file(
                import_file_as_upload(session.import_file),
                canonical_enabled=is_canonical(session.parser_version),
            )
        except SupplierProductImportParseError as error:
            _fail_import_session(session.pk, str(error))
            session.refresh_from_db()
            return session
        except ImportFileUnavailable:
            _fail_import_session(session.pk, "Исходный файл недоступен: срок хранения истёк. Загрузите файл ещё раз.")
            session.refresh_from_db()
            return session
        _create_import_items(session, parsed_rows)
        SupplierProductImportSession.objects.filter(pk=session.pk).update(
            rows_total=len(parsed_rows), updated_at=timezone.now()
        )
        logger.info(
            "import.extract.done session=%s file=%s rows=%s extract_s=%.1f",
            session.pk,
            extractor_name_for_file((session.source_file_name or "").lower()),
            len(parsed_rows),
            time.monotonic() - extract_started,
        )
    return process_supplier_product_import_session(session_id=session.id)


def process_supplier_product_import_session(*, session_id):
    """Медленная часть импорта: ENRICH + MATCH пачками по SUPPLIER_PRODUCT_IMPORT_BATCH_SIZE строк, между пачками
    обновляется rows_done (для percent и для выдачи готовых строк в GET сессии). pending -> processing -> ready;
    при любой ошибке -- failed с читаемым error_message."""
    session = SupplierProductImportSession.objects.select_related("supplier").get(pk=session_id)
    if session.processing_status not in (
        SupplierProductImportProcessingStatus.PENDING,
        SupplierProductImportProcessingStatus.PROCESSING,
    ):
        return session
    SupplierProductImportSession.objects.filter(pk=session.pk).update(
        processing_status=SupplierProductImportProcessingStatus.PROCESSING, updated_at=timezone.now()
    )
    started = time.monotonic()
    totals = {"enrich_s": 0.0, "match_s": 0.0, "save_s": 0.0}
    logger.info(
        "import.process.start session=%s rows=%s queue_wait_s=%.1f",
        session.pk,
        session.rows_total,
        (timezone.now() - session.created_at).total_seconds(),
    )
    try:
        items = list(session.items.order_by("row_number"))
        batch_size = _batch_size()
        for start in range(0, len(items), batch_size):
            timings = _enrich_and_match_items(supplier=session.supplier, items=items[start : start + batch_size]) or {}
            for key, value in timings.items():
                totals[key] = totals.get(key, 0.0) + value
            SupplierProductImportSession.objects.filter(pk=session.pk).update(
                rows_done=min(start + batch_size, len(items)), updated_at=timezone.now()
            )
            logger.info(
                "import.process.batch session=%s rows=%s-%s of=%s enrich_s=%.2f match_s=%.2f save_s=%.2f",
                session.pk,
                start + 1,
                min(start + batch_size, len(items)),
                len(items),
                timings.get("enrich_s", 0.0),
                timings.get("match_s", 0.0),
                timings.get("save_s", 0.0),
            )
    except Exception as error:
        SupplierProductImportSession.objects.filter(
            pk=session.pk, processing_status=SupplierProductImportProcessingStatus.PROCESSING
        ).update(
            processing_status=SupplierProductImportProcessingStatus.FAILED,
            error_message=str(error)[:1000] or error.__class__.__name__,
            updated_at=timezone.now(),
        )
        raise
    SupplierProductImportSession.objects.filter(
        pk=session.pk, processing_status=SupplierProductImportProcessingStatus.PROCESSING
    ).update(
        processing_status=SupplierProductImportProcessingStatus.READY,
        rows_done=session.rows_total,
        updated_at=timezone.now(),
    )
    session.refresh_from_db()
    logger.info(
        "import.process.done session=%s rows=%s total_s=%.1f enrich_s=%.1f match_s=%.1f save_s=%.1f",
        session.pk,
        session.rows_total,
        time.monotonic() - started,
        totals["enrich_s"],
        totals["match_s"],
        totals["save_s"],
    )
    return session


def _enrich_and_match_items(*, supplier, items):
    # ENRICH -- только то, что нельзя получить детерминированно: единица (нет в файле) и страна (только если в
    # названии есть явное указание). Нетто теперь берёт парсер из названия (product_import_net).
    enrichment_input = []
    for item in items:
        if item.parsed_snapshot:
            continue  # Canonical LLM candidates use the shared resolver, never mutate the DTO here.
        needs_unit = item.unit_id is None
        needs_country = not item.country_of_origin and has_country_hint(item.name)
        if needs_unit or needs_country:
            enrichment_input.append(
                {
                    "row_number": item.row_number,
                    "name": item.name,
                    "needs_unit": needs_unit,
                    "needs_country": needs_country,
                }
            )
    stage_started = time.monotonic()
    try:
        enrichment = enrich_supplier_product_import_rows(rows=enrichment_input)
    except SupplierProductEnrichmentError as error:
        logger.warning("import.enrich_failed rows=%s error=%s", len(enrichment_input), error)
        enrichment = {}

    by_row = {item.row_number: item for item in items}
    for row_number, fields in enrichment.items():
        item = by_row.get(row_number)
        if not item:
            continue
        field_sources = dict(item.field_sources)
        if "unit_id" in fields and item.unit_id is None:
            item.unit_id = fields["unit_id"]
            field_sources["unit"] = "llm"
        if "country_of_origin" in fields and not item.country_of_origin:
            item.country_of_origin = fields["country_of_origin"]
            field_sources["country_of_origin"] = "llm"
        item.field_sources = field_sources

    enrich_s = time.monotonic() - stage_started

    # MATCH -- one batch query for every row.
    stage_started = time.monotonic()
    names = [item.name for item in items]
    candidates_by_position = find_supplier_product_import_candidates(supplier_id=supplier.id, names=names)
    for position, item in enumerate(items):
        candidates = candidates_by_position.get(position, [])
        item.match_candidates = [
            {
                "id": str(c["id"]),
                "name": c["name"],
                "unit_id": str(c["unit_id"]) if c["unit_id"] else None,
                "unit_name": c["unit_name"],
                "score": c["score"],
            }
            for c in candidates
        ]
        item.action = SupplierProductImportAction.REVIEW if candidates else SupplierProductImportAction.NEW

    match_s = time.monotonic() - stage_started

    # MATCH по артикулу (TASK-133) -- точнее названия: известный sku сразу даёт replace на этот товар.
    stage_started = time.monotonic()
    _apply_sku_matches(supplier=supplier, items=items)
    _flag_duplicate_names(items=items)
    _apply_exact_name_matches(items=items)

    if items:
        from file_imports.canonical import effective_issues

        for item in items:
            if effective_issues(item.parsed_snapshot) or is_numeric_name(item.raw_name):
                item.action = SupplierProductImportAction.REVIEW
                item.target_supplier_product = None
                item.target_expected_updated_at = None
                drop_inherited_fields(item)
        SupplierProductImportItem.objects.bulk_update(
            items,
            [
                "unit",
                "net_value",
                "net_unit",
                "country_of_origin",
                "min_order_qty",
                "delivery_multiple",
                "availability",
                "field_sources",
                "match_candidates",
                "validation_errors",
                "action",
                "target_supplier_product",
                "target_expected_updated_at",
            ],
        )
    return {"enrich_s": enrich_s, "match_s": match_s, "save_s": time.monotonic() - stage_started}


def _apply_exact_name_matches(*, items):
    """Строка, название которой один в один совпало с названием ровно одного товара поставщика -> action=replace
    на этот товар, как при совпадении артикула. Повторная загрузка почти того же прайса не должна отправлять на
    проверку каждую строку: «похожих» тут нет, товар тот же. Остаются на проверке строки без точного совпадения,
    с несколькими точными, повторяющиеся в файле (их уже пометил _flag_duplicate_names) и те, чей товар уже
    назначен другой строке файла (иначе несколько replace на один target и конфликт при commit)."""
    taken = Counter(str(item.target_supplier_product_id) for item in items if item.target_supplier_product_id)
    exact_by_item = {}
    for item in items:
        if (
            item.action != SupplierProductImportAction.REVIEW
            or item.target_supplier_product_id
            or item.validation_errors
        ):
            continue
        exact = [str(c["id"]) for c in item.match_candidates if c["score"] >= 1.0]
        if len(exact) == 1:
            exact_by_item[item] = exact[0]
    taken.update(exact_by_item.values())
    products = {
        str(product.id): product for product in SupplierProduct.objects.filter(id__in=list(exact_by_item.values()))
    }
    for item, product_id in exact_by_item.items():
        product = products.get(product_id)
        if product is None or taken[product_id] > 1:
            continue
        item.action = SupplierProductImportAction.REPLACE
        item.target_supplier_product = product
        item.target_expected_updated_at = product.updated_at
        inherit_target_fields(item, product)


def _apply_sku_matches(*, supplier, items):
    """Строка с артикулом, который уже есть у товара этого поставщика -> action=replace, target проставлен,
    кандидаты по названию заменяются самим target (score 1.0 -- как точное совпадение). Если один и тот же sku
    встречается в файле несколько раз, по артикулу совпадает только первая строка (иначе несколько replace на
    один target и конфликт при commit); остальные остаются на поиске по названию.

    Артикул товара другого вида -- не автозамена: поставщик мог отдать старый код другому товару, а replace
    переписал бы карточку, на которую уже ссылаются матрицы, корзины и заказы (аудит идентификаторов, F2). Такая
    строка уходит на проверку с товаром по артикулу первым кандидатом. Вид товара -- первое слово названия, как
    в поиске по названию: переименования («Сыр Хохланд» -> «Сыр Hochland плавленый») проходят как раньше.

    Артикул архивного или неактивного товара матчинг не предлагает, но и новую карточку с ним не сохранить
    (F6): такая строка уходит на проверку с объяснением, что делать."""
    skus = [(item.supplier_sku or "").strip() for item in items]
    products_by_sku = find_supplier_products_by_sku(supplier_id=supplier.id, skus=skus)
    hidden_by_sku = find_hidden_sku_holders(supplier_id=supplier.id, skus=skus)
    first_rows = (
        dict(
            SupplierProductImportItem.objects.filter(
                session_id=items[0].session_id,
                supplier_sku__in=[item.supplier_sku for item in items if item.supplier_sku],
            )
            .values("supplier_sku")
            .annotate(first_row=Min("row_number"))
            .values_list("supplier_sku", "first_row")
        )
        if items
        else {}
    )
    used_skus = set()
    for item in items:
        sku = (item.supplier_sku or "").strip()
        product = products_by_sku.get(sku)
        if sku in used_skus or item.row_number != first_rows.get(sku, item.row_number):
            item.action = SupplierProductImportAction.REVIEW
            item.target_supplier_product = None
            item.target_expected_updated_at = None
            item.validation_errors = ["Артикул повторяется в другой строке файла."]
            continue
        if product is None:
            hidden = hidden_by_sku.get(sku)
            if hidden is not None:
                item.action = SupplierProductImportAction.REVIEW
                item.target_supplier_product = None
                item.target_expected_updated_at = None
                item.validation_errors = [hidden_sku_holder_message(sku, hidden)]
            continue
        used_skus.add(sku)
        sku_candidate = {
            "id": str(product.id),
            "name": product.supplier_name,
            "unit_id": str(product.unit_id) if product.unit_id else None,
            "unit_name": product.unit.name if product.unit_id else None,
            "score": 1.0,
        }
        if not same_product_kind(item.name, product.supplier_name):
            item.action = SupplierProductImportAction.REVIEW
            item.target_supplier_product = None
            item.target_expected_updated_at = None
            item.match_candidates = [sku_candidate, *item.match_candidates]
            item.validation_errors = [
                f"Артикул {sku} уже у товара «{product.supplier_name}» -- похоже, это другой товар. "
                "Если это тот же товар, выберите замену; если другой -- исправьте артикул."
            ]
            continue
        item.action = SupplierProductImportAction.REPLACE
        item.target_supplier_product = product
        item.target_expected_updated_at = product.updated_at
        inherit_target_fields(item, product)
        item.match_candidates = [sku_candidate]


def _flag_duplicate_names(*, items):
    """Вторая и далее строка с тем же названием в сессии -> review (UNIQUE supplier+name не даст создать обе)."""
    if not items:
        return
    first_rows = dict(
        SupplierProductImportItem.objects.filter(session_id=items[0].session_id)
        .annotate(key=Lower("name"))
        .values("key")
        .annotate(first=Min("row_number"))
        .values_list("key", "first")
    )
    for item in items:
        first_row = first_rows.get((item.name or "").lower(), item.row_number)
        if item.row_number != first_row:
            item.action = SupplierProductImportAction.REVIEW
            item.target_supplier_product = None
            item.target_expected_updated_at = None
            item.validation_errors = [f"Название повторяется в другой строке файла (строка {first_row})."]


def _processed_items_queryset(session):
    """Строки сессии, которые уже окончательно обработаны (дозаполнены и сопоставлены).

    ready -- все. pending/processing -- первые rows_done строк по порядку (обработка идёт пачками в порядке
    row_number, и rows_done обновляется только после записи пачки). failed -- ничего."""
    queryset = session.items.select_related(
        "unit", "net_unit", "pack_item_unit", "pack_type", "target_supplier_product"
    ).order_by("row_number")
    status = session.processing_status
    if status == SupplierProductImportProcessingStatus.READY:
        return queryset
    if status in (SupplierProductImportProcessingStatus.PENDING, SupplierProductImportProcessingStatus.PROCESSING):
        if session.rows_done <= 0:
            return queryset.none()
        last_row_number = next(
            iter(queryset.values_list("row_number", flat=True)[session.rows_done - 1 : session.rows_done]), None
        )
        return queryset.none() if last_row_number is None else queryset.filter(row_number__lte=last_row_number)
    return queryset.none()


def _items_page_limit():
    """Размер страницы строк по умолчанию и максимум (SUPPLIER_PRODUCT_IMPORT_ITEMS_PAGE_SIZE / _MAX)."""
    maximum = max(1, int(getattr(settings, "SUPPLIER_PRODUCT_IMPORT_ITEMS_MAX_PAGE_SIZE", 1000) or 1000))
    default = max(1, int(getattr(settings, "SUPPLIER_PRODUCT_IMPORT_ITEMS_PAGE_SIZE", 500) or 500))
    return min(default, maximum), maximum


def _price_changed_filter():
    """Замена, у которой цена строки отличается от текущей цены заменяемого товара."""
    return (
        Q(action=SupplierProductImportAction.REPLACE, price__isnull=False, target_supplier_product__isnull=False)
        & Q(target_supplier_product__current_price__isnull=False)
        & ~Q(price=F("target_supplier_product__current_price"))
    )


def supplier_product_import_session_payload(
    session, *, include_items=True, after_row_number=None, limit=None, action=None, price_changed=False
):
    """Состояние сессии для GET. items: готовые строки (см. _processed_items_queryset); include_items=False --
    только статус и счётчики (лёгкий опрос), after_row_number=N -- только строки с row_number > N (курсор),
    limit -- сколько строк вернуть за раз (по умолчанию 500, максимум 1000), action -- только строки с этим
    действием. has_more/next_after_row_number говорят, есть ли продолжение и откуда его брать.
    counts всегда считаются по всем готовым строкам (без action и limit), а не по возвращённой странице."""
    processed = _processed_items_queryset(session)
    aggregated = processed.order_by().aggregate(
        ready=Count(
            "id",
            filter=(
                Q(action="new")
                | Q(action="replace", target_supplier_product__status__in=[EntityStatus.ACTIVE, EntityStatus.DRAFT])
            )
            & Q(
                validation_errors=[],
                unit__status=EntityStatus.ACTIVE,
                net_unit__status=EntityStatus.ACTIVE,
                price__gte=0,
                delivery_multiple__gt=0,
                net_value__gt=0,
                min_order_qty__gt=0,
                availability__in=ProductAvailability.values,
                country_of_origin__isnull=False,
            )
            & ~Q(country_of_origin="")
            & ~Q(name=""),
        ),
        all=Count("id"),
        review=Count("id", filter=Q(action=SupplierProductImportAction.REVIEW)),
        new=Count("id", filter=Q(action=SupplierProductImportAction.NEW)),
        replace=Count("id", filter=Q(action=SupplierProductImportAction.REPLACE)),
        skip=Count("id", filter=Q(action=SupplierProductImportAction.SKIP)),
        price_changed=Count("id", filter=_price_changed_filter()),
    )
    items = []
    has_more = False
    next_after_row_number = None
    if include_items:
        default_limit, max_limit = _items_page_limit()
        page_size = min(limit or default_limit, max_limit)
        if action:
            processed = processed.filter(action=action)
        if price_changed:
            processed = processed.filter(_price_changed_filter())
        if after_row_number is not None:
            processed = processed.filter(row_number__gt=after_row_number)
        items = list(processed[: page_size + 1])
        has_more = len(items) > page_size
        items = items[:page_size]
        if has_more:
            next_after_row_number = items[-1].row_number
    return {
        "session": session,
        "counts": aggregated,
        "items": items,
        "has_more": has_more,
        "next_after_row_number": next_after_row_number,
    }


def list_supplier_product_import_sessions(*, supplier, status=None):
    """Сессии импорта этого поставщика, новые первыми. status: open (по умолчанию -- незавершённые, включая
    упавшие), committed или None -- все."""
    queryset = SupplierProductImportSession.objects.filter(supplier=supplier)
    if status:
        queryset = queryset.filter(status=status)
    return queryset.order_by("-created_at")
