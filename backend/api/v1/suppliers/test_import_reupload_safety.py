"""Re-uploading a price list: a replace row keeps the card's own fields, shows the price change and writes nothing
when nothing changed; sessions can be removed; a dead queue leaves no session behind."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from api.v1.suppliers.product_import_services import (
    _item_errors,
    _item_write_data,
    apply_supplier_product_import_defaults,
    commit_supplier_product_import_session,
    patch_supplier_product_import_item,
    set_supplier_product_import_item_action,
)
from api.v1.suppliers.serializers import SupplierProductImportItemSerializer
from api.v1.suppliers.services import create_supplier_product, issue_supplier_cabinet_tokens
from api.v1.suppliers.tasks import purge_committed_product_import_sessions_task
from core.models import AuditLog, AuditLogAction, Unit, UnitKind
from suppliers.models import (
    ProductAvailability,
    Supplier,
    SupplierProduct,
    SupplierProductImportAction,
    SupplierProductImportCommitStatus,
    SupplierProductImportItem,
    SupplierProductImportProcessingStatus,
    SupplierProductImportSession,
)


def _supplier(name="Reupload LLC"):
    return Supplier.objects.create(legal_name=name, display_name=name)


def _unit():
    unit, _ = Unit.objects.get_or_create(code="ru-pcs", defaults={"name": "ru-pcs", "kind": UnitKind.PIECE})
    return unit


def _card(supplier, **extra):
    """An existing card created the way an import creates it."""
    unit = _unit()
    values = {
        "supplier_raw_name": "Сахар",
        "supplier_normalized_name": "Сахар",
        "unit": unit,
        "country_of_origin": "Италия",
        "net_value": Decimal("1"),
        "net_unit": unit,
        "min_order_qty": Decimal("3"),
        "delivery_multiple": Decimal("2"),
        "availability": ProductAvailability.ON_ORDER,
        "price": Decimal("60"),
    }
    values.update(extra)
    return create_supplier_product(supplier_id=supplier.id, data=values, supplier=supplier)


def _row(session, number, name="Сахар", **extra):
    unit = _unit()
    values = {
        "session": session,
        "row_number": number,
        "raw_name": name,
        "name": name,
        "unit": unit,
        "price": Decimal("60"),
        "net_value": Decimal("1"),
        "net_unit": unit,
        "action": SupplierProductImportAction.REVIEW,
    }
    values.update(extra)
    return SupplierProductImportItem.objects.create(**values)


class ReplaceRowKeepsTheCardsFieldsTests(TestCase):
    def setUp(self):
        self.supplier = _supplier()
        self.card = _card(self.supplier)
        self.session = SupplierProductImportSession.objects.create(supplier=self.supplier)

    def _replace(self, item):
        return set_supplier_product_import_item_action(
            item=item, action=SupplierProductImportAction.REPLACE, target_supplier_product_id=self.card.id
        )

    def test_replace_takes_the_missing_fields_from_the_card_and_is_ready(self):
        item = self._replace(_row(self.session, 1))

        self.assertEqual(item.country_of_origin, "Италия")
        self.assertEqual((item.min_order_qty, item.delivery_multiple), (Decimal("3"), Decimal("2")))
        self.assertEqual(item.availability, ProductAvailability.ON_ORDER)
        for field in ("country_of_origin", "min_order_qty", "delivery_multiple", "availability"):
            self.assertEqual(item.field_sources[field], "target", field)
        self.assertEqual(_item_errors(item, validate_units=False), [])

    def test_a_value_from_the_file_is_not_replaced_by_the_card(self):
        item = self._replace(_row(self.session, 1, country_of_origin="Франция"))

        self.assertEqual(item.country_of_origin, "Франция")
        self.assertNotIn("country_of_origin", item.field_sources)

    def test_switching_away_from_replace_drops_the_copied_fields(self):
        item = self._replace(_row(self.session, 1))

        item = set_supplier_product_import_item_action(item=item, action=SupplierProductImportAction.NEW)

        self.assertIsNone(item.country_of_origin)
        self.assertIsNone(item.min_order_qty)
        self.assertNotIn("country_of_origin", item.field_sources)

    def test_renaming_the_row_drops_the_copied_fields_but_keeps_a_value_set_in_the_same_edit(self):
        item = self._replace(_row(self.session, 1))

        item = patch_supplier_product_import_item(item=item, data={"name": "Сахар другой", "country_of_origin": "Чили"})

        self.assertEqual(item.action, SupplierProductImportAction.NEW)  # no similar card for the new name
        self.assertEqual(item.country_of_origin, "Чили")
        self.assertEqual(item.field_sources["country_of_origin"], "user")
        self.assertIsNone(item.min_order_qty)

    def test_fill_for_all_does_not_touch_a_replace_row_and_the_commit_keeps_the_cards_country(self):
        item = self._replace(_row(self.session, 1, price=Decimal("65")))

        apply_supplier_product_import_defaults(
            session=self.session, values={"country_of_origin": "Россия", "min_order_qty": Decimal("1")}
        )
        result = commit_supplier_product_import_session(session=self.session)

        self.assertEqual(result["imported_count"], 1)
        self.card.refresh_from_db()
        self.assertEqual(self.card.country_of_origin, "Италия")
        self.assertEqual(self.card.min_order_qty, Decimal("3"))
        self.assertEqual(self.card.current_price, Decimal("65"))
        self.assertNotIn("country_of_origin", _item_write_data(item))

    def test_a_user_edit_of_an_inherited_field_is_written(self):
        item = self._replace(_row(self.session, 1))
        item = patch_supplier_product_import_item(item=item, data={"country_of_origin": "Чили"})

        commit_supplier_product_import_session(session=self.session)

        self.card.refresh_from_db()
        self.assertEqual(self.card.country_of_origin, "Чили")
        self.assertEqual(item.field_sources["country_of_origin"], "user")


class AdvisoryCountryDoesNotOverwriteTheCardTests(TestCase):
    """A country only guessed from a section heading or a GS1 prefix is a hint, not data: a replace row takes the card's
    country instead. A country written in the row (`name`) or in a country column is explicit and still wins."""

    def setUp(self):
        self.supplier = _supplier()
        self.card = _card(self.supplier, country_of_origin="Россия")
        self.session = SupplierProductImportSession.objects.create(supplier=self.supplier)

    def _replace(self, item):
        return set_supplier_product_import_item_action(
            item=item, action=SupplierProductImportAction.REPLACE, target_supplier_product_id=self.card.id
        )

    def test_manual_replace_takes_the_cards_country_over_a_guess(self):
        for number, source in enumerate(("gs1_prefix", "section"), 1):
            with self.subTest(source=source):
                row = _row(
                    self.session,
                    number,
                    name=f"Сахар {number}",
                    country_of_origin="Казахстан",
                    field_sources={"country_of_origin": source},
                )
                item = self._replace(row)

                self.assertEqual(item.country_of_origin, "Россия")
                self.assertEqual(item.field_sources["country_of_origin"], "target")
                self.assertNotIn("country_of_origin", _item_write_data(item))

    def test_manual_replace_keeps_a_country_written_in_the_row(self):
        row = _row(self.session, 1, country_of_origin="Турция", field_sources={"country_of_origin": "name"})

        item = self._replace(row)

        self.assertEqual(item.country_of_origin, "Турция")
        self.assertEqual(item.field_sources["country_of_origin"], "name")
        self.assertEqual(_item_write_data(item)["country_of_origin"], "Турция")

    def test_auto_sku_replace_takes_the_cards_country_over_a_guess(self):
        from api.v1.suppliers.product_import_selectors import _apply_sku_matches

        SupplierProduct.objects.filter(pk=self.card.pk).update(supplier_sku="SKU-1")
        row = _row(
            self.session,
            1,
            name="Сахар песок",
            supplier_sku="SKU-1",
            country_of_origin="Казахстан",
            field_sources={"country_of_origin": "gs1_prefix"},
        )

        _apply_sku_matches(supplier=self.supplier, items=[row])

        self.assertEqual(row.action, SupplierProductImportAction.REPLACE)
        self.assertEqual(row.country_of_origin, "Россия")
        self.assertEqual(row.field_sources["country_of_origin"], "target")

    def test_auto_name_replace_takes_the_cards_country_over_a_guess(self):
        from api.v1.suppliers.product_import_selectors import _apply_exact_name_matches

        row = _row(
            self.session,
            1,
            country_of_origin="Италия",
            field_sources={"country_of_origin": "section"},
            match_candidates=[
                {"id": str(self.card.id), "name": "Сахар", "unit_id": None, "unit_name": None, "score": 1.0}
            ],
        )

        _apply_exact_name_matches(items=[row])

        self.assertEqual(row.action, SupplierProductImportAction.REPLACE)
        self.assertEqual(row.country_of_origin, "Россия")
        self.assertEqual(row.field_sources["country_of_origin"], "target")


@override_settings(
    CANONICAL_SUPPLIER_PARSER_ENABLED=True,
    CANONICAL_IMPORT_LLM_ENABLED=False,
    CANONICAL_IMPORT_COUNTRY_HINTS_ENABLED=True,
)
class ReuploadWithAGuessedCountryTests(TestCase):
    """The reviewer's case: the card says «Россия»; the file has a valid 487… (Kazakhstan) barcode and no country column.
    The re-uploaded row must keep the card's country, be ready and not write the guess back."""

    NAME = "Лапша Кимчи Роллтон"

    def setUp(self):
        self.supplier = _supplier()
        self.card = _card(
            self.supplier,
            supplier_raw_name=self.NAME,
            supplier_normalized_name=self.NAME,
            country_of_origin="Россия",
        )
        SupplierProduct.objects.filter(pk=self.card.pk).update(supplier_sku="R-1")

    def _import(self, text):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file
        from api.v1.suppliers.product_import_selectors import _create_import_items, _enrich_and_match_items

        session = SupplierProductImportSession.objects.create(supplier=self.supplier)
        rows = parse_supplier_product_import_file(SimpleUploadedFile("price.csv", text.encode("utf-8")))
        _create_import_items(session, rows)
        items = list(session.items.order_by("row_number"))
        _enrich_and_match_items(supplier=self.supplier, items=items)
        (item,) = session.items.all()
        return item

    def test_auto_name_replace_keeps_the_cards_country(self):
        item = self._import(f"Название;Штрихкод;Цена\n{self.NAME};4870123456789;60\n")

        self.assertEqual(item.action, SupplierProductImportAction.REPLACE)
        self.assertEqual(item.target_supplier_product_id, self.card.id)
        self.assertEqual(item.country_of_origin, "Россия")
        self.assertEqual(item.field_sources["country_of_origin"], "target")
        self.assertNotIn("country_of_origin", _item_write_data(item))

    def test_auto_sku_replace_keeps_the_cards_country(self):
        item = self._import(f"Артикул;Название;Штрихкод;Цена\nR-1;{self.NAME} new;4870123456789;60\n")

        self.assertEqual(item.action, SupplierProductImportAction.REPLACE)
        self.assertEqual(item.country_of_origin, "Россия")
        self.assertEqual(item.field_sources["country_of_origin"], "target")

    def test_a_country_column_still_wins_over_the_card(self):
        item = self._import(f"Название;Страна;Штрихкод;Цена\n{self.NAME};Казахстан;4870123456789;60\n")

        self.assertEqual(item.action, SupplierProductImportAction.REPLACE)
        self.assertEqual(item.country_of_origin, "Казахстан")
        self.assertNotIn("country_of_origin", item.field_sources)
        self.assertEqual(_item_write_data(item)["country_of_origin"], "Казахстан")


