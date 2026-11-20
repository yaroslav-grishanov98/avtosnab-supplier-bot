from dataclasses import dataclass, field


@dataclass(frozen=True)
class RawImportRow:
    """Одна извлечённая строка файла -- чистые raw-ячейки, без домена, без
    бизнес-интерпретации (unified-import-architecture spec §3.1)."""

    row_number: int
    cells: dict[str, str]
    source: dict = field(default_factory=dict)


class ExtractedRows(list):
    """List-compatible raw rows with file-level diagnostics and provenance."""

    def __init__(self, rows=(), *, metadata=None):
        super().__init__(rows)
        self.metadata = metadata or {}
