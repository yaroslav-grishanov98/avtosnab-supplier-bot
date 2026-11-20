from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from api.v1.suppliers.product_import_parsing import parse_supplier_product_import_file
from api.v1.suppliers.product_import_selectors import _flag_duplicate_names, _resolve_unit_label
from api.v1.suppliers.product_import_services import (
    _is_item_ready,
    _item_errors,
    apply_supplier_product_import_defaults,
    set_supplier_product_import_item_action,
)
from api.v1.suppliers.services import issue_supplier_cabinet_tokens
from core.models import Unit, UnitKind
from file_imports.canonical import override_snapshot
from suppliers.models import (
    ProductAvailability,
    Supplier,
    SupplierProductImportAction,
    SupplierProductImportCommitStatus,
    SupplierProductImportItem,
    SupplierProductImportSession,
)


class UnitAliasTests(TestCase):
    def test_pack_label_resolves_to_package_unit(self):
        unit = _resolve_unit_label("упак", {})
        self.assertEqual(unit.code, "уп")
        unit = _resolve_unit_label("упак.", {})
        self.assertEqual(unit.code, "уп")


def _parse_csv(text):
    upload = SimpleUploadedFile("price.csv", text.encode("utf-8"), content_type="text/csv")
    return parse_supplier_product_import_file(upload, canonical_enabled=True)


class DuplicateNameTests(TestCase):
    def test_colliding_cleaned_names_keep_measure(self):
        rows = _parse_csv("Номенклатура;Цена\nПюре Роллтон 10 кг;4806,34\nПюре Роллтон 240г;100\n")
        self.assertEqual([row["name"] for row in rows], ["Пюре Роллтон 10 кг", "Пюре Роллтон 240г"])

    def test_non_colliding_names_stay_clean(self):
        rows = _parse_csv("Номенклатура;Цена\nПюре Роллтон 10 кг, шт;4806,34\n")
        self.assertEqual([row["name"] for row in rows], ["Пюре Роллтон"])

    def test_identical_rows_second_goes_to_review(self):
        supplier = Supplier.objects.create(legal_name="Dup LLC", display_name="Dup LLC")
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        items = [
            SupplierProductImportItem.objects.create(
                session=session,
                row_number=number,
                raw_name=name,
                name=name,
                action=SupplierProductImportAction.NEW,
            )
            for number, name in enumerate(["Грибы", "грибы", "Мука"], start=1)
        ]
        _flag_duplicate_names(items=items)
        first, second, third = items
        self.assertEqual(second.action, SupplierProductImportAction.REVIEW)
        self.assertIsNone(second.target_supplier_product)
        self.assertEqual(second.validation_errors, ["Название повторяется в другой строке файла (строка 1)."])
        self.assertEqual(first.action, SupplierProductImportAction.NEW)
        self.assertEqual(third.action, SupplierProductImportAction.NEW)
        self.assertFalse(first.validation_errors)
        self.assertFalse(third.validation_errors)


class BatchedLlmPipelineTests(TestCase):
    def test_batch_skips_non_product_rows_shares_budget_and_keeps_disambiguation(self):
        from unittest.mock import patch

        from django.test import override_settings

        from file_imports.llm_candidates import BatchCandidateResponse

        text = (
            "Номенклатура;Цена\nПюре Роллтон 10 кг;4806,34\nПюре Роллтон 240г;100\nПюре Роллтон 5 кг;50\nИтого;4956\n"
        )
        with (
            override_settings(
                CANONICAL_IMPORT_LLM_ENABLED=True,
                CANONICAL_IMPORT_LLM_MAX_REQUESTS=1,
                CANONICAL_IMPORT_LLM_BATCH_SIZE=2,
            ),
            patch("file_imports.llm_candidates.LLMConnector") as connector,
        ):
            structured = connector.return_value.structured
            structured.return_value = BatchCandidateResponse(rows=[])
            rows = _parse_csv(text)
        # Budget of 1 is shared across chunks: 3 product rows / batch of 2 would need 2 requests.
        self.assertEqual(structured.call_count, 1)
        sent = [item for call in structured.call_args_list for item in call.kwargs["payload"]["rows"]]
        self.assertEqual(len(sent), 2)
        self.assertTrue(all("Итого" not in item["cells"].values() for item in sent))
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row["name"] for row in rows}), 3)


