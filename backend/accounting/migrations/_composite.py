"""Composite foreign keys for accounting migrations.

Django 5.1 cannot declare a foreign key over several columns, so these are
plain RunSQL and the model state knows nothing about them: change them only
with a new hand-written migration and an isolation test (docs/adr/0020).
The module name starts with "_" so the migration loader skips it.
"""

import re
from collections.abc import Sequence

from django.db import migrations

# PostgreSQL silently truncates identifiers longer than 63 bytes.
MAX_IDENTIFIER_LENGTH = 63
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def _identifier(value: str) -> str:
    if not _IDENTIFIER.match(value) or len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"Invalid SQL identifier: {value!r}")
    return value


def composite_fk(
    table: str, name: str, columns: Sequence[str], ref_table: str, ref_columns: Sequence[str]
) -> migrations.RunSQL:
    """FOREIGN KEY (columns) REFERENCES ref_table (ref_columns) ON DELETE RESTRICT, reversible.

    The default MATCH SIMPLE is intended: a row with a NULL in any key column
    (a warehouse or department without a venue yet) is not checked. The key is
    not deferrable, so a mismatching row fails on its own INSERT/UPDATE.
    ref_columns need a unique constraint over exactly these columns.
    """
    if not columns or len(columns) != len(ref_columns):
        raise ValueError("columns and ref_columns must be non-empty and of the same length")
    cols = ", ".join(_identifier(column) for column in columns)
    ref_cols = ", ".join(_identifier(column) for column in ref_columns)
    table, name, ref_table = _identifier(table), _identifier(name), _identifier(ref_table)
    return migrations.RunSQL(
        sql=(
            f"ALTER TABLE {table} ADD CONSTRAINT {name} "
            f"FOREIGN KEY ({cols}) REFERENCES {ref_table} ({ref_cols}) ON DELETE RESTRICT;"
        ),
        reverse_sql=f"ALTER TABLE {table} DROP CONSTRAINT {name};",
    )
