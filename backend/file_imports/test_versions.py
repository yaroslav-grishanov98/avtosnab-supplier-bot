from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from file_imports import versions
from file_imports.canonical import PARSER_VERSION


class ParserVersionRegistryTests(SimpleTestCase):
    def test_current_canonical_version_is_supported(self):
        self.assertIn(versions.CANONICAL_V2, versions.CANONICAL_VERSIONS)
        self.assertEqual(PARSER_VERSION, versions.CANONICAL_V2)

    def test_is_canonical_recognises_supported_versions_only(self):
        self.assertTrue(versions.is_canonical(versions.CANONICAL_V2))
        self.assertFalse(versions.is_canonical(versions.LEGACY_V1))
        self.assertFalse(versions.is_canonical(""))
        self.assertFalse(versions.is_canonical(None))
        self.assertFalse(versions.is_canonical("canonical-products-v0"))

    def test_parser_version_for_maps_the_flag_to_a_version(self):
        self.assertEqual(versions.parser_version_for(True), versions.CANONICAL_V2)
        self.assertEqual(versions.parser_version_for(False), versions.LEGACY_V1)

    def test_newly_registered_version_is_treated_as_canonical(self):
        with mock.patch.object(versions, "CANONICAL_VERSIONS", versions.CANONICAL_VERSIONS | {"canonical-products-v3"}):
            self.assertTrue(versions.is_canonical("canonical-products-v3"))
            self.assertTrue(versions.is_canonical(versions.CANONICAL_V2))

    def test_no_hardcoded_parser_version_literals_outside_registry(self):
        # A literal comparison silently switches Master/Venue to legacy parsing once a new
        # version exists; every consumer must go through file_imports.versions instead.
        root = Path(__file__).resolve().parent.parent
        skipped_parts = {"migrations", "var", ".venv", "node_modules", "project_raw", "scripts"}
        offenders = []
        for path in root.rglob("*.py"):
            relative = path.relative_to(root)
            if skipped_parts & set(relative.parts) or path.name.startswith("test") or path.name == "versions.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "canonical-products-v" in text or '"legacy-v1"' in text:
                offenders.append(str(relative))
        self.assertEqual(offenders, [])

    def test_version_literals_are_stable(self):
        # Stored in sessions and snapshots; changing them breaks in-flight imports.
        self.assertEqual(versions.CANONICAL_V2, "canonical-products-v2")
        self.assertEqual(versions.LEGACY_V1, "legacy-v1")