class ReplaceWithoutChangesTests(TestCase):
    def test_same_values_leave_the_card_and_its_history_alone(self):
        supplier = _supplier()
        card = _card(supplier)
        before = SupplierProduct.objects.get(pk=card.pk)
        audits_before = AuditLog.objects.filter(action=AuditLogAction.REPLACED).count()
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        item = _row(session, 1)
        item = set_supplier_product_import_item_action(
            item=item, action=SupplierProductImportAction.REPLACE, target_supplier_product_id=card.id
        )

        result = commit_supplier_product_import_session(session=session)

        self.assertEqual(result["imported_count"], 1)
        card.refresh_from_db()
        self.assertEqual(card.updated_at, before.updated_at)
        self.assertEqual(AuditLog.objects.filter(action=AuditLogAction.REPLACED).count(), audits_before)

    def test_a_changed_price_is_written_and_logged(self):
        supplier = _supplier()
        card = _card(supplier)
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        item = _row(session, 1, price=Decimal("65"))
        set_supplier_product_import_item_action(
            item=item, action=SupplierProductImportAction.REPLACE, target_supplier_product_id=card.id
        )

        commit_supplier_product_import_session(session=session)

        card.refresh_from_db()
        self.assertEqual((card.current_price, card.previous_price), (Decimal("65"), Decimal("60")))
        self.assertEqual(AuditLog.objects.filter(action=AuditLogAction.REPLACED).count(), 1)


