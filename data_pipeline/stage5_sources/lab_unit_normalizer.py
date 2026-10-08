"""Conservative laboratory unit normalization."""

from __future__ import annotations

import re
from typing import Any


UNIT_NORMALIZER_VERSION = "lab_unit_normalizer_v1"
_UNIT_ALIASES = {
    "u/l": "U/L",
    "mg/dl": "mg/dL",
    "g/l": "g/L",
    "mmol/l": "mmol/L",
    "miu/l": "mIU/L",
    "iu/l": "IU/L",
    "ng/ml": "ng/mL",
    "pg/ml": "pg/mL",
    "μmol/l": "μmol/L",
    "µmol/l": "μmol/L",
    "μg/l": "μg/L",
    "µg/l": "μg/L",
    "%": "%",
}


def _lookup_key(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("Μ", "μ").lower()


def normalize_unit(value: Any) -> dict[str, Any]:
    unit_raw = value
    if value is None or not str(value).strip():
        return {
            "unit_raw": unit_raw,
            "unit_normalized": None,
            "unit_mapping_status": "EMPTY",
        }
    text = str(value).strip()
    normalized = _UNIT_ALIASES.get(_lookup_key(text))
    if normalized is None:
        return {
            "unit_raw": unit_raw,
            "unit_normalized": None,
            "unit_mapping_status": "UNKNOWN_UNCHANGED",
        }
    return {
        "unit_raw": unit_raw,
        "unit_normalized": normalized,
        "unit_mapping_status": "UNCHANGED_CONFIRMED" if text == normalized else "NORMALIZED_CONFIRMED",
    }