class ActionNewAcceptsIssuesTests(TestCase):
    def _item(self, snapshot):
        supplier = Supplier.objects.create(legal_name="Acc LLC", display_name="Acc LLC")
        session = SupplierProductImportSession.objects.create(supplier=supplier)
        unit, _ = Unit.objects.get_or_create(code="acc-u", defaults={"name": "acc-u", "kind": UnitKind.PIECE})
        return SupplierProductImportItem.objects.create(
            session=session,
            row_number=1,
            raw_name="Сахар",
            name="Сахар",
            unit=unit,
            country_of_origin="Россия",
            price=Decimal("10"),
            delivery_multiple=Decimal("1"),
            net_value=Decimal("1"),
            net_unit=unit,
            min_order_qty=Decimal("1"),
            availability=ProductAvailability.IN_STOCK,
            action=SupplierProductImportAction.REVIEW,
            parsed_snapshot=snapshot,
        )

    @staticmethod
    def _snapshot():
        return {
            "fields": {
                "pack_items_count": {"value": 3, "status": "ambiguous"},
                "net_value": {"value": "1", "status": "ambiguous"},
            },
            "parse_issues": [
                {
                    "field": field,
                    "code": "ambiguous",
                    "message": f"Поле {field}: ambiguous.",
                    "review_required": True,
                }
                for field in ("pack_items_count", "net_value")
            ],
        }

    def test_action_new_accepts_review_issues(self):
        item = self._item(self._snapshot())
        self.assertTrue(_item_errors(item))
        item = set_supplier_product_import_item_action(item=item, action=SupplierProductImportAction.NEW, actor_id=5)
        item.refresh_from_db()
        self.assertEqual(_item_errors(item), [])
        self.assertTrue(_is_item_ready(item))

    def test_action_skip_does_not_accept_issues(self):
        item = self._item(self._snapshot())
        item = set_supplier_product_import_item_action(item=item, action=SupplierProductImportAction.SKIP)
        self.assertTrue(_item_errors(item))
        self.assertNotIn("overrides", item.parsed_snapshot)

    def test_action_new_keeps_user_edited_field(self):
        snapshot = override_snapshot(self._snapshot(), {"net_value": Decimal("2")}, at="t", actor_id=1)
        item = self._item(snapshot)
        item = set_supplier_product_import_item_action(item=item, action=SupplierProductImportAction.NEW)
        item.refresh_from_db()
        net = [o for o in item.parsed_snapshot["overrides"] if o["field"] == "net_value"]
        self.assertEqual(len(net), 1)
        self.assertEqual(net[0]["value"], "2")