class PriceChangeInTheRowTests(TestCase):
    def _data(self, item):
        return SupplierProductImportItemSerializer(item, context={"exclude_field_meta": True}).data

    def test_up_down_same_and_no_target(self):
        supplier = _supplier()
        card = _card(supplier)
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        cases = [
            (Decimal("65"), "up", Decimal("8.3")),
            (Decimal("45"), "down", Decimal("-25.0")),
            (Decimal("60"), "same", Decimal("0.0")),
        ]
        for number, (price, change, percent) in enumerate(cases, 1):
            item = _row(session, number, name=f"row {number}", price=price)
            item = set_supplier_product_import_item_action(
                item=item, action=SupplierProductImportAction.REPLACE, target_supplier_product_id=card.id
            )
            data = self._data(item)
            self.assertEqual(data["price_change"], change)
            self.assertEqual(Decimal(str(data["price_change_percent"])), percent)
            self.assertEqual(Decimal(str(data["target_current_price"])), Decimal("60"))
        plain = self._data(_row(session, 9, name="no target"))
        self.assertEqual(
            (plain["price_change"], plain["price_change_percent"], plain["target_current_price"]), (None, None, None)
        )


class SessionCleanupApiTests(APITestCase):
    def setUp(self):
        self.supplier = _supplier("Cleanup LLC")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_supplier_cabinet_tokens(self.supplier)['access']}")

    def _session(self, **extra):
        values = {"supplier": self.supplier, "processing_status": SupplierProductImportProcessingStatus.READY}
        values.update(extra)
        session = SupplierProductImportSession.objects.create(**values)
        _row(session, 1)
        return session

    def _url(self, session):
        return f"/api/v1/suppliers/products/import/{session.id}"

    def test_delete_removes_the_session_and_its_rows(self):
        session = self._session()

        response = self.client.delete(self._url(session))

        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(SupplierProductImportSession.objects.filter(pk=session.pk).exists())
        self.assertFalse(SupplierProductImportItem.objects.filter(session_id=session.pk).exists())

    def test_a_failed_session_can_be_deleted(self):
        session = self._session(processing_status=SupplierProductImportProcessingStatus.FAILED)
        self.assertEqual(self.client.delete(self._url(session)).status_code, 204)

    def test_delete_is_refused_while_processing_or_committing(self):
        processing = self._session(processing_status=SupplierProductImportProcessingStatus.PROCESSING)
        committing = self._session(commit_status=SupplierProductImportCommitStatus.RUNNING)

        first = self.client.delete(self._url(processing))
        second = self.client.delete(self._url(committing))

        self.assertEqual((first.status_code, first.json()), (409, {"detail": "Импорт ещё обрабатывается."}))
        self.assertEqual((second.status_code, second.json()), (409, {"detail": "Идёт применение импорта."}))
        self.assertTrue(SupplierProductImportSession.objects.filter(pk=processing.pk).exists())

    def test_other_suppliers_session_is_not_found(self):
        foreign = SupplierProductImportSession.objects.create(supplier=_supplier("Other LLC"))

        self.assertEqual(self.client.delete(self._url(foreign)).status_code, 404)
        self.assertTrue(SupplierProductImportSession.objects.filter(pk=foreign.pk).exists())

    def test_delete_without_a_token_is_401(self):
        session = self._session()
        self.client.credentials()
        self.assertEqual(self.client.delete(self._url(session)).status_code, 401)

    @override_settings(SUPPLIER_PRODUCT_IMPORT_FAILED_TTL_HOURS=24, SUPPLIER_PRODUCT_IMPORT_STUCK_TTL_HOURS=6)
    def test_purge_removes_sessions_stuck_in_processing_but_not_recent_ones(self):
        stuck = self._session(processing_status=SupplierProductImportProcessingStatus.PENDING)
        busy = self._session(processing_status=SupplierProductImportProcessingStatus.PROCESSING)
        SupplierProductImportSession.objects.filter(pk=stuck.pk).update(updated_at=timezone.now() - timedelta(hours=7))

        purge_committed_product_import_sessions_task()

        self.assertFalse(SupplierProductImportSession.objects.filter(pk=stuck.pk).exists())
        self.assertTrue(SupplierProductImportSession.objects.filter(pk=busy.pk).exists())

    @override_settings(SUPPLIER_PRODUCT_IMPORT_FAILED_TTL_HOURS=24)
    def test_purge_removes_only_old_failed_sessions(self):
        old_failed = self._session(processing_status=SupplierProductImportProcessingStatus.FAILED)
        fresh_failed = self._session(processing_status=SupplierProductImportProcessingStatus.FAILED)
        old_open = self._session()
        stale = timezone.now() - timedelta(hours=30)
        SupplierProductImportSession.objects.filter(pk__in=[old_failed.pk, old_open.pk]).update(updated_at=stale)

        result = purge_committed_product_import_sessions_task()

        self.assertEqual(result["purged_failed"], 1)
        self.assertFalse(SupplierProductImportSession.objects.filter(pk=old_failed.pk).exists())
        self.assertTrue(SupplierProductImportSession.objects.filter(pk=fresh_failed.pk).exists())
        self.assertTrue(SupplierProductImportSession.objects.filter(pk=old_open.pk).exists())


