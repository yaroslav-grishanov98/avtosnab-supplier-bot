"""Replace rows take the fields the file does not give from the card they replace.

A replace row is an update of an existing card. The fields below are mandatory for a new card, but the card already has
them, so the row must not wait for them and the commit must not overwrite them with a bulk "fill for all" value. A value
copied from the card is marked `field_sources[field] == "target"`; the commit leaves such fields alone, and they are
dropped again when the row stops being a replace of that card.

A country only guessed from a section heading or a barcode's GS1 prefix (`ADVISORY_SOURCES`) is a hint, not data: for a
replace row it counts as empty and the card's country is taken instead. A country written in the row (`name`) is
explicit and still overwrites the card."""

INHERITED_FIELDS = ("country_of_origin", "min_order_qty", "delivery_multiple", "availability")
TARGET_SOURCE = "target"
ADVISORY_SOURCES = frozenset({"section", "gs1_prefix"})


def _empty(value):
    return value is None or value == ""


def inherit_target_fields(item, target):
    """Fill the row's empty inherited fields from `target`. Returns True when something changed. Values the file or the
    user gave are never touched (an advisory guess counts as empty); a field the card itself lacks stays as it is."""
    changed = False
    sources = dict(item.field_sources or {})
    for field in INHERITED_FIELDS:
        value = getattr(target, field, None)
        fillable = _empty(getattr(item, field)) or sources.get(field) in ADVISORY_SOURCES
        if fillable and not _empty(value):
            setattr(item, field, value)
            sources[field] = TARGET_SOURCE
            changed = True
    if changed:
        item.field_sources = sources
    return changed


def drop_inherited_fields(item):
    """Undo `inherit_target_fields`: the row is no longer a replace of that card. Returns True when something changed."""
    sources = dict(item.field_sources or {})
    dropped = [field for field in INHERITED_FIELDS if sources.get(field) == TARGET_SOURCE]
    for field in dropped:
        setattr(item, field, None)
        del sources[field]
    if dropped:
        item.field_sources = sources
    return bool(dropped)


def inherited_fields_for_write(item):
    """Fields of a replace row that came from the card and must not be written back to it."""
    return [field for field in INHERITED_FIELDS if (item.field_sources or {}).get(field) == TARGET_SOURCE]
