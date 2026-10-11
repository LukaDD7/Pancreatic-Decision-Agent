"""Versioned decision interface, strict parsing and visible-source validation."""

from importlib.resources import files
import json

import jsonschema

from ..four_stage import sha

PROTOCOL_VERSION = "independent-checkpoint-eval-v0.1"
PROMPT_VERSION = "pancreas-predecision-json-v0.1.1"
RESOURCES = files(__package__).joinpath("resources")
PROMPT_TEXT = RESOURCES.joinpath("system-v0.1.1.txt").read_text(encoding="utf-8")
ACTION_CATALOG = json.loads(RESOURCES.joinpath("actions-v0.1.json").read_text(encoding="utf-8"))
OUTPUT_SCHEMA = json.loads(RESOURCES.joinpath("output-v0.1.schema.json").read_text(encoding="utf-8"))
ACTION_MODES = {a["code"]: a["mode"] for a in ACTION_CATALOG["actions"]}
ACTION_TEXT = "\n".join("\n" + mode + " 可选动作：\n" + "\n".join(
    a["code"] + "：" + a["description"] for a in ACTION_CATALOG["actions"] if a["mode"] == mode)
    for mode in ("PAUSE", "CONTINUE", "EXIT"))
SYSTEM_PROMPT = (PROMPT_TEXT + "\n动作表（分组说明，不是回答JSON模板）：\n" + ACTION_TEXT
                 + "\n输出 JSON Schema：\n" + json.dumps(OUTPUT_SCHEMA, ensure_ascii=False))


def build_messages(question, context):
    """Only the clinical question and entire fixed context enter the user message."""
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "问题：" + question + "\n\n患者资料：\n\n" + context}]


def protocol_manifest():
    return {"protocol_version": PROTOCOL_VERSION, "prompt_version": PROMPT_VERSION,
            "system_prompt_sha256": sha(SYSTEM_PROMPT), "output_schema_sha256": sha(OUTPUT_SCHEMA),
            "action_space_version": ACTION_CATALOG["version"], "action_space_sha256": sha(ACTION_CATALOG)}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("NONFINITE_JSON_NUMBER")


def validate_output(text, visible_source_ids):
    """Return the parsed candidate and safe error codes; never repair a decision."""
    try:
        candidate = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, TypeError):
        return None, ["INVALID_JSON"]
    errors = list(jsonschema.Draft202012Validator(OUTPUT_SCHEMA).iter_errors(candidate))
    if errors:
        # jsonschema's default error message can echo arbitrary model text.
        return candidate, ["SCHEMA:" + "/".join(map(str, e.absolute_path)) + ":" + str(e.validator)
                           for e in errors]
    problems = []
    if ACTION_MODES[candidate["primary_action"]["code"]] != candidate["mode"]:
        problems.append("MODE_ACTION_MISMATCH")
    cited = set(candidate["primary_action"]["source_ids"])
    cited.update(e["source_id"] for e in candidate["evidence"])
    if cited - set(visible_source_ids):
        problems.append("NONVISIBLE_SOURCE_REFERENCE")
    if candidate["mode"] == "PAUSE" and not candidate["decision_change_conditions"]:
        problems.append("PAUSE_REQUIRES_DECISION_CHANGE_CONDITION")
    return candidate, problems
