"""Fail-closed validator for a documented subset, not a complete FHIR validator.

FHIR R4 resource definitions: https://hl7.org/fhir/R4/questionnaire-definitions.html
and https://hl7.org/fhir/R4/questionnaireresponse-definitions.html.
Clinical form content is supplied by the study; this module supplies no modes,
actions, defaults, or clinical judgement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import math
import re

VALIDATOR_VERSION = "0.1.0"
SUPPORTED_TYPES = frozenset({"group", "display", "choice", "string", "text", "boolean", "integer", "decimal", "date"})
Q_FIELDS = frozenset({"resourceType", "id", "url", "version", "status", "experimental", "name", "title", "description", "purpose", "publisher", "date", "language", "item"})
ITEM_FIELDS = frozenset({"linkId", "prefix", "text", "type", "required", "repeats", "readOnly", "maxLength", "answerOption", "enableWhen", "enableBehavior", "item"})
R_FIELDS = frozenset({"resourceType", "id", "questionnaire", "status", "authored", "item"})
R_ITEM_FIELDS = frozenset({"linkId", "text", "answer", "item"})
CODING_FIELDS = frozenset({"system", "version", "code", "display"})
VALUE_FIELDS = {"boolean": "valueBoolean", "integer": "valueInteger", "decimal": "valueDecimal", "date": "valueDate", "string": "valueString", "text": "valueString"}
OPERATORS = frozenset({"exists", "=", "!=", ">", "<", ">=", "<="})
ORDER_OPERATORS = frozenset({">", "<", ">=", "<="})
# Explicit cross-runtime blank set: Python Unicode isspace() plus BOM/FEFF.
# Native JavaScript trim() has a different set; the browser mirrors this list.
BLANK_CHARACTERS = frozenset(chr(cp) for cp in (
    *range(0x0009, 0x000E), *range(0x001C, 0x0021), 0x0085, 0x00A0, 0x1680,
    *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF,
))


class ReviewValidationError(ValueError):
    """Stable error code and JSON-style path; no raw clinical values in errors."""

    def __init__(self, code: str, path: str):
        self.code, self.path = code, path
        super().__init__(f"{code}:{path}")


def _fail(code, path):
    raise ReviewValidationError(code, path)


def _object(value, path, allowed=None):
    if not isinstance(value, dict):
        _fail("OBJECT_REQUIRED", path)
    if allowed is not None and set(value) - allowed:
        _fail("UNSUPPORTED_FIELD", path + "." + sorted(set(value) - allowed)[0])


def _string(value, path):
    if not isinstance(value, str) or not value or all(char in BLANK_CHARACTERS for char in value):
        _fail("NONEMPTY_STRING_REQUIRED", path)
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _fail("INVALID_UNICODE_STRING", path)
    return value


def _array(value, path, *, nonempty=True):
    if not isinstance(value, list) or (nonempty and not value):
        _fail("NONEMPTY_ARRAY_REQUIRED", path)
    return value


def _bool(value, path):
    if type(value) is not bool:
        _fail("BOOLEAN_REQUIRED", path)


def _uri(value, path):
    _string(value, path)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*:[^\s|\ufeff]+", value):
        _fail("ABSOLUTE_URI_REQUIRED", path)


def _full_date(value, path):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        _fail("FULL_DATE_REQUIRED", path)
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail("INVALID_DATE", path)


def _date_time(value, path):
    # Restriction to complete, timezone-bearing instants makes browser/receiver
    # handling deterministic; partial FHIR dateTime values are unsupported.
    # Python 3.14 accepts 24:00 as ISO 8601 midnight; the supported FHIR
    # dateTime subset does not. Check clock fields after lexical validation.
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        _fail("FULL_DATETIME_REQUIRED", path)
    hour, minute, second = (int(value[offset:offset + 2]) for offset in (11, 14, 17))
    if hour > 23 or minute > 59 or second > 59:
        _fail("INVALID_DATETIME", path)
    if value[-1] != "Z":
        offset_hours, offset_minutes = int(value[-5:-3]), int(value[-2:])
        if offset_hours > 14 or offset_minutes > 59 or (offset_hours == 14 and offset_minutes != 0):
            _fail("INVALID_DATETIME", path)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("INVALID_DATETIME", path)


def _coding(value, path):
    _object(value, path, CODING_FIELDS)
    _uri(value.get("system"), path + ".system")
    _string(value.get("code"), path + ".code")
    for key in ("version", "display"):
        if key in value:
            _string(value[key], path + "." + key)
    # display is presentation, never the identity of a permitted code.
    return (value["system"], value["code"], value.get("version"))


def _primitive(kind, value, path):
    if kind == "valueCoding":
        return _coding(value, path)
    if kind == "valueBoolean":
        _bool(value, path)
    elif kind == "valueInteger":
        if type(value) is not int or not -(2**31) <= value < 2**31:
            _fail("FHIR_INTEGER_REQUIRED", path)
    elif kind == "valueDecimal":
        if type(value) not in (int, float) or abs(value) > 9007199254740991 or not math.isfinite(value):
            _fail("FINITE_JSON_DECIMAL_REQUIRED", path)
    elif kind == "valueDate":
        _full_date(value, path)
    elif kind == "valueString":
        _string(value, path)
    else:
        _fail("UNSUPPORTED_ANSWER_TYPE", path)
    return value


@dataclass
class _Model:
    q: dict
    by_id: dict
    order: list
    parents: dict
    option_kinds: dict
    option_values: dict


def _compile(q):
    _object(q, "$", Q_FIELDS)
    if q.get("resourceType") != "Questionnaire":
        _fail("QUESTIONNAIRE_RESOURCE_REQUIRED", "$.resourceType")
    if q.get("status") != "active":
        _fail("ACTIVE_QUESTIONNAIRE_REQUIRED", "$.status")
    _uri(q.get("url"), "$.url")
    _string(q.get("version"), "$.version")
    if "|" in q["version"]:
        _fail("INVALID_QUESTIONNAIRE_VERSION", "$.version")
    for key in Q_FIELDS - {"resourceType", "url", "version", "status", "experimental", "item", "date"}:
        if key in q:
            _string(q[key], "$." + key)
    if "experimental" in q:
        _bool(q["experimental"], "$.experimental")
    if "date" in q:
        _date_time(q["date"], "$.date")
    model = _Model(q=q, by_id={}, order=[], parents={}, option_kinds={}, option_values={})

    def walk(items, parent, path):
        _array(items, path)
        for index, item in enumerate(items):
            p = f"{path}[{index}]"
            _object(item, p, ITEM_FIELDS)
            link = _string(item.get("linkId"), p + ".linkId")
            if link in model.by_id:
                _fail("DUPLICATE_QUESTIONNAIRE_LINK_ID", p + ".linkId")
            kind = item.get("type")
            if not isinstance(kind, str) or kind not in SUPPORTED_TYPES:
                _fail("UNSUPPORTED_ITEM_TYPE", p + ".type")
            for key in ("prefix", "text"):
                if key in item:
                    _string(item[key], p + "." + key)
            for key in ("required", "repeats", "readOnly"):
                if key in item:
                    _bool(item[key], p + "." + key)
            if item.get("readOnly"):
                _fail("READ_ONLY_UNSUPPORTED", p + ".readOnly")
            if kind == "display" and any(key in item for key in ("required", "repeats", "readOnly")):
                _fail("DISPLAY_CANNOT_HAVE_QUESTION_FLAGS", p)
            if item.get("repeats") and kind != "choice":
                _fail("ONLY_CHOICE_REPETITION_SUPPORTED", p + ".repeats")
            if "maxLength" in item:
                if kind not in ("string", "text") or type(item["maxLength"]) is not int or item["maxLength"] <= 0:
                    _fail("UNSUPPORTED_MAX_LENGTH", p + ".maxLength")
            if kind == "choice":
                options = _array(item.get("answerOption"), p + ".answerOption")
                values, option_kind = [], None
                for oi, option in enumerate(options):
                    op = f"{p}.answerOption[{oi}]"
                    _object(option, op, {"valueCoding", "valueString"})
                    if len(option) != 1:
                        _fail("SINGLE_OPTION_VALUE_REQUIRED", op)
                    key = next(iter(option))
                    if option_kind is not None and key != option_kind:
                        _fail("MIXED_CHOICE_OPTION_TYPES_UNSUPPORTED", op)
                    option_kind = key
                    value = _primitive(key, option[key], op + "." + key)
                    if value in values:
                        _fail("DUPLICATE_CHOICE_OPTION", op)
                    values.append(value)
                model.option_kinds[link], model.option_values[link] = option_kind, values
            elif "answerOption" in item:
                _fail("ANSWER_OPTIONS_ONLY_FOR_CHOICE", p + ".answerOption")
            conditions = item.get("enableWhen", [])
            if "enableWhen" in item:
                _array(conditions, p + ".enableWhen")
            if "enableBehavior" in item and (item["enableBehavior"] not in ("all", "any") or not conditions):
                _fail("INVALID_ENABLE_BEHAVIOR", p + ".enableBehavior")
            if len(conditions) > 1 and "enableBehavior" not in item:
                _fail("MULTIPLE_CONDITIONS_REQUIRE_BEHAVIOR", p + ".enableBehavior")
            for ci, condition in enumerate(conditions):
                cp = f"{p}.enableWhen[{ci}]"
                _object(condition, cp, {"question", "operator", "answerBoolean", "answerInteger", "answerDecimal", "answerDate", "answerString", "answerCoding"})
                source = condition.get("question")
                if not isinstance(source, str) or source not in model.by_id or model.by_id[source]["type"] in ("group", "display"):
                    _fail("CONDITION_REQUIRES_PRECEDING_QUESTION", cp + ".question")
                operator = condition.get("operator")
                if not isinstance(operator, str) or operator not in OPERATORS:
                    _fail("UNSUPPORTED_CONDITION_OPERATOR", cp + ".operator")
                keys = [key for key in condition if key.startswith("answer")]
                if len(keys) != 1:
                    _fail("SINGLE_CONDITION_VALUE_REQUIRED", cp)
                key = keys[0]
                if operator == "exists":
                    if key != "answerBoolean":
                        _fail("EXISTS_REQUIRES_BOOLEAN", cp)
                    _bool(condition[key], cp + "." + key)
                else:
                    source_type = model.by_id[source]["type"]
                    expected = model.option_kinds[source] if source_type == "choice" else VALUE_FIELDS[source_type]
                    if key != "answer" + expected[5:]:
                        _fail("CONDITION_TYPE_MISMATCH", cp + "." + key)
                    value = _primitive(expected, condition[key], cp + "." + key)
                    if source_type == "choice" and value not in model.option_values[source]:
                        _fail("CONDITION_CHOICE_NOT_PERMITTED", cp + "." + key)
                    if operator in ORDER_OPERATORS and source_type not in ("integer", "decimal", "date"):
                        _fail("ORDER_CONDITION_TYPE_UNSUPPORTED", cp + ".operator")
            model.by_id[link], model.parents[link] = item, parent
            model.order.append(link)
            if kind == "group":
                walk(item.get("item"), link, p + ".item")
            elif "item" in item:
                _fail("ONLY_GROUP_CHILDREN_SUPPORTED", p + ".item")

    walk(q.get("item"), None, "$.item")
    def has_answerable_descendant(item):
        return any(child["type"] not in ("group", "display")
                   or (child["type"] == "group" and has_answerable_descendant(child))
                   for child in item["item"])

    for link, item in model.by_id.items():
        if item["type"] == "group" and item.get("required") and not has_answerable_descendant(item):
            _fail("REQUIRED_GROUP_HAS_NO_ANSWERABLE_ITEM", f"$.linkId[{link}]")
    return model


def validate_questionnaire(q: dict) -> dict:
    """Accept a raw active Questionnaire only if every used feature is supported."""
    model = _compile(q)
    return {"status": "PASS", "validator_version": VALIDATOR_VERSION,
            "questionnaire": f"{q['url']}|{q['version']}", "item_count": len(model.order)}


def validate_questionnaire_response(q: dict, response: dict, *, require_completed: bool = True) -> dict:
    """Validate exact form binding, tree, types, options and enabled requirements."""
    model = _compile(q)
    _object(response, "$response", R_FIELDS)
    if response.get("resourceType") != "QuestionnaireResponse":
        _fail("QUESTIONNAIRE_RESPONSE_REQUIRED", "$response.resourceType")
    canonical = f"{q['url']}|{q['version']}"
    if response.get("questionnaire") != canonical:
        _fail("RESPONSE_QUESTIONNAIRE_MISMATCH", "$response.questionnaire")
    status = response.get("status")
    if status not in ("in-progress", "completed", "amended", "entered-in-error", "stopped"):
        _fail("INVALID_RESPONSE_STATUS", "$response.status")
    if require_completed and status != "completed":
        _fail("COMPLETED_RESPONSE_REQUIRED", "$response.status")
    if "authored" in response:
        _date_time(response["authored"], "$response.authored")
    if "id" in response:
        _string(response["id"], "$response.id")
    present, answers = {}, {}

    def walk(items, expected, parent, path):
        _array(items, path)
        siblings = {item["linkId"]: index for index, item in enumerate(expected)}
        last_position = -1
        for index, ritem in enumerate(items):
            p = f"{path}[{index}]"
            _object(ritem, p, R_ITEM_FIELDS)
            link = _string(ritem.get("linkId"), p + ".linkId")
            if link not in model.by_id:
                _fail("UNKNOWN_RESPONSE_LINK_ID", p + ".linkId")
            if link in present:
                _fail("DUPLICATE_RESPONSE_ITEM", p + ".linkId")
            if link not in siblings or model.parents[link] != parent:
                _fail("RESPONSE_HIERARCHY_MISMATCH", p + ".linkId")
            if siblings[link] <= last_position:
                _fail("RESPONSE_ORDER_MISMATCH", p + ".linkId")
            last_position = siblings[link]
            item = model.by_id[link]
            present[link] = ritem
            if "text" in ritem and ritem["text"] != item.get("text"):
                _fail("RESPONSE_TEXT_MISMATCH", p + ".text")
            kind = item["type"]
            if kind in ("group", "display"):
                if "answer" in ritem:
                    _fail("NONQUESTION_CANNOT_HAVE_ANSWERS", p + ".answer")
                if kind == "group" and "item" in ritem:
                    walk(ritem["item"], item["item"], link, p + ".item")
                elif kind == "display" and "item" in ritem:
                    _fail("DISPLAY_CANNOT_HAVE_CHILDREN", p + ".item")
                continue
            if "item" in ritem:
                _fail("QUESTION_CHILDREN_UNSUPPORTED", p + ".item")
            values = []
            if "answer" in ritem:
                raw_answers = _array(ritem["answer"], p + ".answer")
                if len(raw_answers) > 1 and not item.get("repeats", False):
                    _fail("MULTIPLE_ANSWERS_NOT_ALLOWED", p + ".answer")
                expected_key = model.option_kinds[link] if kind == "choice" else VALUE_FIELDS[kind]
                for ai, answer in enumerate(raw_answers):
                    ap = f"{p}.answer[{ai}]"
                    _object(answer, ap, {expected_key})
                    if len(answer) != 1 or expected_key not in answer:
                        _fail("SINGLE_TYPED_ANSWER_REQUIRED", ap)
                    value = _primitive(expected_key, answer[expected_key], ap + "." + expected_key)
                    if kind == "choice" and value not in model.option_values[link]:
                        _fail("ANSWER_CHOICE_NOT_PERMITTED", ap)
                    if kind in ("string", "text") and "maxLength" in item and len(value) > item["maxLength"]:
                        _fail("ANSWER_EXCEEDS_MAX_LENGTH", ap)
                    if value in values:
                        _fail("DUPLICATE_ANSWER", ap)
                    values.append(value)
            answers[link] = values

    if "item" in response:
        walk(response["item"], q["item"], None, "$response.item")
    enabled = {}

    def condition_met(condition):
        source = condition["question"]
        values = answers.get(source, []) if enabled.get(source, False) else []
        operator = condition["operator"]
        if operator == "exists":
            return bool(values) == condition["answerBoolean"]
        key = next(key for key in condition if key.startswith("answer"))
        value = _primitive("value" + key[6:], condition[key], "$condition")
        # != is absence of an equal answer, not existence of a different one.
        # This matters for multiple selections and unanswered optional questions.
        if operator == "!=":
            return not any(answer == value for answer in values)
        checks = {"=": lambda a: a == value,
                  ">": lambda a: a > value, "<": lambda a: a < value,
                  ">=": lambda a: a >= value, "<=": lambda a: a <= value}
        return any(checks[operator](answer) for answer in values)

    required_check = require_completed or status in ("completed", "amended")

    def answered_descendant_of(group):
        for answer_link, values in answers.items():
            if not values:
                continue
            ancestor = model.parents[answer_link]
            while ancestor is not None:
                if ancestor == group:
                    return True
                ancestor = model.parents[ancestor]
        return False

    for link in model.order:
        item, parent = model.by_id[link], model.parents[link]
        conditions = item.get("enableWhen", [])
        results = [condition_met(condition) for condition in conditions]
        own_enabled = (all(results) if item.get("enableBehavior") == "all" else any(results)) if results else True
        enabled[link] = own_enabled and (parent is None or enabled[parent])
        if not enabled[link]:
            if link in present:
                _fail("DISABLED_ITEM_PRESENT", f"$response.linkId[{link}]")
            continue
        if required_check and item.get("required"):
            if item["type"] == "group":
                if link not in present or not answered_descendant_of(link):
                    _fail("REQUIRED_GROUP_MISSING", f"$response.linkId[{link}]")
            elif not answers.get(link):
                _fail("REQUIRED_ANSWER_MISSING", f"$response.linkId[{link}]")
    return {"status": "PASS", "validator_version": VALIDATOR_VERSION,
            "questionnaire": canonical, "response_status": status,
            "answered_item_count": sum(bool(values) for values in answers.values()),
            "enabled_link_ids": [link for link in model.order if enabled[link]]}


def validate_submission(submission: dict, packet: dict) -> dict:
    """First verify existing packet/binding hashes, then verify actual answers."""
    from ..p2_checkpoint_packet import validate_review_submission

    validate_review_submission(submission, packet)
    return validate_questionnaire_response(packet["questionnaire"]["resource"],
                                           submission["questionnaire_response"], require_completed=True)