class ImportDefaultsTests(TestCase):
    def _make(self, session, number, **kwargs):
        unit, _ = Unit.objects.get_or_create(code="def-u", defaults={"name": "def-u", "kind": UnitKind.PIECE})
        values = {
            "session": session,
            "row_number": number,
            "raw_name": f"Товар {number}",
            "name": f"Товар {number}",
            "unit": unit,
            "price": Decimal("10"),
            "net_value": Decimal("1"),
            "net_unit": unit,
            "action": SupplierProductImportAction.NEW,
        }
        values.update(kwargs)
        return SupplierProductImportItem.objects.create(**values)

    def _session(self):
        supplier = Supplier.objects.create(legal_name="Def LLC", display_name="Def LLC")
        return SupplierProductImportSession.objects.create(supplier=supplier)

    def test_defaults_fill_only_empty_fields(self):
        session = self._session()
        empty = self._make(
            session,
            1,
            country_of_origin="",
            validation_errors=["Не заполнено поле country_of_origin.", "Другое."],
            parsed_snapshot={"fields": {}, "parse_issues": []},
        )
        filled = self._make(session, 2, country_of_origin="Италия")

        updated = apply_supplier_product_import_defaults(
            session=session, values={"country_of_origin": "Россия"}, actor_id=7
        )

        empty.refresh_from_db()
        filled.refresh_from_db()
        self.assertEqual(updated, 1)
        self.assertEqual(empty.country_of_origin, "Россия")
        self.assertEqual(filled.country_of_origin, "Италия")
        self.assertEqual(empty.validation_errors, ["Другое."])
        self.assertEqual(empty.field_sources["country_of_origin"], "user")
        self.assertNotIn("country_of_origin", filled.field_sources)
        override = [o for o in empty.parsed_snapshot["overrides"] if o["field"] == "country_of_origin"]
        self.assertEqual([o["value"] for o in override], ["Россия"])

    def test_defaults_fill_numbers_and_availability(self):
        session = self._session()
        item = self._make(
            session,
            1,
            country_of_origin="Россия",
            min_order_qty=None,
            delivery_multiple=None,
            availability="",
            parsed_snapshot={"fields": {}, "parse_issues": []},
        )

        updated = apply_supplier_product_import_defaults(
            session=session,
            values={
                "min_order_qty": Decimal("2"),
                "delivery_multiple": Decimal("3"),
                "availability": ProductAvailability.IN_STOCK,
            },
        )

        item.refresh_from_db()
        self.assertEqual(updated, 1)
        self.assertEqual(item.min_order_qty, Decimal("2"))
        self.assertEqual(item.delivery_multiple, Decimal("3"))
        self.assertEqual(item.availability, ProductAvailability.IN_STOCK)
        for key in ("min_order_qty", "delivery_multiple", "availability"):
            self.assertEqual(item.field_sources[key], "user", key)
        self.assertEqual(
            {o["field"] for o in item.parsed_snapshot["overrides"]},
            {"min_order_qty", "delivery_multiple", "availability_label"},
        )

    def test_defaults_leave_populated_values_and_overrides_untouched(self):
        session = self._session()
        snapshot = {"fields": {}, "parse_issues": []}
        populated = self._make(
            session, 1, country_of_origin="Россия", min_order_qty=Decimal("5"), parsed_snapshot=snapshot
        )
        empty = self._make(session, 2, country_of_origin="Россия", min_order_qty=None, parsed_snapshot=snapshot)

        updated = apply_supplier_product_import_defaults(session=session, values={"min_order_qty": Decimal("2")})

        populated.refresh_from_db()
        empty.refresh_from_db()
        self.assertEqual(updated, 1)
        self.assertEqual(populated.min_order_qty, Decimal("5"))
        self.assertNotIn("min_order_qty", populated.field_sources)
        self.assertNotIn("overrides", populated.parsed_snapshot)
        self.assertEqual(empty.min_order_qty, Decimal("2"))
        self.assertEqual([o["field"] for o in empty.parsed_snapshot["overrides"]], ["min_order_qty"])

    def test_defaults_skip_rows_with_skip_action_and_imported(self):
        session = self._session()
        skipped = self._make(session, 1, country_of_origin="", action=SupplierProductImportAction.SKIP)
        imported = self._make(session, 2, country_of_origin="", imported_at=timezone.now())

        updated = apply_supplier_product_import_defaults(session=session, values={"country_of_origin": "Россия"})

        skipped.refresh_from_db()
        imported.refresh_from_db()
        self.assertEqual(updated, 0)
        self.assertEqual(skipped.country_of_origin, "")
        self.assertEqual(imported.country_of_origin, "")

    def _piece_unit(self):
        unit, _ = Unit.objects.get_or_create(code="шт", defaults={"name": "шт", "kind": UnitKind.PIECE})
        return unit

    def test_unit_default_fills_only_rows_without_a_unit_and_derives_the_net(self):
        session = self._session()
        piece = self._piece_unit()
        snapshot = {"fields": {"net_value": {"status": "missing"}}, "parse_issues": []}
        empty = self._make(
            session,
            1,
            unit=None,
            net_value=None,
            net_unit=None,
            validation_errors=["Не заполнено поле unit_id.", "Не заполнено поле net_value.", "Другое."],
            parsed_snapshot=snapshot,
        )
        with_unit = self._make(session, 2)

        updated = apply_supplier_product_import_defaults(session=session, values={"unit": piece}, actor_id=7)

        empty.refresh_from_db()
        with_unit.refresh_from_db()
        self.assertEqual(updated, 1)
        self.assertEqual((empty.unit_id, empty.net_value, empty.net_unit_id), (piece.pk, Decimal("1"), piece.pk))
        self.assertEqual(empty.validation_errors, ["Другое."])
        self.assertEqual(empty.field_sources["unit"], "user")
        self.assertEqual((empty.field_sources["net_value"], empty.field_sources["net_unit"]), ("rule", "rule"))
        overridden = {o["field"]: o["value"] for o in empty.parsed_snapshot["overrides"]}
        self.assertEqual(overridden, {"unit_label": "шт", "net_value": "1", "net_unit_label": "шт"})
        self.assertNotEqual(with_unit.unit_id, piece.pk)
        self.assertNotIn("unit", with_unit.field_sources)

    def test_unit_default_keeps_a_net_that_the_row_already_has(self):
        session = self._session()
        gram, _ = Unit.objects.get_or_create(code="г", defaults={"name": "г", "kind": UnitKind.WEIGHT})
        row = self._make(
            session,
            1,
            unit=None,
            net_value=Decimal("90"),
            net_unit=gram,
            parsed_snapshot={"fields": {"net_value": {"status": "parsed"}}, "parse_issues": []},
        )

        apply_supplier_product_import_defaults(session=session, values={"unit": self._piece_unit()})

        row.refresh_from_db()
        self.assertEqual((row.net_value, row.net_unit_id), (Decimal("90"), gram.pk))
        self.assertNotIn("net_value", row.field_sources)

    def test_unit_default_does_not_invent_a_net_over_an_ambiguous_one(self):
        session = self._session()
        row = self._make(
            session,
            1,
            unit=None,
            net_value=None,
            net_unit=None,
            parsed_snapshot={"fields": {"net_value": {"status": "ambiguous"}}, "parse_issues": []},
        )

        apply_supplier_product_import_defaults(session=session, values={"unit": self._piece_unit()})

        row.refresh_from_db()
        self.assertIsNone(row.net_value)
        self.assertEqual(row.field_sources["unit"], "user")

    def test_a_later_unit_change_redoes_the_net_set_by_the_bulk_choice(self):
        from api.v1.suppliers.product_import_services import patch_supplier_product_import_item

        session = self._session()
        piece = self._piece_unit()
        kilo, _ = Unit.objects.get_or_create(code="кг", defaults={"name": "кг", "kind": UnitKind.WEIGHT})
        row = self._make(
            session,
            1,
            unit=None,
            net_value=None,
            net_unit=None,
            parsed_snapshot={"fields": {"net_value": {"status": "missing"}}, "parse_issues": []},
        )
        apply_supplier_product_import_defaults(session=session, values={"unit": piece})
        row.refresh_from_db()

        patch_supplier_product_import_item(item=row, data={"unit_id": kilo.pk})

        row.refresh_from_db()
        self.assertEqual((row.unit_id, row.net_value, row.net_unit_id), (kilo.pk, Decimal("1"), kilo.pk))


