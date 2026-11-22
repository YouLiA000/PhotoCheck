"""Compatibility import for the canonical masking implementation.

The implementation lives in ``photocheck.contract.masking`` so the same file is
available to the integrated runtime and the portable application. Contract
callers keep the stable ``contract.masking`` import path.
"""

from photocheck.contract.masking import (
    IDCARD_HEAD,
    IDCARD_LEN,
    IDCARD_SHORT_KEEP_FROM,
    IDCARD_STARS,
    IDCARD_TAIL,
    PHONE_HEAD,
    PHONE_LEN,
    PHONE_SHORT_KEEP_FROM,
    PHONE_TAIL,
    STAR,
    mask_for,
    mask_idcard,
    mask_phone,
    mask_summary,
    strip_country_code,
)

__all__ = [
    "mask_for",
    "mask_idcard",
    "mask_phone",
    "mask_summary",
    "strip_country_code",
]
