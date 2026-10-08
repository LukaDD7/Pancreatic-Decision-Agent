"""Rule-only parsing for laboratory reference expressions."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any


REFERENCE_TYPES = (
    "NUMERIC_INTERVAL",
    "UPPER_BOUND",
    "LOWER_BOUND",
    "QUALITATIVE_REFERENCE",
    "MULTI_ZONE_REFERENCE",
    "METHOD_TEXT",
    "EMPTY",
    "UNPARSED",
)
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_INTERVAL_RE = re.compile(rf"^\s*([\[\(])?\s*({_NUMBER})\s*(?:-|–|—|~|～|至)\s*({_NUMBER})\s*([\]\)])?\s*$")
_UPPER_RE = re.compile(rf"^\s*(<=|<|≤|小于|低于)\s*({_NUMBER})\s*$")
_LOWER_RE = re.compile(rf"^\s*(>=|>|≥|大于|高于)\s*({_NUMBER})\s*$")
_ZONE_RE = re.compile(r"(?P<label>建议复查|灰区|阴性|阳性|弱阴性|弱阳性|正常|异常)\s*[:：=]\s*(?P<rule>[^,，;；]+)")
_ZONE_LABEL_RE = re.compile(r"建议复查|灰区|阴性|阳性|弱阴性|弱阳性|正常|异常")
_QUALITATIVE_RE = re.compile(r"^(?:阴性|阳性|弱阴性|弱阳性|正常|异常|未见|未检出)(?:\s*[/、,，]\s*(?:阴性|阳性|弱阴性|弱阳性|正常|异常|未见|未检出))*$")
_METHOD_MARKERS = ("方法", "试剂", "实验室", "参考范围不同", "以说明书", "以试剂盒")


def _number(value: str) -> float:
    try:
        return float(Decimal(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(value) from exc


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def parse_reference(value: Any) -> dict[str, Any]:
    reference_raw = value
    text = "" if value is None else str(value).strip()
    result = {
        "reference_raw": reference_raw,
        "reference_type": "EMPTY",
        "reference_rule_json": None,
        "reference_parse_status": "EMPTY",
    }
    if not text:
        return result

    zone_matches = list(_ZONE_RE.finditer(text))
    if len(zone_matches) >= 2:
        zones = [
            {"label": match.group("label"), "rule": match.group("rule").strip()}
            for match in zone_matches
        ]
        result.update(
            reference_type="MULTI_ZONE_REFERENCE",
            reference_rule_json=_json({"zones": zones}),
            reference_parse_status="PARSED",
        )
        return result
    labels = _ZONE_LABEL_RE.findall(text)
    if len(labels) >= 2 and any(separator in text for separator in (";", "；", ",", "，", "/", "、")):
        result.update(
            reference_type="MULTI_ZONE_REFERENCE",
            reference_rule_json=_json({"zones": [{"label": label, "rule": None} for label in labels]}),
            reference_parse_status="PARSED",
        )
        return result

    interval = _INTERVAL_RE.fullmatch(text)
    if interval:
        left, low, high, right = interval.groups()
        result.update(
            reference_type="NUMERIC_INTERVAL",
            reference_rule_json=_json(
                {
                    "low": _number(low),
                    "high": _number(high),
                    "low_inclusive": left != "(",
                    "high_inclusive": right != ")",
                }
            ),
            reference_parse_status="PARSED",
        )
        return result

    upper = _UPPER_RE.fullmatch(text)
    if upper:
        result.update(
            reference_type="UPPER_BOUND",
            reference_rule_json=_json({"operator": upper.group(1), "value": _number(upper.group(2))}),
            reference_parse_status="PARSED",
        )
        return result

    lower = _LOWER_RE.fullmatch(text)
    if lower:
        result.update(
            reference_type="LOWER_BOUND",
            reference_rule_json=_json({"operator": lower.group(1), "value": _number(lower.group(2))}),
            reference_parse_status="PARSED",
        )
        return result

    if _QUALITATIVE_RE.fullmatch(text):
        result.update(
            reference_type="QUALITATIVE_REFERENCE",
            reference_rule_json=_json({"values": re.split(r"\s*[/、,，]\s*", text)}),
            reference_parse_status="PARSED",
        )
        return result

    if any(marker in text for marker in _METHOD_MARKERS):
        result.update(
            reference_type="METHOD_TEXT",
            reference_rule_json=_json({"text": text}),
            reference_parse_status="PARSED",
        )
        return result

    result.update(
        reference_type="UNPARSED",
        reference_rule_json=_json({"raw": text}),
        reference_parse_status="UNPARSED",
    )
    return result
