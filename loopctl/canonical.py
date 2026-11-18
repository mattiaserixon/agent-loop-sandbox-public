"""Canonical JSON and the config hash, identical to the UI page's JavaScript (phase2-design §12.3, proof test T-HASH).

The page computes `canon(v)`: arrays and objects recursively, object keys sorted with JavaScript's default `sort()`
(UTF-16 code units), every key and primitive through `JSON.stringify`. `config_hash` is the sha256 of that text in
UTF-8, as lowercase hex. Python's `json.dumps` differs from `JSON.stringify` on numbers (`1.0`, `1e-07`, `1e+16`)
and on key order for characters outside the Basic Multilingual Plane, so this module formats both itself.

Values that the two languages can't agree on are refused rather than approximated: NaN and infinities, integers
beyond ±(2**53 - 1), strings with unpaired surrogates, and non-string keys.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1


class CanonicalError(ValueError):
    """The value has no single canonical form shared with the page."""


def _number(value: int | float) -> str:
    if isinstance(value, bool):  # bool is an int subclass; handled by the caller
        raise CanonicalError("internal: bool reached _number")
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise CanonicalError(f"integer {value} is outside JavaScript's safe range")
        return str(value)
    if math.isnan(value) or math.isinf(value):
        raise CanonicalError(f"{value!r} has no JSON form")
    if value == 0:
        return "0"  # JavaScript prints -0 as "0"
    # Always start from the shortest round-trip digits, as JavaScript does. (Not str(int(value)): above 2**53 that
    # prints exact digits, 55070455020920952, where JavaScript prints 55070455020920950.)
    text = repr(value)
    mantissa, _, exp_text = text.partition("e")
    if not exp_text:
        return text[:-2] if text.endswith(".0") else text
    exp = int(exp_text)
    if exp >= 21 or exp <= -7:
        return f"{mantissa}e{'+' if exp > 0 else '-'}{abs(exp)}"
    # JavaScript writes plain decimals for 1e-7 < |x| < 1e21; Python's repr used an exponent.
    plain = format(Decimal(text), "f")
    if "." in plain:
        plain = plain.rstrip("0").rstrip(".")
    return plain


def _string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalError("string with an unpaired surrogate has no single JSON form") from exc
    return json.dumps(value, ensure_ascii=False)


def _utf16_key(key: str) -> bytes:
    return key.encode("utf-16-be")  # byte order of UTF-16BE = JavaScript's code-unit order


def canonical(value: Any) -> str:
    """The canonical JSON text of `value` (dicts, lists/tuples, str, int, float, bool, None)."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise CanonicalError(f"object key {key!r} is not a string")
        keys = sorted(value, key=_utf16_key)
        return "{" + ",".join(_string(k) + ":" + canonical(value[k]) for k in keys) + "}"
    raise CanonicalError(f"{type(value).__name__} has no canonical JSON form")


def config_hash(config: Any) -> str:
    """sha256 of the canonical JSON, lowercase hex: what every tick reports and the page predicts."""
    return hashlib.sha256(canonical(config).encode("utf-8")).hexdigest()