class DeadQueueTests(APITestCase):
    @patch("api.v1.suppliers.tasks.extract_supplier_product_import_session_task.delay", side_effect=OSError("down"))
    @patch("api.v1.suppliers.tasks.process_supplier_product_import_session_task.delay", side_effect=OSError("down"))
    def test_no_session_is_left_when_the_queue_is_down(self, _process, _extract):
        from django.core.files.uploadedfile import SimpleUploadedFile

        supplier = _supplier("Queue LLC")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_supplier_cabinet_tokens(supplier)['access']}")
        csv = "Название;Цена\nСахар 1 кг;100\n".encode()

        response = self.client.post(
            "/api/v1/suppliers/products/import/analyze",
            {"file": SimpleUploadedFile("p.csv", csv, content_type="text/csv")},
            format="multipart",
        )

        self.assertEqual(response.status_code, 503, response.content)
        self.assertFalse(SupplierProductImportSession.objects.filter(supplier=supplier).exists())


class PriceChangedFilterApiTests(APITestCase):
    def test_counts_and_filter_find_only_replacements_that_change_the_price(self):
        supplier = _supplier("Filter LLC")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_supplier_cabinet_tokens(supplier)['access']}")
        card_a = _card(supplier, supplier_raw_name="Сахар А", supplier_normalized_name="Сахар А")
        card_b = _card(supplier, supplier_raw_name="Сахар Б", supplier_normalized_name="Сахар Б")
        session = SupplierProductImportSession.objects.create(
            supplier=supplier, processing_status=SupplierProductImportProcessingStatus.READY
        )
        for number, (name, card, price) in enumerate(
            [("Сахар А", card_a, Decimal("65")), ("Сахар Б", card_b, Decimal("60"))], 1
        ):
            item = _row(session, number, name=name, price=price)
            set_supplier_product_import_item_action(
                item=item, action=SupplierProductImportAction.REPLACE, target_supplier_product_id=card.id
            )
        _row(session, 3, name="Новый", price=Decimal("10"), action=SupplierProductImportAction.NEW)

        url = f"/api/v1/suppliers/products/import/{session.id}"
        everything = self.client.get(url).json()
        changed = self.client.get(url, {"price_changed": "true"}).json()

        self.assertEqual(everything["counts"]["price_changed"], 1)
        self.assertEqual(len(everything["items"]), 3)
        self.assertEqual([item["name"] for item in changed["items"]], ["Сахар А"])
        self.assertEqual(changed["counts"]["price_changed"], 1)
