"""Canonical JSON and digests.

Every hash in this package (step keys, argument digests, the audit chain) is
taken over the same canonical encoding, so two dicts that are equal produce
the same bytes regardless of key order.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
