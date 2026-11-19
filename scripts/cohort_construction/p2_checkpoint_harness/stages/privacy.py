"""Conservative direct-identifier de-identification for Chinese clinical text.

This is not a complete HIPAA Safe Harbor implementation. Ages, sex, exact dates,
and clinical measurements are intentionally retained; temporal masking is separate.
"""
from __future__ import annotations

import re


NAME_REPLACEMENT = "[姓名已去标识化]"
PHONE_REPLACEMENT = "[电话已去标识化]"
EMAIL_REPLACEMENT = "[邮箱已去标识化]"
ADDRESS_REPLACEMENT = "[地址已去标识化]"
IDENTIFIER_REPLACEMENT = "[就诊或证件标识已去标识化]"

_FIELD_LABELS = (
    r"(?:患者姓名|病人姓名|姓名|性别|年龄|联系电话|联系手机|手机号码|手机号|手机|"
    r"电话|联系方式|邮箱|电子邮箱|家庭住址|家庭地址|现住址|居住地址|通讯地址|"
    r"联系地址|住址|地址|住院号|门诊号|身份证号)"
)
_FIELD_END = (
    r"(?=[ \t]*(?:$|[,，;；。\r\n]|" + _FIELD_LABELS + r"[ \t]*[:：]?|"
    r"(?:男|女)(?=[ \t]*(?:$|[,，;；。\r\n]))))"
)

_NAME = re.compile(
    r"(?P<prefix>(?:患者姓名|病人姓名|姓名)[ \t]*[:：]?[ \t]*)"
    r"(?P<value>(?:[A-Za-z]+(?:[ .'\-]+[A-Za-z]+){0,2}|"
    r"[\u4e00-\u9fff]{1,4}(?:[·•][\u4e00-\u9fff]{1,8})?))"
    + _FIELD_END,
    re.MULTILINE,
)
_IDENTIFIER = re.compile(
    r"(?P<prefix>(?:住院号|门诊号|身份证号)[ \t]*[:：]?[ \t]*)"
    r"(?P<value>[A-Za-z0-9][A-Za-z0-9_\-/]{2,31})" + _FIELD_END,
    re.MULTILINE,
)
_ADDRESS = re.compile(
    r"(?P<prefix>(?:家庭住址|家庭地址|现住址|居住地址|通讯地址|联系地址|住址|地址)"
    r"[ \t]*[:：]?[ \t]*)"
    r"(?P<value>[\u4e00-\u9fffA-Za-z0-9#＃_\-—()（）\t ]+?)" + _FIELD_END,
    re.MULTILINE,
)
_LABELED_PHONE = re.compile(
    r"(?P<prefix>(?:联系电话|联系手机|手机号码|手机号|手机|电话|联系方式)"
    r"[ \t]*[:：]?[ \t]*)"
    r"(?P<value>(?:\+?86[- ]?)?(?:1[3-9]\d[- ]?\d{4}[- ]?\d{4}|"
    r"0\d{2,3}[- ]?\d{7,8}(?:[-转]\d{1,6})?|\d{7,8}))(?!\d)"
)
_MOBILE = re.compile(
    r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d[- ]?\d{4}[- ]?\d{4}(?!\d)"
)
_LANDLINE = re.compile(
    r"(?<!\d)0\d{2,3}[- ]?\d{7,8}(?:[-转]\d{1,6})?(?!\d)"
)
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@"
    r"[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?![A-Za-z0-9._%+\-])"
)
_AGE_OVER_89 = re.compile(r"(?<![\d≥.])(?:9\d|[1-9]\d{2,})\s*岁")
_CLINICAL_NUMBER_BEFORE_MOBILE = re.compile(
    r"(?:指标|检验值|检查结果|测定值|检测值|结果|计数|数值)\s*[:：=]?\s*$")

_VALUE_PATTERNS = {
    "name": (_NAME, NAME_REPLACEMENT),
    "identifier": (_IDENTIFIER, IDENTIFIER_REPLACEMENT),
    "address": (_ADDRESS, ADDRESS_REPLACEMENT),
    "phone": (_LABELED_PHONE, PHONE_REPLACEMENT),
}


def _replace_labeled(match, replacement):
    return match.group("prefix") + replacement


