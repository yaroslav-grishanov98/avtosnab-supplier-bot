"""Registry of import parser versions stored on sessions and parsed snapshots.

Sessions pin the version they were created with, so a session started on an older
version keeps being processed by the same rules after a newer one is introduced.
"""

CANONICAL_V2 = "canonical-products-v2"
LEGACY_V1 = "legacy-v1"

# Versions handled by the canonical parser. Anything else falls back to legacy parsing.
CANONICAL_VERSIONS = frozenset({CANONICAL_V2})


def is_canonical(version):
    return version in CANONICAL_VERSIONS


def parser_version_for(canonical_enabled):
    return CANONICAL_V2 if canonical_enabled else LEGACY_V1
