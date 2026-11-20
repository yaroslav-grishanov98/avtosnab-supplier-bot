"""
Tests for api/v1/suppliers/dedup.py.

Pure/algorithmic module — no database needed.
"""

from django.test import SimpleTestCase

from api.v1.suppliers.dedup import (
    DIFFERENT,
    POSSIBLE_DUPLICATE,
    SAME_BRANCH,
    SAME_COMPANY,
    CompanyCanonical,
    dedup_decision,
    normalize_text,
    similarity,
)


class NormalizeTextTests(SimpleTestCase):
    def test_strips_legal_form_and_punctuation(self):
        self.assertEqual(normalize_text('ООО "Ромашка"'), "РОМАШКА")

    def test_strips_various_legal_forms(self):
        self.assertEqual(normalize_text("Публичное акционерное общество Сбербанк"), "СБЕРБАНК")
        self.assertEqual(normalize_text("АО Рога и Копыта"), "РОГА И КОПЫТА")

    def test_normalizes_yo(self):
        self.assertEqual(normalize_text("Мёд"), "МЕД")

    def test_collapses_whitespace(self):
        self.assertEqual(normalize_text("ООО   Ромашка  "), "РОМАШКА")

    def test_none_returns_empty_string(self):
        self.assertEqual(normalize_text(None), "")


class SimilarityTests(SimpleTestCase):
    def test_identical_strings(self):
        self.assertEqual(similarity("РОМАШКА", "РОМАШКА"), 1.0)

    def test_empty_inputs_return_zero(self):
        self.assertEqual(similarity(None, "РОМАШКА"), 0.0)
        self.assertEqual(similarity("РОМАШКА", ""), 0.0)


class DedupDecisionTests(SimpleTestCase):
    def test_same_inn_and_kpp_is_same_branch(self):
        existing = CompanyCanonical(inn="7707083893", kpp="770701001", full_name="ООО Ромашка")
        incoming = CompanyCanonical(inn="7707083893", kpp="770701001", full_name="ООО Ромашка Филиал")
        label, confidence = dedup_decision(existing, incoming)
        self.assertEqual(label, SAME_BRANCH)
        self.assertGreaterEqual(confidence, 0.95)

    def test_same_inn_different_kpp_is_possible_duplicate(self):
        existing = CompanyCanonical(inn="7707083893", kpp="770701001")
        incoming = CompanyCanonical(inn="7707083893", kpp="997750001")
        label, confidence = dedup_decision(existing, incoming)
        self.assertEqual(label, POSSIBLE_DUPLICATE)

    def test_same_inn_missing_kpp_on_one_side_is_same_company(self):
        existing = CompanyCanonical(inn="7707083893", kpp="770701001")
        incoming = CompanyCanonical(inn="7707083893", kpp=None)
        label, confidence = dedup_decision(existing, incoming)
        self.assertEqual(label, SAME_COMPANY)

    def test_same_ogrn_is_same_company(self):
        existing = CompanyCanonical(ogrn="1027700132195", full_name="Компания А")
        incoming = CompanyCanonical(ogrn="1027700132195", full_name="Компания Б")
        label, confidence = dedup_decision(existing, incoming)
        self.assertEqual(label, SAME_COMPANY)
        self.assertGreaterEqual(confidence, 0.97)

    def test_no_identifiers_but_matching_name_and_address_is_possible_duplicate(self):
        existing = CompanyCanonical(full_name="ООО Ромашка", address="Москва, ул. Ленина, 1")
        incoming = CompanyCanonical(full_name="ООО Ромашка", address="г. Москва, улица Ленина, дом 1")
        label, confidence = dedup_decision(existing, incoming)
        self.assertEqual(label, POSSIBLE_DUPLICATE)
        self.assertGreaterEqual(confidence, 0.80)

    def test_matching_name_without_address_stays_below_threshold(self):
        # No address on either side means addr_score is always 0.0, capping
        # the weighted score at 0.65 (name_score * 0.65) — below the 0.80
        # possible_duplicate threshold. Matches the source document's model:
        # without at least one supporting signal, an exact name match alone
        # isn't enough to flag a duplicate.
        existing = CompanyCanonical(full_name="ООО Ромашка")
        incoming = CompanyCanonical(full_name="ООО Ромашка")
        label, confidence = dedup_decision(existing, incoming)
        self.assertEqual(label, DIFFERENT)

    def test_completely_different_companies(self):
        existing = CompanyCanonical(inn="7707083893", full_name="ООО Ромашка", address="Москва")
        incoming = CompanyCanonical(inn="7728168971", full_name="ООО Василёк", address="Новосибирск")
        label, confidence = dedup_decision(existing, incoming)
        self.assertEqual(label, DIFFERENT)

    def test_conflicting_status_reduces_confidence(self):
        matching_status = dedup_decision(
            CompanyCanonical(full_name="ООО Ромашка", address="Москва, ул. Ленина, 1", status="ACTIVE"),
            CompanyCanonical(full_name="ООО Ромашка", address="Москва, ул. Ленина, 1", status="ACTIVE"),
        )
        conflicting_status = dedup_decision(
            CompanyCanonical(full_name="ООО Ромашка", address="Москва, ул. Ленина, 1", status="ACTIVE"),
            CompanyCanonical(full_name="ООО Ромашка", address="Москва, ул. Ленина, 1", status="LIQUIDATED"),
        )
        self.assertGreater(matching_status[1], conflicting_status[1])
