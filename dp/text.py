"""Script and diacritic normalization for Serbian text.

The original text is always kept; these helpers produce the search forms:
Latin script, and Latin without diacritics (lowercased).
"""

import re
import unicodedata

_CYR_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ђ": "đ", "е": "e", "ж": "ž",
    "з": "z", "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj", "м": "m", "н": "n",
    "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "ћ": "ć", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "č", "џ": "dž", "ш": "š",
}  # fmt: skip

_ASCII_FOLD = {"đ": "dj", "Đ": "Dj", "ž": "z", "Ž": "Z", "č": "c", "Č": "C",
               "ć": "c", "Ć": "C", "š": "s", "Š": "S"}  # fmt: skip

_WS = re.compile(r"\s+")


def to_latin(text: str) -> str:
    """Transliterate Serbian Cyrillic to Latin, preserving case."""
    out = []
    for ch in text:
        lat = _CYR_TO_LAT.get(ch.lower())
        if lat is None:
            out.append(ch)
        elif ch.isupper():
            # Digraph capitalization: Љ -> Lj (title case is right for most contexts).
            out.append(lat[0].upper() + lat[1:])
        else:
            out.append(lat)
    return "".join(out)


def fold(text: str) -> str:
    """Search form: Latin, no diacritics, lowercase, collapsed whitespace."""
    # Strip combining marks first, so accented Cyrillic (ѐ, ѝ) transliterates too.
    bare = "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )
    lat = "".join(_ASCII_FOLD.get(ch, ch) for ch in to_latin(bare))
    return _WS.sub(" ", lat).strip().lower()
