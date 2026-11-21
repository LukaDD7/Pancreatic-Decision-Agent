"""Load and expand the draft management-action dictionary using only stdlib."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re


PHASES = {
    "INITIAL_RESECTABILITY_ASSESSMENT",
    "PREOPERATIVE_OPTIMIZATION",
    "NAT_OR_CONVERSION",
    "POST_TREATMENT_REASSESSMENT",
    "SURGICAL_EXPLORATION",
    "POSTOPERATIVE_TRAJECTORY",
}
DEFAULT_DICTIONARY_PATH = (
    Path(__file__).resolve().parents[4] / "configs" / "action_dictionary.v0.2.draft.json"
)
_ABSTRACT_TARGET_TERMS = (
    "完善检查", "综合治疗", "手术评估", "可切除性再评估", "可切除性评估",
)
_BARE_ABSTRACT_TARGETS = {
    "检查", "完善", "评估", "再评估", "影像", "imaging", "手术", "surgery",
    "治疗", "新辅助治疗", "支持治疗", "随访", "穿刺", "穿刺活检", "mdt",
}


def load_dictionary(path=None):
    source = Path(path) if path is not None else DEFAULT_DICTIONARY_PATH
    value = json.loads(source.read_text(encoding="utf-8"))
    if value.get("status") != "DRAFT_NOT_CLINICAL_REFERENCE":
        raise ValueError("ACTION_DICTIONARY_STATUS_INVALID")
    if not isinstance(value.get("schema_version"), str) or not value["schema_version"]:
        raise ValueError("ACTION_DICTIONARY_VERSION_REQUIRED")
    actions = value.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("ACTION_DICTIONARY_ACTIONS_REQUIRED")
    seen = set()
    for action in actions:
        for field in ("action_id", "action_type", "label", "target", "allowed_phases"):
            if field not in action:
                raise ValueError("ACTION_DICTIONARY_FIELD_REQUIRED:" + field)
        action_id = action["action_id"]
        if not isinstance(action_id, str) or not action_id or action_id in seen:
            raise ValueError("ACTION_DICTIONARY_ACTION_ID_INVALID:" + str(action_id))
        seen.add(action_id)
        phases = action["allowed_phases"]
        if not isinstance(phases, list) or not phases or not set(phases) <= set(PHASES):
            raise ValueError("ACTION_DICTIONARY_PHASE_INVALID:" + action_id)
        for field in ("historical_usage", "current_usage", "center_availability"):
            if action.get(field) != "PENDING_FREEZE":
                raise ValueError("ACTION_DICTIONARY_FREEZE_STATUS_INVALID:" + action_id)
    return value


def _actions(dictionary=None):
    return (load_dictionary() if dictionary is None else dictionary)["actions"]


def action_ids(*, dictionary=None):
    return [action["action_id"] for action in _actions(dictionary)]


def get_action(action_id, *, dictionary=None):
    action = next((item for item in _actions(dictionary)
                   if item["action_id"] == action_id), None)
    if action is None:
        raise ValueError("ACTION_ID_UNKNOWN:" + str(action_id))
    return deepcopy(action)


def action_ids_for_phase(phase, *, dictionary=None):
    if phase not in PHASES:
        raise ValueError("ACTION_PHASE_UNKNOWN:" + str(phase))
    return [action["action_id"] for action in _actions(dictionary)
            if phase in action["allowed_phases"]]


def dictionary_version(*, dictionary=None):
    return (load_dictionary() if dictionary is None else dictionary)["schema_version"]


def dictionary_hash(*, dictionary=None):
    value = load_dictionary() if dictionary is None else dictionary
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def canonical_action(action_id, *, target=None, phase=None, dictionary=None):
    action = get_action(action_id, dictionary=dictionary)
    if phase is not None and phase not in action["allowed_phases"]:
        if phase not in PHASES:
            raise ValueError("ACTION_PHASE_UNKNOWN:" + str(phase))
        raise ValueError("ACTION_PHASE_NOT_ALLOWED:%s:%s" % (action_id, phase))
    if action_id == "OTHER_SPECIFIED":
        specific = target.strip() if isinstance(target, str) else ""
        if not specific:
            raise ValueError("ACTION_TARGET_REQUIRED_FOR_OTHER_SPECIFIED")
        normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", specific.lower())
        if any(term in normalized for term in _ABSTRACT_TARGET_TERMS) \
                or normalized in _BARE_ABSTRACT_TARGETS:
            raise ValueError("ACTION_TARGET_ABSTRACT_FOR_OTHER_SPECIFIED:" + specific)
        action["target"] = specific
    return {key: deepcopy(action[key]) for key in (
        "action_id", "action_type", "label", "target", "allowed_phases")}


__all__ = [
    "PHASES", "DEFAULT_DICTIONARY_PATH", "load_dictionary", "action_ids", "get_action",
    "action_ids_for_phase", "dictionary_version", "dictionary_hash", "canonical_action",
]
