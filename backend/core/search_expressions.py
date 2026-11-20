from django.db.models import Func, TextField, Value

_LATIN = "abcdefghijklmnopqrstuvwxyz"
_RUSSIAN = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
# A compact search key, not a user-facing transliteration. pg_trgm recognises
# these ASCII letters/digits even with LC_CTYPE=C; ё and е deliberately agree.
_ENCODED = "abcdeffghijklmnopqrstuvwxyz012345"
_SOURCE = _LATIN.upper() + _RUSSIAN.upper() + _RUSSIAN
_TARGET = _LATIN + _ENCODED + _ENCODED
_TABLE = str.maketrans(_SOURCE, _TARGET)


def search_name_key(text: str) -> str:
    return text.translate(_TABLE)


class SearchNameKey(Func):
    function = "TRANSLATE"
    output_field = TextField()

    def __init__(self, expression):
        super().__init__(expression, Value(_SOURCE), Value(_TARGET))