def _literal_values(patient):
    values = {"name": set(), "address": set(), "phone": set(), "identifier": set()}
    if not isinstance(patient, dict):
        return values
    for name in patient.get("name") or []:
        if not isinstance(name, dict):
            continue
        family = name.get("family") if isinstance(name.get("family"), str) else ""
        given = " ".join(part for part in (name.get("given") or [])
                         if isinstance(part, str))
        joined = family + given
        variants = (name.get("text"), joined, (family + " " + given).strip(),
                    (given + " " + family).strip())
        for value in variants:
            if isinstance(value, str) and len(value.strip()) >= 2:
                values["name"].add(value.strip())
    for address in patient.get("address") or []:
        if not isinstance(address, dict):
            continue
        candidates = [address.get("text"), *(address.get("line") or [])]
        joined = "".join(str(part) for key in
                         ("line", "city", "district", "state", "postalCode", "country")
                         for part in (address.get(key) if isinstance(address.get(key), list)
                                      else [address.get(key)]) if part)
        if isinstance(joined, str):
            candidates.append(joined)
        for value in candidates:
            if isinstance(value, str) and len(value.strip()) >= 4:
                values["address"].add(value.strip())
    for contact in patient.get("telecom") or []:
        if isinstance(contact, dict) and isinstance(contact.get("value"), str):
            values["phone"].add(contact["value"].strip())
    for identifier in patient.get("identifier") or []:
        if isinstance(identifier, dict) and isinstance(identifier.get("value"), str):
            value = identifier["value"].strip()
            if len(value) >= 6 and not value.isdigit():
                values["identifier"].add(value)
    return values


def _collect_labeled(values, text):
    for category, (pattern, _) in _VALUE_PATTERNS.items():
        for match in pattern.finditer(text or ""):
            value = match.group("value").strip()
            minimum = 4 if category == "address" else 6 if category == "identifier" else 2
            if len(value) >= minimum and not (category == "identifier" and value.isdigit()):
                values[category].add(value)


def collect_identifier_values(sources, patient=None):
    values = _literal_values(patient)
    key_categories = {"患者姓名": "name", "病人姓名": "name", "姓名": "name",
                      "家庭住址": "address", "家庭地址": "address", "现住址": "address",
                      "住址": "address", "地址": "address", "住院号": "identifier",
                      "门诊号": "identifier", "身份证号": "identifier"}
    for source in sources:
        _collect_labeled(values, source.get("text"))
        _collect_labeled(values, source.get("title"))
        def visit(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in key_categories and isinstance(item, str):
                        category = key_categories[key]
                        minimum = 4 if category == "address" else 6 if category == "identifier" else 2
                        if len(item.strip()) >= minimum and not (
                                category == "identifier" and item.strip().isdigit()):
                            values[category].add(item.strip())
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
        visit(source.get("original_fields"))
        visit(source.get("provenance"))
    if isinstance(patient, dict):
        structured_numeric = {
            item.get("value").strip()
            for item in patient.get("identifier") or []
            if isinstance(item, dict) and isinstance(item.get("value"), str)
            and len(item.get("value").strip()) >= 6
            and item.get("value").strip().isdigit()
        }
        labeled_numeric = {
            match.group("value").strip()
            for source in sources
            for text in (source.get("text"), source.get("title"))
            for match in _IDENTIFIER.finditer(text or "")
            if match.group("value").strip().isdigit()
        }
        values["identifier"].update(structured_numeric & labeled_numeric)
    return values


def _replace_unlabeled_mobile(match):
    prefix = match.string[max(0, match.start() - 30):match.start()]
    if _CLINICAL_NUMBER_BEFORE_MOBILE.search(prefix):
        return match.group(0)
    return PHONE_REPLACEMENT


def deidentify_text(text, identifier_values=None) -> str:
    """Replace explicitly labeled names/addresses and recognizable contact details.

    Bare Chinese words and unknown field names are never treated as identifiers.
    Exact dates, ages, sex, and unrelated clinical numbers are left unchanged.
    """
    if not isinstance(text, str):
        raise TypeError("deidentify_text expects str")
    text = _EMAIL.sub(EMAIL_REPLACEMENT, text)
    text = _NAME.sub(lambda match: _replace_labeled(match, NAME_REPLACEMENT), text)
    text = _IDENTIFIER.sub(
        lambda match: _replace_labeled(match, IDENTIFIER_REPLACEMENT), text)
    text = _ADDRESS.sub(lambda match: _replace_labeled(match, ADDRESS_REPLACEMENT), text)
    text = _LABELED_PHONE.sub(
        lambda match: _replace_labeled(match, PHONE_REPLACEMENT), text)
    text = _MOBILE.sub(_replace_unlabeled_mobile, text)
    text = _LANDLINE.sub(PHONE_REPLACEMENT, text)
    for category, replacement in (("name", NAME_REPLACEMENT),
                                  ("address", ADDRESS_REPLACEMENT),
                                  ("phone", PHONE_REPLACEMENT),
                                  ("identifier", IDENTIFIER_REPLACEMENT)):
        for value in sorted((identifier_values or {}).get(category) or (), key=len,
                            reverse=True):
            if value:
                if category in {"phone", "identifier"}:
                    text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(value)
                                  + r"(?![A-Za-z0-9])", lambda _: replacement, text)
                else:
                    text = text.replace(value, replacement)
    return _AGE_OVER_89.sub("≥90岁", text)


def deidentify_value(value, identifier_values=None):
    """Recursively de-identify string values in dicts/lists without using key names."""
    if isinstance(value, str):
        return deidentify_text(value, identifier_values)
    if isinstance(value, dict):
        return {key: deidentify_value(item, identifier_values) for key, item in value.items()}
    if isinstance(value, list):
        return [deidentify_value(item, identifier_values) for item in value]
    return value
