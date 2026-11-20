"""Общий File Import Layer (unified-import-architecture spec, docs/superpowers/specs/
2026-09-29-unified-import-architecture.md, §3.1) -- домен-независимое извлечение
raw-строк из файлов прайсов/каталогов (CSV/XLSX/XLSM/PDF/DOCX/TXT/image) для
Мастер-справочника, Справочника заведения и Справочника поставщика.

Парсер не принимает бизнес-решений (не решает create/replace/skip) -- только
извлекает сырые ячейки. Это делает domain adapter после preview."""