DEFAULTS_BASE = "/api/v1/suppliers/products/import"


class ImportDefaultsApiTests(APITestCase):
    def _setup(self, *, commit_status=SupplierProductImportCommitStatus.NONE):
        supplier = Supplier.objects.create(legal_name="Api LLC", display_name="Api LLC")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_supplier_cabinet_tokens(supplier)['access']}")
        session = SupplierProductImportSession.objects.create(supplier=supplier, commit_status=commit_status)
        item = SupplierProductImportItem.objects.create(
            session=session,
            row_number=1,
            raw_name="Сахар",
            name="Сахар",
            country_of_origin="",
            action=SupplierProductImportAction.NEW,
            validation_errors=["Не заполнено поле country_of_origin."],
        )
        return session, item

    def test_defaults_empty_body_returns_400(self):
        session, _ = self._setup()

        response = self.client.patch(f"{DEFAULTS_BASE}/{session.id}/items/defaults", {}, format="json")

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("non_field_errors", response.json())

    def test_defaults_during_commit_returns_409(self):
        session, _ = self._setup(commit_status=SupplierProductImportCommitStatus.RUNNING)

        response = self.client.patch(
            f"{DEFAULTS_BASE}/{session.id}/items/defaults", {"country_of_origin": "Россия"}, format="json"
        )

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json(), {"detail": "Идёт применение импорта."})

    def test_defaults_other_suppliers_session_returns_404(self):
        self._setup()
        other = Supplier.objects.create(legal_name="Other LLC", display_name="Other LLC")
        foreign = SupplierProductImportSession.objects.create(supplier=other)

        response = self.client.patch(
            f"{DEFAULTS_BASE}/{foreign.id}/items/defaults", {"country_of_origin": "Россия"}, format="json"
        )

        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(response.json(), {"detail": "No SupplierProductImportSession matches the given query."})

    def test_defaults_without_jwt_returns_401(self):
        session, _ = self._setup()
        self.client.credentials()

        response = self.client.patch(
            f"{DEFAULTS_BASE}/{session.id}/items/defaults", {"country_of_origin": "Россия"}, format="json"
        )

        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(response.json(), {"detail": "Учетные данные не были предоставлены."})

    def test_defaults_ok_returns_updated_count(self):
        session, item = self._setup()

        response = self.client.patch(
            f"{DEFAULTS_BASE}/{session.id}/items/defaults", {"country_of_origin": "Россия"}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"updated_count": 1})
        item.refresh_from_db()
        self.assertEqual(item.country_of_origin, "Россия")

    def test_defaults_unit_id_fills_rows_without_a_unit(self):
        session, item = self._setup()
        piece, _ = Unit.objects.get_or_create(code="шт", defaults={"name": "шт", "kind": UnitKind.PIECE})

        response = self.client.patch(
            f"{DEFAULTS_BASE}/{session.id}/items/defaults", {"unit_id": str(piece.pk)}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"updated_count": 1})
        item.refresh_from_db()
        self.assertEqual(item.unit_id, piece.pk)
        self.assertEqual(item.field_sources["unit"], "user")

    def test_defaults_unknown_unit_id_returns_400(self):
        session, _ = self._setup()

        response = self.client.patch(
            f"{DEFAULTS_BASE}/{session.id}/items/defaults",
            {"unit_id": "00000000-0000-0000-0000-000000000000"},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("unit_id", response.json())


class CorpusRegressionMetricsTests(TestCase):
    def test_duplicate_names_increase_is_regression(self):
        from suppliers.management.commands.check_supplier_import_corpus import compare_with_baseline

        base = {"rows": 10, "with_price": 10, "with_numeric_price": 10, "with_unit": 10, "with_sku": 0}
        regressions, improvements = compare_with_baseline(
            {**base, "duplicate_names": 5, "unpriced_rows": 0}, {**base, "duplicate_names": 2, "unpriced_rows": 0}
        )
        self.assertEqual(regressions, ["duplicate_names: 2 -> 5"])
        self.assertEqual(improvements, [])

    def test_lower_is_better_decrease_is_improvement_and_missing_metric_is_skipped(self):
        from suppliers.management.commands.check_supplier_import_corpus import compare_with_baseline

        base = {"rows": 10, "with_price": 10, "with_numeric_price": 10, "with_unit": 10, "with_sku": 0}
        regressions, improvements = compare_with_baseline(
            {**base, "duplicate_names": 1, "unpriced_rows": 3}, {**base, "duplicate_names": 4}
        )
        self.assertEqual(regressions, [])
        self.assertEqual(improvements, ["duplicate_names: 4 -> 1"])

    def test_update_merges_into_existing_baseline(self):
        import json
        import tempfile
        from io import StringIO
        from pathlib import Path

        from django.core.management import call_command

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "prices"
            directory.mkdir()
            (directory / "tiny.csv").write_text(
                "Наименование;Цена;Ед\nМолоко 1л;100;шт\nМолоко 1л;110;шт\nСыр;;шт\n", encoding="utf-8"
            )
            expected_path = Path(tmp) / "expected.json"
            other = {"rows": 7, "with_price": 7, "with_numeric_price": 7, "with_unit": 7, "with_sku": 0}
            expected_path.write_text(json.dumps({"other.xlsx": other}), encoding="utf-8")

            call_command(
                "check_supplier_import_corpus",
                dir=str(directory),
                expected=str(expected_path),
                update=True,
                stdout=StringIO(),
            )

            data = json.loads(expected_path.read_text(encoding="utf-8"))
        self.assertEqual(data["other.xlsx"], other)
        self.assertIn("tiny.csv", data)
        self.assertIn("duplicate_names", data["tiny.csv"])
        self.assertIn("unpriced_rows", data["tiny.csv"])
