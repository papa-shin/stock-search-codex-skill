from __future__ import annotations

import re
from datetime import datetime


MAX_PUBLIC_TEXT_CODEPOINTS = 256
_ISO_8601_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)


def is_bounded_unicode_scalar(
    value: object,
    *,
    maximum: int = MAX_PUBLIC_TEXT_CODEPOINTS,
    allow_empty: bool = False,
) -> bool:
    if not isinstance(value, str):
        return False
    if (not allow_empty and not value) or len(value) > maximum:
        return False
    return not any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def is_iso_8601_timestamp(value: object) -> bool:
    if not is_bounded_unicode_scalar(value):
        return False
    assert isinstance(value, str)
    if _ISO_8601_TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None
