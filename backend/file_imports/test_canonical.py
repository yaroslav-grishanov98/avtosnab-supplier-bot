from decimal import Decimal

from django.test import SimpleTestCase, override_settings

from file_imports.canonical import (
    FieldCandidate,
    accept_issues,
    effective_issues,
    override_snapshot,
    parse_product_row,
    resolve_field,
)
from file_imports.types import RawImportRow


class CanonicalParsingTests(SimpleTestCase):
    def parse(self, name, **cells):
        return parse_product_row(RawImportRow(20, {"name": name, **cells}, {"sheet_name": "Прайс"}), lambda key: key)

    def test_adzhika_clean_name_and_independent_business_fields(self):
        row = self.parse('Аджика "Балтийская" в ведре 1/5 кг, упак')
        self.assertEqual(row.fields["name"].value, 'Аджика "Балтийская"')
        # D2: «1/5 кг» is one container of 5 kg; the name says it is a bucket, so 1 x 5 kg is kept with the type.
        self.assertEqual(row.fields["pack_items_count"].value, 1)
        self.assertEqual(row.fields["pack_item_value"].value, Decimal("5"))
        self.assertEqual(row.fields["net_value"].value, Decimal("5"))
        self.assertEqual(row.fields["net_unit_label"].value, "кг")
        self.assertEqual(row.fields["pack_type_label"].value, "ведро")
        self.assertEqual(row.fields["unit_label"].value, "упак")
        self.assertEqual(row.fields["pack_item_value"].evidence, "1/5 кг")
        self.assertEqual(row.fields["pack_item_value"].source, "name")
        self.assertEqual(row.fields["pack_item_value"].status, "inferred")
        for key in ("min_order_qty", "delivery_multiple", "price_basis"):
            self.assertIsNone(row.fields[key].value)
            self.assertEqual(row.fields[key].status, "missing")
        self.assertEqual(row.raw["cells"]["name"], 'Аджика "Балтийская" в ведре 1/5 кг, упак')

    def test_country_column_alias_is_canonical(self):
        row = self.parse("Сыр", country_of_origin="РФ")
        field = row.fields["country_of_origin"]
        self.assertEqual(field.value, "Россия")
        self.assertEqual(field.source, "column")
        self.assertEqual(field.confidence, 1.0)
        self.assertEqual(field.raw_value, "РФ")

    def test_country_column_unknown_text_kept_for_review(self):
        text = "Россия, Краснодарский край"
        row = self.parse("Сыр", country_of_origin=text)
        field = row.fields["country_of_origin"]
        self.assertEqual(field.value, text)
        self.assertEqual(field.status, "ambiguous")
        self.assertEqual(field.raw_value, text)
        self.assertTrue([i for i in row.parse_issues if i["field"] == "country_of_origin"])

    def test_country_column_blank_is_not_ambiguous(self):
        row = self.parse("Сыр", country_of_origin="  ")
        self.assertNotEqual(row.fields["country_of_origin"].status, "ambiguous")

    def test_country_column_confidence_only_for_a_recognized_value(self):
        recognized = self.parse("Сыр", country_of_origin="Италия").fields["country_of_origin"]
        unknown = self.parse("Сыр", country_of_origin="Россия, Краснодарский край").fields["country_of_origin"]

        self.assertEqual(recognized.confidence, 1.0)
        self.assertEqual(unknown.status, "ambiguous")
        self.assertIsNone(unknown.confidence)

    def test_country_column_with_a_trailing_dot_is_recognized(self):
        field = self.parse("Сыр", country_of_origin="РФ.").fields["country_of_origin"]

        self.assertEqual(field.value, "Россия")
        self.assertNotEqual(field.status, "ambiguous")

    def test_country_in_the_name_is_parsed(self):
        field = self.parse("Макароны 500г, Турция").fields["country_of_origin"]

        self.assertEqual(field.value, "Турция")
        self.assertEqual(field.source, "name")
        self.assertEqual(field.status, "parsed")
        self.assertEqual(field.confidence, 0.95)

    def test_package_variants(self):
        single = self.parse("Товар 1/0,295 кг")
        self.assertIsNone(single.fields["pack_items_count"].value)
        self.assertEqual(single.fields["net_value"].value, Decimal("0.295"))
        for text, count, amount, unit in [
            ("12 × 200 г", 12, "200", "г"),
            ("кор 12 шт по 200 г", 12, "200", "г"),
            ("6×1 л", 6, "1", "л"),
            ("20*0,5л", 20, "0.5", "л"),
            ("4 x 2.5 кг", 4, "2.5", "кг"),
        ]:
            with self.subTest(text=text):
                row = self.parse("Товар " + text)
                self.assertEqual(row.fields["pack_items_count"].value, count)
                self.assertEqual(row.fields["pack_item_value"].value, Decimal(amount))
                self.assertEqual(row.fields["pack_item_unit_label"].value, unit)
                self.assertIsNone(row.fields["min_order_qty"].value)

    def test_missing_vs_invalid_and_zero_price(self):
        self.assertEqual(self.parse("Товар").fields["price"].status, "missing")
        for price in ("abc", "-100", "NaN", "Infinity"):
            self.assertEqual(self.parse("Товар", price=price).fields["price"].status, "invalid")
        self.assertEqual(self.parse("Товар", price="0").fields["price"].value, Decimal(0))

    def test_conflict_preserves_name_and_candidates(self):
        row = self.parse("Товар 500 г", net_value="5 кг")
        self.assertEqual(row.fields["net_value"].status, "ambiguous")
        self.assertEqual(len(row.fields["net_value"].candidates), 2)
        self.assertEqual(row.fields["name"].value, "Товар 500 г")
        self.assertTrue(effective_issues(row.snapshot()))

    def test_equivalent_measures_do_not_conflict(self):
        row = self.parse("Товар 500 г", net_value="0,5 кг")
        self.assertNotEqual(row.fields["net_value"].status, "ambiguous")
        self.assertNotEqual(row.fields["net_unit_label"].status, "ambiguous")

    def test_measure_glued_to_a_case_ratio_is_still_found(self):
        row = self.parse("Соевый соус ORIENT TASTE 1000 мл1/12 , шт", price="213,81")
        self.assertEqual(row.fields["net_value"].value, Decimal("1000"))
        self.assertEqual(row.fields["net_unit_label"].value, "мл")
        self.assertEqual(row.fields["unit_label"].value, "шт")

    def test_price_basis_wins_over_case_weight(self):
        for flavour in ("клубника", "вишня"):
            row = self.parse(f"Наполнитель плодово ягодный {flavour} (цена за 1 кг) 50%HELCOM 12 кг, кг", price="295,1")
            self.assertEqual(row.fields["net_value"].value, Decimal("1"))
            self.assertEqual(row.fields["net_unit_label"].value, "кг")
            self.assertNotEqual(row.fields["net_value"].status, "ambiguous")
            self.assertEqual(row.fields["unit_label"].value, "кг")
            self.assertNotIn("цена за", row.fields["name"].value)

    @override_settings(CANONICAL_DEFAULT_PIECE_UNIT=True)
    def test_piece_without_weight_gets_one_piece_net(self):
        for name, price in (("манго Тайланд, шт", "220"), ("манго среднее штука", "120")):
            row = self.parse(name, price=price)
            self.assertEqual(row.fields["unit_label"].value, "шт")
            self.assertEqual(row.fields["net_value"].value, Decimal("1"))
            self.assertEqual(row.fields["net_unit_label"].value, "шт")
            self.assertEqual(row.fields["net_value"].status, "inferred")

    def test_piece_net_is_not_invented_over_a_measure_or_a_conflict(self):
        measured = self.parse("Сок яблочный 1 л, шт", price="90")
        self.assertEqual(measured.fields["net_unit_label"].value, "л")
        conflicting = self.parse("Товар 500 г 1 кг, шт", price="90")
        self.assertEqual(conflicting.fields["net_value"].status, "ambiguous")
        self.assertNotEqual(conflicting.fields["net_unit_label"].value, "шт")

    def test_no_invented_weight(self):
        name = "Биточки замороженные в вакуумной упаковке"
        row = self.parse(name)
        self.assertEqual(row.fields["name"].value, name)
        self.assertEqual(row.fields["net_value"].status, "missing")

    def test_weight_word_gives_kg_unit_and_unit_net(self):
        row = self.parse("авокадо ВЕСОВОЙ", price="330")
        self.assertEqual(row.fields["unit_label"].value, "кг")
        self.assertEqual(row.fields["unit_label"].source, "rule")
        self.assertEqual(row.fields["net_value"].value, Decimal("1"))
        self.assertEqual(row.fields["net_unit_label"].value, "кг")

    def test_weight_word_with_a_piece_weight_is_sold_by_kg(self):
        # D10: «вес»/«весовой» means sold by the kilogram; the weight in the name is only the piece's reference weight.
        cases = (("Сыр весовой 300 г", "0.3"), ("Печенье весовое 2,5 кг", "2.5"), ("Конфеты, вес 500 г", "0.5"))
        for name, reference in cases:
            with self.subTest(name=name):
                row = self.parse(name, price="100")
                self.assertEqual(row.fields["unit_label"].value, "кг")
                self.assertNotEqual(row.fields["unit_label"].status, "ambiguous")
                net = (row.fields["net_value"].value, row.fields["net_unit_label"].value)
                self.assertEqual(net, (Decimal("1"), "кг"))
                self.assertEqual(row.fields["estimated_piece_weight"].value, Decimal(reference))

    def test_weight_word_with_unit_net_or_no_net_is_not_ambiguous(self):
        for name in ("Мука весовая", "Сыр весовой 1 кг", "Сыр весовой 1000 г"):
            with self.subTest(name=name):
                row = self.parse(name, price="100")
                self.assertEqual(row.fields["unit_label"].value, "кг")
                self.assertNotEqual(row.fields["unit_label"].status, "ambiguous")

    def test_column_unit_with_weight_word_and_net_untouched(self):
        row = self.parse("Сыр весовой 300 г", unit_label="кг", price="100")
        self.assertNotEqual(row.fields["unit_label"].status, "ambiguous")

    def test_defaulted_unit_is_requested_from_llm(self):
        from file_imports.llm_candidates import requested_fields

        self.assertIn("unit_label", requested_fields(self.parse("Бульон Роллтон 90г", price="36")))
        self.assertNotIn("unit_label", requested_fields(self.parse("Бульон Роллтон 90г", unit_label="шт", price="36")))

    def test_missing_unit_stays_missing_for_a_manual_choice(self):
        # D12: the import spec (section 52) asks for a manual choice when the unit is absent; no silent "шт".
        row = self.parse("Бульон Роллтон 90г", price="36")
        self.assertIsNone(row.fields["unit_label"].value)
        self.assertEqual(row.fields["unit_label"].status, "missing")

    @override_settings(CANONICAL_DEFAULT_PIECE_UNIT=True)
    def test_piece_default_is_marked_defaulted_when_the_switch_is_on(self):
        row = self.parse("Бульон Роллтон 90г", price="36")
        self.assertEqual(row.fields["unit_label"].value, "шт")
        self.assertEqual(row.fields["unit_label"].status, "defaulted")

    def test_column_unit_beats_rule_and_default(self):
        row = self.parse("Мука весовая 5 шт", unit_label="кг", price="50")
        self.assertEqual(row.fields["unit_label"].source, "column")
        self.assertEqual(row.fields["unit_label"].value, "кг")

    def test_blank_unit_column_is_missing(self):
        row = self.parse("Бульон Роллтон 90г", unit_label="", price="36")
        self.assertIsNone(row.fields["unit_label"].value)
        self.assertEqual(row.fields["unit_label"].status, "missing")

    @override_settings(CANONICAL_DEFAULT_PIECE_UNIT=True)
    def test_blank_unit_column_does_not_block_default_when_the_switch_is_on(self):
        row = self.parse("Бульон Роллтон 90г", unit_label="", price="36")
        self.assertEqual(row.fields["unit_label"].value, "шт")
        self.assertEqual(row.fields["unit_label"].status, "defaulted")

    def test_no_price_no_unit_default(self):
        row = self.parse("Бульоны, супы")
        self.assertEqual(row.fields["unit_label"].status, "missing")

    def test_unit_net_added_for_piece_without_measure(self):
        # Changed 2026-10-14 on the product owner's request: "манго, шт" without a weight is one piece, not a gap.
        row = self.parse("Товар", unit_label="шт", price="36")
        self.assertEqual(row.fields["net_value"].value, Decimal("1"))
        self.assertEqual(row.fields["net_unit_label"].value, "шт")
        self.assertEqual(row.fields["net_value"].status, "inferred")

    def test_llm_cannot_overwrite_column(self):
        parsed = resolve_field(
            "unit_label", [FieldCandidate("шт", source="column"), FieldCandidate("кг", source="llm", evidence="кг")]
        )
        self.assertEqual(parsed.value, "шт")
        self.assertEqual(parsed.status, "ambiguous")
        self.assertEqual(len(parsed.candidates), 2)
        self.assertEqual(resolve_field("price", [FieldCandidate("100", source="llm")]).status, "ambiguous")

    def test_manual_override_preserves_original_and_resolves_only_edited_field(self):
        snapshot = self.parse("Товар 500 г", net_value="5 кг", price="abc").snapshot()
        changed = override_snapshot(snapshot, {"price": Decimal("100")})
        self.assertEqual(changed["fields"]["price"]["status"], "invalid")
        self.assertEqual(changed["overrides"][0]["source"], "user")
        self.assertTrue(any(i["field"] == "net_value" for i in effective_issues(changed)))
        self.assertFalse(any(i["field"] == "price" for i in effective_issues(changed)))
        self.assertNotIn("overrides", snapshot)

    def test_non_product_rows(self):
        for name, kind in [("Итого", "total"), ("Наименование товара", "header")]:
            self.assertEqual(self.parse(name).row_type, kind)

    def test_duplicate_net_columns_keep_both_candidates(self):
        raw = RawImportRow(2, {"name": "Мука", "вес": "5 кг", "вес_2": "6 кг"})
        parsed = parse_product_row(raw, lambda key: "net_value" if key.startswith("вес") else key)
        self.assertEqual(len(parsed.fields["net_value"].candidates), 2)
        self.assertEqual(parsed.fields["net_value"].status, "ambiguous")

    def parse_qty(self, name, qty):
        mapper = lambda key: {"name": "name", "qty": "pack_items_count"}.get(key, key)  # noqa: E731
        return parse_product_row(RawImportRow(2, {"name": name, "qty": qty}), mapper)

    def test_pack_count_column_with_name_measure_gives_composition(self):
        row = self.parse_qty("Бульон куриный Mareven 2кг", "4")
        self.assertEqual(row.fields["pack_items_count"].value, 4)
        self.assertEqual(row.fields["pack_items_count"].status, "parsed")
        self.assertEqual(row.fields["pack_item_value"].value, Decimal("2"))
        self.assertEqual(row.fields["pack_item_value"].source, "calculated")
        self.assertEqual(row.fields["pack_item_unit_label"].value, "кг")
        self.assertEqual(row.fields["pack_item_unit_label"].source, "calculated")

    def test_column_count_resolves_ratio_ambiguity(self):
        row = self.parse_qty("Мороженое Ваниль 450гр (1/5)", "5")
        self.assertNotEqual(row.fields["pack_items_count"].status, "ambiguous")
        self.assertFalse([i for i in row.parse_issues if i["field"] == "pack_items_count"])

    def test_caliber_range_is_not_pack_ambiguity(self):
        row = self.parse("Устрицы Маака 100/150")
        self.assertEqual(row.fields["pack_items_count"].status, "missing")
        self.assertEqual(row.parse_issues, [])

    def test_unitless_ratio_without_column_stays_ambiguous(self):
        row = self.parse("картофель очищенный фасовка 1/3 весовой")
        self.assertEqual(row.fields["pack_items_count"].status, "ambiguous")

    def test_pack_only_doubt_does_not_require_review(self):
        row = self.parse("картофель очищенный фасовка 1/3 весовой")
        snapshot = row.snapshot()
        self.assertTrue([i for i in snapshot["parse_issues"] if i["field"] == "pack_items_count"])
        self.assertEqual(effective_issues(snapshot), [])

    def test_pack_doubt_does_not_hide_weight_doubt(self):
        snapshot = self.parse("Товар ~500 г (8/1)").snapshot()
        self.assertTrue(
            all(i["field"] not in ("pack_items_count", "pack_item_value") for i in effective_issues(snapshot))
        )
        self.assertTrue(any(i["field"] == "net_value" for i in effective_issues(snapshot)))

    def test_pack_quantity_headers_map_to_pack_items_count(self):
        from api.v1.suppliers.product_import_parsing import _field_for_header_key

        for header in ("Кол. в упаковке", "Кол-во в упаковке", "Количество в упаковке"):
            self.assertEqual(_field_for_header_key(header), "pack_items_count", header)

    def test_price_basis_does_not_come_from_package(self):
        parsed = parse_product_row(
            RawImportRow(2, {"name": "Мука 1/5 кг", "цена за кг": "1 100,00 руб."}),
            lambda key: "price" if key.startswith("цена") else key,
        )
        self.assertEqual(parsed.fields["price"].value, Decimal("1100.00"))
        self.assertEqual(parsed.fields["currency"].source, "column")
        self.assertEqual(parsed.fields["price_basis"].value, "кг")
        self.assertEqual(parsed.fields["price_basis"].evidence, "цена за кг")

    def test_approximate_weight_stays_for_review(self):
        name = "Сыр Гауда 45% БЗМЖ круг 1/4 ~3,5кг"
        parsed = self.parse(name)
        self.assertEqual(parsed.fields["net_value"].status, "ambiguous")
        self.assertEqual(parsed.fields["name"].value, name)

    def test_gram_spelling_and_grouped_number(self):
        parsed = self.parse("Кетчуп 1/5 000 гр, шт")
        # D2: one container of 5000 g -- the net, not a 1 x 5000 g composition.
        self.assertIsNone(parsed.fields["pack_items_count"].value)
        self.assertEqual(parsed.fields["net_value"].value, Decimal("5000"))
        self.assertEqual(parsed.fields["net_unit_label"].value, "г")
        self.assertEqual(parsed.fields["name"].value, "Кетчуп")

    def test_cleaned_names_have_no_orphan_punctuation(self):
        cases = {
            "морс клюквенный 0,9 л, пюр-пак": "морс клюквенный, пюр-пак",
            "помидоры ЧЕРРИ упак. (1/0,25кг)": "помидоры ЧЕРРИ упак.",
            "Бульон Роллтон 200г.Говядина домашний": "Бульон Роллтон Говядина домашний",
            "Сыр Большой куш, 20%, 350г, брусок , шт": "Сыр Большой куш, 20%, брусок",
            "Лапша яичная 500 гр, 30 шт/кор, Китай 1/30, шт": "Лапша яичная, 30 шт, Китай 1/30",
            "Кавказские травы 10гр*40шт Relish": "Кавказские травы Relish",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(self.parse(raw).fields["name"].value, expected)

    def test_llm_fabricated_evidence_is_rejected(self):
        from unittest.mock import patch

        from django.test import override_settings

        from file_imports.llm_candidates import CandidateResponse, SuggestedField, enrich_candidates

        parsed = self.parse("Бифштекс")
        response = CandidateResponse(candidates=[SuggestedField(field="unit_label", value="шт", evidence="1 шт")])
        with (
            override_settings(CANONICAL_IMPORT_LLM_ENABLED=True),
            patch("file_imports.llm_candidates.LLMConnector") as connector,
        ):
            connector.return_value.structured.return_value = response
            enriched = enrich_candidates(parsed, request_budget=[1])
        self.assertEqual(enriched.fields["unit_label"].status, "missing")

    def _enrich(self, parsed, *suggestions):
        from unittest.mock import patch

        from django.test import override_settings

        from file_imports.llm_candidates import CandidateResponse, SuggestedField, enrich_candidates

        response = CandidateResponse(
            candidates=[SuggestedField(field=f, value=v, evidence=e) for f, v, e in suggestions]
        )
        with (
            override_settings(CANONICAL_IMPORT_LLM_ENABLED=True),
            patch("file_imports.llm_candidates.LLMConnector") as connector,
        ):
            connector.return_value.structured.return_value = response
            enriched = enrich_candidates(parsed, request_budget=[1])
        return enriched, connector

    def test_llm_payload_is_labelled_by_column(self):
        parsed = self.parse("Сыр 3 шт", price="330")
        _, connector = self._enrich(parsed)
        payload = connector.return_value.structured.call_args.kwargs["payload"]
        (row_payload,) = payload["rows"]
        self.assertEqual(row_payload["row_number"], 20)
        self.assertEqual(row_payload["cells"], {"name": "Сыр 3 шт", "price": "330"})
        self.assertIsInstance(row_payload["requested_fields"], list)

    def test_llm_not_asked_for_country_without_hint(self):
        from file_imports.llm_candidates import requested_fields

        self.assertNotIn("country_of_origin", requested_fields(self.parse("Мука 1 кг")))
        self.assertIn("country_of_origin", requested_fields(self.parse("Сыр итальянский")))

    def test_llm_not_asked_for_pack_fields_without_digits(self):
        from file_imports.llm_candidates import requested_fields

        fields = requested_fields(self.parse("авокадо"))
        self.assertFalse([f for f in fields if f.startswith("pack_")])

    def test_llm_pack_value_equal_to_price_rejected(self):
        parsed = self.parse("Сыр 5 кг", price="5")
        enriched, _ = self._enrich(parsed, ("pack_item_value", "5", "5 кг"))
        self.assertIsNone(enriched.fields["pack_item_value"].value)

    def test_llm_count_from_non_name_cell_rejected(self):
        parsed = self.parse("Сыр 3 шт", price="330", number="1")
        enriched, _ = self._enrich(parsed, ("pack_items_count", "330", "330"), ("pack_items_count", "1", "1"))
        self.assertIsNone(enriched.fields["pack_items_count"].value)

    def test_llm_country_must_be_country(self):
        parsed = self.parse("Сыр Италия 5 кг", price="110", qty="шт", region="Калининградский")
        enriched, _ = self._enrich(
            parsed,
            ("country_of_origin", "110", "110"),
            ("country_of_origin", "шт", "шт"),
            ("country_of_origin", "Калининградский", "Калининградский"),
        )
        self.assertIsNone(enriched.fields["country_of_origin"].value)
        enriched, _ = self._enrich(self.parse("Сыр Италия 5 кг"), ("country_of_origin", "Италия", "Италия"))
        self.assertEqual(enriched.fields["country_of_origin"].value, "Италия")

    def test_llm_unit_label_not_taken_from_quantity(self):
        enriched, _ = self._enrich(self.parse("Пюре Роллтон 10 кг"), ("unit_label", "кг", "10 кг"))
        self.assertIsNone(enriched.fields["unit_label"].value)
        self.assertNotIn("llm_value_review", [i["code"] for i in enriched.parse_issues])
        enriched, _ = self._enrich(self.parse("Сыр Бри, шт"), ("unit_label", "шт", "шт"))
        self.assertEqual(enriched.fields["unit_label"].value, "шт")

    def test_llm_pack_numbers_match_whole_tokens(self):
        enriched, _ = self._enrich(self.parse("Мука 10 кг"), ("pack_item_value", "1", "10 кг"))
        self.assertIsNone(enriched.fields["pack_item_value"].value)
        enriched, _ = self._enrich(self.parse("Гвозди 10 шт"), ("pack_items_count", "1", "10 шт"))
        self.assertIsNone(enriched.fields["pack_items_count"].value)
        enriched, _ = self._enrich(self.parse("Мука 25 кг"), ("pack_items_count", "25", "25"))
        self.assertIsNone(enriched.fields["pack_items_count"].value)
        enriched, _ = self._enrich(self.parse("Гвозди 6 шт"), ("pack_items_count", "6", "6 шт"))
        self.assertEqual(enriched.fields["pack_items_count"].value, 6)
        enriched, _ = self._enrich(self.parse("Мука 0,5 кг"), ("pack_item_value", "0,5", "0,5 кг"))
        self.assertEqual(enriched.fields["pack_item_value"].value, Decimal("0.5"))

    def test_accepted_llm_value_requires_review(self):
        enriched, _ = self._enrich(self.parse("Сыр Италия 5 кг"), ("country_of_origin", "Италия", "Италия"))
        issues = effective_issues(enriched.snapshot())
        self.assertIn(("llm_value_review", "country_of_origin"), [(i["code"], i["field"]) for i in issues])

    def test_mapping_fallback_cached_and_requires_review(self):
        from unittest.mock import patch

        from django.test import override_settings

        from file_imports.llm_candidates import (
            MappingEntry,
            MappingResponse,
            apply_mapping_provenance,
            mapping_candidates,
        )

        raw = RawImportRow(2, {"name": "Мука", "Стоим.": "100"})

        def mapper(key):
            return "name" if key == "name" else None

        cache = {}
        with (
            override_settings(CANONICAL_IMPORT_LLM_ENABLED=True),
            patch("file_imports.llm_candidates.LLMConnector") as connector,
        ):
            connector.return_value.structured.return_value = MappingResponse(
                mapping=[MappingEntry(column="Стоим.", field="price", evidence="Стоим.")]
            )
            mapping = mapping_candidates(raw, mapper, cache)
            self.assertEqual(mapping_candidates(raw, mapper, cache), mapping)
            self.assertEqual(connector.return_value.structured.call_count, 1)
        parsed = parse_product_row(raw, lambda key: mapper(key) or mapping.get(key))
        apply_mapping_provenance(parsed, mapping)
        self.assertEqual(parsed.mapping["price"][0]["source"], "llm")
        self.assertEqual(parsed.fields["price"].status, "ambiguous")

    def test_mapping_ignores_columns_the_rules_already_know(self):
        from unittest.mock import patch

        from django.test import override_settings

        from file_imports.llm_candidates import (
            MappingEntry,
            MappingResponse,
            apply_mapping_provenance,
            mapping_candidates,
        )

        raw = RawImportRow(2, {"name": "Мука", "Стоим.": "100"})

        def mapper(key):
            return "name" if key == "name" else None

        with (
            override_settings(CANONICAL_IMPORT_LLM_ENABLED=True),
            patch("file_imports.llm_candidates.LLMConnector") as connector,
        ):
            connector.return_value.structured.return_value = MappingResponse(
                mapping=[
                    MappingEntry(column="name", field="name", evidence="name"),
                    MappingEntry(column="Стоим.", field="price", evidence="Стоим."),
                ]
            )
            mapping = mapping_candidates(raw, mapper, {})
        self.assertEqual(mapping, {"Стоим.": "price"})
        parsed = parse_product_row(raw, lambda key: mapper(key) or mapping.get(key))
        apply_mapping_provenance(parsed, mapping)
        self.assertNotEqual(parsed.mapping["name"][0]["source"], "llm")
        self.assertNotEqual(parsed.fields["name"].status, "ambiguous")
        self.assertEqual([i["field"] for i in parsed.parse_issues if i["code"] == "llm_mapping_review"], ["price"])

    def test_mapping_entry_rejects_non_field_value(self):
        from pydantic import ValidationError

        from file_imports.llm_candidates import MappingEntry

        with self.assertRaises(ValidationError):
            MappingEntry(column="цена", field="цена", evidence="цена")

    def test_mapping_schema_passes_strict_json_schema_with_enum(self):
        from api.llm.connector import _strict_json_schema
        from file_imports.llm_candidates import MappingField, MappingResponse

        schema = _strict_json_schema(MappingResponse.model_json_schema())
        field = schema["$defs"]["MappingEntry"]["properties"]["field"]
        self.assertEqual(set(field["enum"]), set(MappingField.__args__))

    def test_mapping_response_drops_only_invalid_entries(self):
        from file_imports.llm_candidates import MappingResponse

        response = MappingResponse.model_validate(
            {
                "mapping": [
                    {"column": "Стоим.", "field": "price", "evidence": "Стоим."},
                    {"column": "цена", "field": "цена", "evidence": "цена"},
                    {"column": "x", "evidence": "x"},
                    "junk",
                ]
            }
        )
        self.assertEqual([(e.column, e.field) for e in response.mapping], [("Стоим.", "price")])

    def test_mapping_candidates_keeps_valid_entries_when_one_is_invalid(self):
        from unittest.mock import patch

        from django.test import override_settings

        from file_imports.llm_candidates import MappingResponse, mapping_candidates

        raw = RawImportRow(2, {"name": "Мука", "Стоим.": "100", "цена": "1"})
        with (
            override_settings(CANONICAL_IMPORT_LLM_ENABLED=True),
            patch("file_imports.llm_candidates.LLMConnector") as connector,
        ):
            connector.return_value.structured.return_value = MappingResponse.model_validate(
                {
                    "mapping": [
                        {"column": "Стоим.", "field": "price", "evidence": "Стоим."},
                        {"column": "цена", "field": "цена", "evidence": "цена"},
                    ]
                }
            )
            mapping = mapping_candidates(raw, lambda key: "name" if key == "name" else None, {})
        self.assertEqual(mapping, {"Стоим.": "price"})

    def test_mapping_prompt_lists_allowed_fields(self):
        from unittest.mock import patch

        from django.test import override_settings

        from file_imports.llm_candidates import MappingResponse, mapping_candidates

        raw = RawImportRow(2, {"name": "Мука", "Стоим.": "100"})
        fields = (
            "name price unit_label net_value net_unit_label supplier_sku availability_label "
            "package_composition country_of_origin delivery_multiple min_order_qty pack_items_count"
        ).split()
        with (
            override_settings(CANONICAL_IMPORT_LLM_ENABLED=True),
            patch("file_imports.llm_candidates.LLMConnector") as connector,
        ):
            connector.return_value.structured.return_value = MappingResponse(mapping=[])
            mapping_candidates(raw, lambda key: "name" if key == "name" else None, {})
        prompt = connector.return_value.structured.call_args.kwargs["system_prompt"]
        for field in fields:
            self.assertIn(f"- {field}:", prompt)
        self.assertIn("колонки без подходящего поля не включать", prompt)
        self.assertIn("Evidence должно совпадать с заголовком", prompt)

    def test_diagnostic_schema_extension_does_not_add_required_fields(self):
        from file_imports.schema import optional_import_diagnostics

        schema = {
            "components": {
                "schemas": {
                    "SupplierProductImportItem": {"required": ["id", "name", "field_meta", "parser_version"]},
                    "Other": {"required": ["parser_version"]},
                }
            }
        }
        result = optional_import_diagnostics(schema, None, None, True)
        self.assertEqual(result["components"]["schemas"]["SupplierProductImportItem"]["required"], ["id", "name"])
        self.assertEqual(result["components"]["schemas"]["Other"]["required"], ["parser_version"])


@override_settings(CANONICAL_IMPORT_COUNTRY_HINTS_ENABLED=True)
class SectionCountryTests(SimpleTestCase):
    @staticmethod
    def _parse(name, section_path=None, **source):
        if section_path is not None:
            source["section_path"] = section_path
        return parse_product_row(RawImportRow(7, {"name": name}, source), lambda key: key)

    def test_section_country_candidate(self):
        field = self._parse("Макароны 500г", ["Италия", "Barilla"]).fields["country_of_origin"]

        self.assertEqual(field.value, "Италия")
        self.assertEqual(field.source, "section")
        self.assertEqual(field.status, "inferred")
        self.assertEqual(field.confidence, 0.8)
        self.assertEqual(field.evidence, "Италия")
        self.assertEqual(field.reliability, "requires_review")

    def test_section_heading_that_is_not_a_country_adds_nothing(self):
        field = self._parse("Соус острый 200г", ["Соус Чили"]).fields["country_of_origin"]

        self.assertIsNone(field.value)
        self.assertEqual(field.status, "missing")

    def test_missing_section_path_adds_nothing(self):
        field = self._parse("Макароны 500г").fields["country_of_origin"]

        self.assertIsNone(field.value)
        self.assertEqual(field.status, "missing")

    def test_innermost_country_heading_wins(self):
        field = self._parse("Бульон 100г", ["Беларусь", "Бульоны", "Mareven"]).fields["country_of_origin"]
        self.assertEqual(field.value, "Беларусь")
        self.assertEqual(field.evidence, "Беларусь")

        field = self._parse("Сыр 1 кг", ["Италия", "Испания"]).fields["country_of_origin"]
        self.assertEqual(field.value, "Испания")

    def test_name_country_beats_section_with_one_conflict(self):
        field = self._parse("Макароны 500г, Турция", ["Италия"]).fields["country_of_origin"]

        self.assertEqual(field.value, "Турция")
        self.assertEqual(field.source, "name")
        self.assertNotEqual(field.status, "ambiguous")
        self.assertEqual(len(field.conflicts), 1)


class CountrySourceOrderTests(SimpleTestCase):
    @staticmethod
    def _country(*pairs):
        return resolve_field(
            "country_of_origin", [FieldCandidate(value, source=source, status="inferred") for value, source in pairs]
        )

    def test_explicit_beats_advisory(self):
        parsed = self._country(("Россия", "name"), ("Италия", "gs1_prefix"))

        self.assertEqual(parsed.value, "Россия")
        self.assertEqual(parsed.source, "name")
        self.assertEqual(parsed.status, "inferred")
        self.assertEqual(len(parsed.conflicts), 1)
        self.assertEqual(parsed.conflicts[0]["code"], "conflicting_candidates")
        self.assertEqual(parsed.reliability, "requires_review")

    def test_explicit_beats_llm_without_blocking(self):
        parsed = self._country(("Россия", "column"), ("Италия", "llm"))

        self.assertEqual(parsed.value, "Россия")
        self.assertEqual(parsed.status, "inferred")
        self.assertEqual(len(parsed.conflicts), 1)
        self.assertEqual(parsed.reliability, "requires_review")

    def test_explicit_agreeing_with_advisory_is_clean(self):
        parsed = self._country(("Россия", "name"), ("Россия", "gs1_prefix"))

        self.assertEqual(parsed.value, "Россия")
        self.assertEqual(parsed.conflicts, [])
        self.assertEqual(parsed.reliability, "deterministic")

    def test_disagreeing_explicit_sources_stay_ambiguous(self):
        parsed = self._country(("Россия", "column"), ("Италия", "name"))

        self.assertEqual(parsed.source, "column")
        self.assertEqual(parsed.status, "ambiguous")
        self.assertEqual(parsed.reliability, "requires_review")
        self.assertEqual(len(parsed.conflicts), 1)

    def test_user_beats_disagreeing_explicit(self):
        parsed = self._country(("Италия", "user"), ("Россия", "column"))

        self.assertEqual(parsed.value, "Италия")
        self.assertEqual(parsed.status, "inferred")

    def test_ambiguous_column_value_keeps_its_status_next_to_advisory(self):
        parsed = resolve_field(
            "country_of_origin",
            [
                FieldCandidate("Россия, Краснодарский край", source="column"),
                FieldCandidate("Италия", source="gs1_prefix", status="inferred"),
            ],
        )

        self.assertEqual(parsed.source, "column")
        self.assertEqual(parsed.status, "ambiguous")
        self.assertEqual(parsed.reliability, "requires_review")

    def test_advisory_agreement(self):
        parsed = self._country(("Италия", "section"), ("Италия", "gs1_prefix"))

        self.assertEqual(parsed.value, "Италия")
        self.assertEqual(parsed.source, "section")
        self.assertEqual(parsed.status, "inferred")
        self.assertEqual(parsed.conflicts, [])
        self.assertEqual(parsed.reliability, "requires_review")

    def test_advisory_disagreement(self):
        parsed = self._country(("Италия", "section"), ("Испания", "gs1_prefix"))

        self.assertEqual(parsed.status, "ambiguous")
        self.assertIsNone(parsed.value)
        self.assertEqual([c.value for c in parsed.candidates], ["Италия", "Испания"])
        self.assertEqual(len(parsed.conflicts), 1)
        self.assertEqual(parsed.reliability, "requires_review")

    def test_two_values_of_the_same_advisory_source_disagree(self):
        parsed = self._country(("Италия", "gs1_prefix"), ("Испания", "gs1_prefix"))

        self.assertEqual(parsed.status, "ambiguous")
        self.assertIsNone(parsed.value)

    def test_source_order_among_advisory_sources(self):
        parsed = self._country(("Италия", "brand"), ("Италия", "catalog_barcode"), ("Италия", "section"))

        self.assertEqual(parsed.source, "section")

    def test_llm_winner_keeps_generic_disagreement_rule(self):
        parsed = resolve_field(
            "country_of_origin",
            [
                FieldCandidate("Италия", source="llm", evidence="packaging"),
                FieldCandidate("Испания", source="llm", evidence="packaging"),
            ],
        )

        self.assertEqual(parsed.status, "ambiguous")
        self.assertEqual(parsed.reliability, "requires_review")

    def test_other_fields_unchanged(self):
        parsed = resolve_field(
            "net_value",
            [
                FieldCandidate(Decimal("1"), source="name", status="inferred"),
                FieldCandidate(Decimal("2"), source="name", status="inferred"),
            ],
        )

        self.assertEqual(parsed.status, "ambiguous")
        self.assertEqual(parsed.value, Decimal("1"))
        self.assertEqual(parsed.reliability, "requires_review")


class AcceptIssuesTests(SimpleTestCase):
    @staticmethod
    def _snapshot(*codes):
        return {
            "fields": {
                "net_value": {"value": "5", "status": "ambiguous"},
                "country_of_origin": {"value": "Китай", "status": "approximate"},
                "price": {"value": None, "status": "invalid"},
            },
            "parse_issues": [
                {"field": field, "code": code, "message": code, "review_required": True}
                for field, code in zip(("net_value", "country_of_origin", "price"), codes, strict=False)
            ],
        }

    def test_accept_issues_resolves_warnings_keeps_values(self):
        snapshot = self._snapshot("ambiguous", "approximate_package")
        accepted = accept_issues(snapshot, at="2026-10-11T00:00:00Z", actor_id=7)
        self.assertEqual(effective_issues(accepted), [])
        self.assertEqual(accepted["fields"]["net_value"]["value"], "5")
        self.assertEqual(accepted["fields"]["country_of_origin"]["value"], "Китай")
        self.assertEqual({o["field"] for o in accepted["overrides"]}, {"net_value", "country_of_origin"})
        self.assertEqual(len(effective_issues(snapshot)), 2)

    def test_accept_issues_leaves_invalid_values_blocked(self):
        accepted = accept_issues(self._snapshot("ambiguous", "approximate_package", "invalid"))
        self.assertEqual([i["code"] for i in effective_issues(accepted)], ["invalid"])


class BatchEnrichmentTests(SimpleTestCase):
    @staticmethod
    def rows(count, name="Мука 25 кг"):
        return [
            parse_product_row(RawImportRow(n, {"name": name}, {"sheet_name": "Прайс"}), lambda key: key)
            for n in range(1, count + 1)
        ]

    def run_batch(self, parsed_rows, response=None, *, budget=10, batch_size=12, error=None):
        from unittest.mock import patch

        from django.test import override_settings

        from file_imports.llm_candidates import BatchCandidateResponse, enrich_candidates_batch

        with (
            override_settings(CANONICAL_IMPORT_LLM_ENABLED=True),
            patch("file_imports.llm_candidates.LLMConnector") as connector,
        ):
            structured = connector.return_value.structured
            if error:
                structured.side_effect = error
            else:
                structured.return_value = response or BatchCandidateResponse(rows=[])
            request_budget = [budget]
            enrich_candidates_batch(parsed_rows, request_budget=request_budget, batch_size=batch_size)
        return structured, request_budget

    def test_batch_uses_one_call_per_chunk(self):
        structured, budget = self.run_batch(self.rows(25), batch_size=12)
        self.assertEqual(structured.call_count, 3)
        self.assertEqual(budget[0], 7)
        payload = structured.call_args_list[0].kwargs["payload"]
        self.assertEqual(len(payload["rows"]), 12)
        self.assertEqual(set(payload["rows"][0]), {"row_number", "requested_fields", "cells"})

    def test_batch_respects_budget(self):
        rows = self.rows(25)
        beyond_before = [r.snapshot() for r in rows[24:]]
        structured, budget = self.run_batch(rows, budget=2, batch_size=12)
        self.assertEqual(structured.call_count, 2)
        self.assertEqual(budget[0], 0)
        self.assertEqual([r.snapshot() for r in rows[24:]], beyond_before)
        sent = {item["row_number"] for call in structured.call_args_list for item in call.kwargs["payload"]["rows"]}
        self.assertNotIn(25, sent)

    def test_prompt_demands_per_row_answers_for_batch_and_wrapper(self):
        from unittest.mock import patch

        from django.test import override_settings

        from file_imports.llm_candidates import enrich_candidates

        structured, _ = self.run_batch(self.rows(2))
        prompts = [structured.call_args.kwargs["system_prompt"]]
        with (
            override_settings(CANONICAL_IMPORT_LLM_ENABLED=True),
            patch("file_imports.llm_candidates.LLMConnector") as connector,
        ):
            enrich_candidates(self.rows(1)[0], request_budget=[1])
            prompts.append(connector.return_value.structured.call_args.kwargs["system_prompt"])
        for prompt in prompts:
            self.assertIn("row_number", prompt)
            self.assertIn("requested_fields", prompt)

    def test_batch_applies_suggestion_to_matching_row_number(self):
        from file_imports.llm_candidates import BatchCandidateResponse, RowCandidates, SuggestedField

        rows = self.rows(10, name="Гвозди 6 шт")
        response = BatchCandidateResponse(
            rows=[
                RowCandidates(
                    row_number=7,
                    candidates=[SuggestedField(field="pack_items_count", value="6", evidence="6 шт")],
                )
            ]
        )
        self.run_batch(rows, response)
        self.assertEqual([r.fields["pack_items_count"].value for r in rows if r.row_number != 7], [None] * 9)
        self.assertEqual(rows[6].fields["pack_items_count"].value, 6)
        self.assertIn("llm_value_review", [i["code"] for i in rows[6].parse_issues])

    def test_batch_failure_leaves_rows_unchanged(self):
        from api.llm import LLMConnectorError

        rows = self.rows(5)
        before = [r.snapshot() for r in rows]
        with self.assertLogs("autosnab.import.candidates", level="WARNING"):
            self.run_batch(rows, error=LLMConnectorError("boom"))
        self.assertEqual([r.snapshot() for r in rows], before)

    def test_answer_room_scales_with_chunk_size(self):
        structured, _ = self.run_batch(self.rows(25), batch_size=12)
        sizes = {len(c.kwargs["payload"]["rows"]): c.kwargs["max_tokens"] for c in structured.call_args_list}
        self.assertEqual(sizes[12], 12 * 250 + 300)
        self.assertEqual(sizes[1], 550)
        self.assertGreater(sizes[12], 1200)

    def test_chunks_are_requested_in_parallel_and_all_applied(self):
        import threading
        from unittest.mock import patch

        from django.test import override_settings

        from file_imports.llm_candidates import (
            BatchCandidateResponse,
            RowCandidates,
            SuggestedField,
            enrich_candidates_batch,
        )

        rows = self.rows(36, name="Гвозди 6 шт")
        meeting = threading.Barrier(3, timeout=5)  # only passes if three requests are in flight at once

        def answer(**kwargs):
            meeting.wait()
            number = kwargs["payload"]["rows"][0]["row_number"]
            return BatchCandidateResponse(
                rows=[
                    RowCandidates(
                        row_number=number,
                        candidates=[SuggestedField(field="pack_items_count", value="6", evidence="6 шт")],
                    )
                ]
            )

        with (
            override_settings(CANONICAL_IMPORT_LLM_ENABLED=True, CANONICAL_IMPORT_LLM_MAX_WORKERS=3),
            patch("file_imports.llm_candidates.LLMConnector") as connector,
        ):
            connector.return_value.structured.side_effect = answer
            budget = [10]
            enrich_candidates_batch(rows, request_budget=budget, batch_size=12)
        self.assertEqual(budget[0], 7)
        self.assertEqual([r.row_number for r in rows if r.fields["pack_items_count"].value == 6], [1, 13, 25])

    def test_one_failed_chunk_does_not_stop_the_others(self):
        from unittest.mock import patch

        from django.test import override_settings

        from api.llm import LLMConnectorError
        from file_imports.llm_candidates import (
            BatchCandidateResponse,
            RowCandidates,
            SuggestedField,
            enrich_candidates_batch,
        )

        rows = self.rows(24, name="Гвозди 6 шт")

        def answer(**kwargs):
            number = kwargs["payload"]["rows"][0]["row_number"]
            if number == 1:
                raise LLMConnectorError("truncated")
            return BatchCandidateResponse(
                rows=[
                    RowCandidates(
                        row_number=number,
                        candidates=[SuggestedField(field="pack_items_count", value="6", evidence="6 шт")],
                    )
                ]
            )

        with (
            override_settings(CANONICAL_IMPORT_LLM_ENABLED=True),
            patch("file_imports.llm_candidates.LLMConnector") as connector,
            self.assertLogs("autosnab.import.candidates", level="WARNING"),
        ):
            connector.return_value.structured.side_effect = answer
            enrich_candidates_batch(rows, request_budget=[5], batch_size=12)
        self.assertEqual([r.row_number for r in rows if r.fields["pack_items_count"].value == 6], [13])

    def test_rows_without_requested_fields_cost_no_calls(self):
        rows = self.rows(5, name="Авокадо, шт")
        structured, budget = self.run_batch(rows)
        self.assertEqual(structured.call_count, 0)
        self.assertEqual(budget[0], 10)


@override_settings(CANONICAL_IMPORT_COUNTRY_HINTS_ENABLED=True)
class BarcodeFieldTests(SimpleTestCase):
    @staticmethod
    def _parse(barcode, **cells):
        return parse_product_row(
            RawImportRow(9, {"name": "Бульон Роллтон 200г", "barcode": barcode, **cells}, {}), lambda key: key
        )

    def test_barcode_is_a_field_and_gives_a_gs1_country_hint(self):
        fields = self._parse("4605496029632").fields

        self.assertEqual(fields["barcode"].value, "4605496029632")
        self.assertEqual(fields["barcode"].status, "parsed")
        self.assertEqual(fields["barcode"].source, "column")
        self.assertEqual(fields["barcode"].source_column, "barcode")
        self.assertEqual(fields["barcode"].method, "barcode-column:ean13")
        country = fields["country_of_origin"]
        self.assertEqual(country.value, "Россия")
        self.assertEqual(country.source, "gs1_prefix")
        self.assertEqual(country.method, "gs1-prefix")
        self.assertEqual(country.status, "inferred")
        self.assertEqual(country.confidence, 0.5)
        self.assertEqual(country.evidence, "460")
        self.assertEqual(country.reliability, "requires_review")

    def test_excel_float_barcode_works(self):
        fields = self._parse(4605496029632.0).fields

        self.assertEqual(fields["barcode"].value, "4605496029632")
        self.assertEqual(fields["country_of_origin"].value, "Россия")

        fields = self._parse("4605496029632.0").fields
        self.assertEqual(fields["barcode"].value, "4605496029632")

    def test_barcode_with_wrong_check_digit_is_invalid_and_gives_no_country(self):
        fields = self._parse("4605496029633").fields

        self.assertEqual(fields["barcode"].status, "invalid")
        self.assertEqual(fields["country_of_origin"].status, "missing")
        self.assertIsNone(fields["country_of_origin"].value)

    def test_invalid_barcode_does_not_raise_a_review_issue(self):
        row = self._parse("4605496029633")

        self.assertFalse([i for i in row.parse_issues if i["field"] == "barcode"])

    def test_missing_barcode_column_gives_a_missing_field(self):
        row = parse_product_row(RawImportRow(9, {"name": "Бульон 200г"}, {}), lambda key: key)

        self.assertEqual(row.fields["barcode"].status, "missing")
        self.assertIsNone(row.fields["barcode"].value)

    def test_internal_prefix_gives_no_country(self):
        fields = self._parse("2000000000008").fields

        self.assertEqual(fields["barcode"].status, "parsed")
        self.assertEqual(fields["country_of_origin"].status, "missing")

    def test_codes_of_different_countries_make_the_country_ambiguous(self):
        fields = self._parse("4605496029632, 4810001000017").fields

        self.assertEqual(fields["country_of_origin"].status, "ambiguous")
        self.assertIsNone(fields["country_of_origin"].value)

    def test_codes_of_one_country_agree(self):
        fields = self._parse("4605496029632, 4605496020035").fields

        self.assertEqual(fields["country_of_origin"].value, "Россия")
        self.assertEqual(fields["country_of_origin"].status, "inferred")

    def test_gtin14_keeps_kind_and_uses_digits_two_to_four(self):
        fields = self._parse("14605496029639").fields

        self.assertEqual(fields["barcode"].value, "4605496029639")
        self.assertEqual(fields["barcode"].method, "barcode-column:gtin14")
        self.assertEqual(fields["barcode"].raw_value, "14605496029639")
        self.assertEqual(fields["country_of_origin"].value, "Россия")
        self.assertEqual(fields["country_of_origin"].evidence, "460")

    def test_column_country_beats_the_gs1_hint(self):
        fields = self._parse("4605496029632", country_of_origin="Китай").fields
        country = fields["country_of_origin"]

        self.assertEqual(country.value, "Китай")
        self.assertEqual(country.source, "column")
        self.assertEqual(country.reliability, "requires_review")
        self.assertIn("gs1_prefix", [c.source for c in country.candidates])
        self.assertEqual(len(country.conflicts), 1)


class CountryHintsFlagTests(SimpleTestCase):
    """CANONICAL_IMPORT_COUNTRY_HINTS_ENABLED is off by default: a section heading and a GS1 prefix give no country,
    while a country in the row, a country column and the barcode field itself keep working."""

    @staticmethod
    def _parse(cells, source=None):
        return parse_product_row(RawImportRow(5, cells, source or {}), lambda key: key)

    def test_default_flag_is_off(self):
        from django.conf import settings

        self.assertFalse(getattr(settings, "CANONICAL_IMPORT_COUNTRY_HINTS_ENABLED", False))

    def test_section_heading_gives_no_country_when_off(self):
        row = self._parse({"name": "Макароны 500г"}, {"section_path": ["Италия"]})

        self.assertIsNone(row.fields["country_of_origin"].value)
        self.assertEqual(row.fields["country_of_origin"].status, "missing")
        self.assertEqual(row.raw["source"]["section_path"], ["Италия"])

    def test_barcode_gives_no_country_when_off_but_is_still_parsed(self):
        fields = self._parse({"name": "Бульон Роллтон 200г", "barcode": "4605496029632"}).fields

        self.assertEqual(fields["barcode"].value, "4605496029632")
        self.assertEqual(fields["barcode"].status, "parsed")
        self.assertIsNone(fields["country_of_origin"].value)
        self.assertEqual(fields["country_of_origin"].candidates, [])

    def test_name_and_column_countries_work_when_off(self):
        in_name = self._parse(
            {"name": "Макароны 500г, Турция", "barcode": "4605496029632"}, {"section_path": ["Италия"]}
        )
        column = self._parse({"name": "Сыр 1 кг", "country_of_origin": "Голландия", "barcode": "4605496029632"})

        self.assertEqual(in_name.fields["country_of_origin"].value, "Турция")
        self.assertEqual(in_name.fields["country_of_origin"].conflicts, [])
        self.assertEqual(column.fields["country_of_origin"].value, "Нидерланды")
        self.assertEqual(column.fields["country_of_origin"].conflicts, [])

    @override_settings(CANONICAL_IMPORT_COUNTRY_HINTS_ENABLED=True)
    def test_flag_on_adds_the_hints(self):
        by_section = self._parse({"name": "Макароны 500г"}, {"section_path": ["Италия"]})
        by_barcode = self._parse({"name": "Бульон Роллтон 200г", "barcode": "4605496029632"})

        self.assertEqual(by_section.fields["country_of_origin"].source, "section")
        self.assertEqual(by_barcode.fields["country_of_origin"].source, "gs1_prefix")
