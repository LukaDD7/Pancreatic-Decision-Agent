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


def _replace_labeled(match, replacement):
    return match.group("prefix") + replacement


def deidentify_text(text) -> str:
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
    text = _MOBILE.sub(PHONE_REPLACEMENT, text)
    text = _LANDLINE.sub(PHONE_REPLACEMENT, text)
    return _AGE_OVER_89.sub("≥90岁", text)


def deidentify_value(value):
    """Recursively de-identify string values in dicts/lists without using key names."""
    if isinstance(value, str):
        return deidentify_text(value)
    if isinstance(value, dict):
        return {key: deidentify_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [deidentify_value(item) for item in value]
    return value
