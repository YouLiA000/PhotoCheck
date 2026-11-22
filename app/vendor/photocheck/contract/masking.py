"""Canonical masking implementation shared by contract, A, B, and the app."""

from __future__ import annotations

import re

PHONE_HEAD = 3
PHONE_TAIL = 4
PHONE_LEN = 11
IDCARD_HEAD = 4
IDCARD_TAIL = 4
IDCARD_LEN = 18
IDCARD_STARS = 10
PHONE_SHORT_KEEP_FROM = 8
IDCARD_SHORT_KEEP_FROM = 12
STAR = "*"


def _starred(head: str, tail: str, width: int) -> str:
    return f"{head}{STAR * width}{tail}"


def strip_country_code(value: str) -> str:
    """Remove ``+86`` / ``0086`` / 13-digit ``86`` before masking."""

    v = re.sub(r"[\s-]", "", (value or "").strip())
    if v.startswith("+86"):
        return v[3:]
    if v.startswith("0086"):
        return v[4:]
    if v.startswith("86") and len(v) == 13:
        return v[2:]
    return v


def mask_phone(value: str) -> str:
    """Mask phones while exposing at most three leading digits.

    Short OCR fragments retain at most two characters; inputs below eight
    characters are fully masked.
    """

    value = strip_country_code(value)
    if not value:
        return ""
    if len(value) == PHONE_LEN:
        return _starred(value[:PHONE_HEAD], value[-PHONE_TAIL:], 4)
    if len(value) < PHONE_LEN:
        if len(value) >= PHONE_SHORT_KEEP_FROM:
            return value[:2] + STAR * (len(value) - 2)
        return STAR * len(value)
    return _starred(value[:PHONE_HEAD], value[-PHONE_TAIL:], 4)


def mask_idcard(value: str) -> str:
    """Mask ID numbers while never exposing more than four leading digits."""

    value = (value or "").strip()
    if not value:
        return ""
    if len(value) == IDCARD_LEN:
        return _starred(value[:IDCARD_HEAD], value[-IDCARD_TAIL:].upper(), IDCARD_STARS)
    if len(value) < IDCARD_LEN:
        if len(value) >= IDCARD_SHORT_KEEP_FROM:
            return value[:2] + STAR * (len(value) - 2)
        return STAR * len(value)
    return _starred(value[:IDCARD_HEAD], value[-IDCARD_TAIL:].upper(), IDCARD_STARS)


def mask_summary(kind: str, subtype: str) -> str:
    """Return a non-sensitive category summary for non-text findings."""

    return f"{kind}/{subtype}"


def mask_for(kind: str, subtype: str, value: str | None = None) -> str:
    """Route masking by record kind and subtype."""

    if kind == "text" and subtype == "phone":
        return mask_phone(value or "")
    if kind == "text" and subtype == "idcard":
        return mask_idcard(value or "")
    return mask_summary(kind, subtype)
