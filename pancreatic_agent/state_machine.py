"""Explicit control flow, independent of the clinical policy and outcome oracle."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from datetime import timedelta
from enum import Enum
import json
from pathlib import Path
import re
from typing import Any, Protocol

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_SCHEMA = json.loads((ROOT / "contracts/v0.1/06_agent_output.schema.json").read_text())
ACTION_MAP = json.loads((ROOT / "configs/action_event_map.v1.json").read_text())
DOMAINS = {"M_LIVER", "M_PERITONEAL", "M_LUNG", "M_OTHER", "LOCAL_RESECTABILITY", "HISTOLOGY", "FITNESS", "DATA_FRESHNESS", "OTHER"}
TERMINAL_MODES = {
    "CONTINUE_NO_NEW_STAGING": "CONTINUE_CURATIVE_PATH",
    "EXIT_CURATIVE_PATH": "EXIT_CURATIVE_PATH",
    "DEFER_EXPERT_REVIEW": "DEFER_TO_EXPERT",
}
BASELINE_KINDS = {"history", "physical_exam", "imaging", "laboratory", "preoperative_pathology"}
ALL_KINDS = BASELINE_KINDS | {"laparoscopy_finding", "expert_review"}
DECISION_TEXT = re.compile(r"拟行.{0,80}(?:根治术|切除术|腹腔镜)|拟施手术|拟手术名称|planned_strategy|无(?:明确)?手术禁忌|已签署.{0,20}手术|术中诊断|最终病理")


class Status(str, Enum):
    READY = "READY"
    DECIDING = "DECIDING"
    WAITING_FOR_OBSERVATION = "WAITING_FOR_OBSERVATION"
    TERMINAL = "TERMINAL"
    COUNTERFACTUAL_UNOBSERVED = "COUNTERFACTUAL_UNOBSERVED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    SYSTEM_FAILURE = "SYSTEM_FAILURE"
    DEPTH_LIMIT_REACHED = "DEPTH_LIMIT_REACHED"


class AuditError(ValueError):
    pass


def exact_time(value):
    if not isinstance(value, str) or len(value) < 16:
        raise AuditError("Precise time required; date-only is not an availability timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AuditError("Invalid availability timestamp") from exc
    if parsed.tzinfo is not None:
        raise AuditError("This pilot requires consistently normalized local-naive timestamps")
    return parsed


@dataclass
class Evidence:
    source_id: str
    domain: str
    value: Any
    kind: str
    raw_excerpt: str
    available_at: str | None
    time_basis: str
    source_time: str | None = None
    epistemic_status: str = "present"
    role: str = "clinical_evidence"
    reviewed: bool = False
    associated_domains: list[str] = field(default_factory=list)

    def validate(self, baseline=False):
        if not isinstance(self.source_id, str) or not self.source_id or self.domain not in DOMAINS:
            raise AuditError("Invalid evidence source or domain")
        if type(self.reviewed) is not bool or not isinstance(self.raw_excerpt, str):
            raise AuditError("Evidence review flag/excerpt has wrong type")
        if not isinstance(self.associated_domains, list) or any(d not in DOMAINS for d in self.associated_domains):
            raise AuditError("Invalid associated evidence domains")
        if self.role != "clinical_evidence":
            raise AuditError("Clinician decision, outcome, plan and retrospective mention are forbidden")
        if self.kind not in (BASELINE_KINDS if baseline else ALL_KINDS):
            raise AuditError("Source type not allowed at this decision step")
        if self.epistemic_status not in {"present", "unknown", "conflicting"}:
            raise AuditError("Unknown evidence status")
        if self.epistemic_status == "unknown" and self.value is not None:
            raise AuditError("Unknown evidence must not be represented as a negative/value")
        if self.time_basis not in {"EXACT", "EXAM_PROXY", "DOCUMENT_PROXY", "NARRATIVE_ORDER", "UNKNOWN"}:
            raise AuditError("Unknown temporal basis")
        if self.time_basis == "EXACT":
            exact_time(self.available_at)
        if DECISION_TEXT.search(json.dumps(self.value, ensure_ascii=False) + " " + self.raw_excerpt):
            raise AuditError("Potential clinician-decision leakage: paragraph review required")

    def visible(self):
        # Review annotations are environment-side; no raw documents or source bundle.
        return {k: v for k, v in asdict(self).items() if k not in {"reviewed", "role"}}


@dataclass
class CaseState:
    case_id: str
    evidence: list[Evidence]
    decision_time: str | None
    endpoint: str = "BEFORE_ANY_INCISION"
    temporal_policy: str = "STRICT"
    baseline_reviewed: bool = False
    endpoint_time: str | None = None
    max_window_days: int = 30
    baseline_time: str | None = None
    step: int = 0
    agent_history: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value):
        allowed = {"case_id", "evidence", "decision_time", "endpoint", "temporal_policy", "baseline_reviewed", "endpoint_time", "max_window_days"}
        if set(value) - allowed:
            raise AuditError("Unexpected baseline fields: never load the full source bundle")
        try:
            kwargs = dict(value)
            kwargs["evidence"] = [Evidence(**e) for e in value["evidence"]]
            return cls(**kwargs)
        except (TypeError, KeyError) as exc:
            raise AuditError("Invalid baseline structure") from exc

    def validate(self, for_execution=False):
        if not isinstance(self.case_id, str) or not self.case_id or type(self.baseline_reviewed) is not bool:
            raise AuditError("Invalid case/review metadata")
        if self.endpoint not in {"BEFORE_ANY_INCISION", "BEFORE_DEFINITIVE_RESECTION"}:
            raise AuditError("Unknown irreversible boundary")
        if self.temporal_policy not in {"STRICT", "REVIEWED_PROXY"}:
            raise AuditError("Unknown temporal policy")
        if type(self.max_window_days) is not int or self.max_window_days <= 0:
            raise AuditError("Invalid replay window")
        if self.decision_time:
            exact_time(self.decision_time)
        if self.endpoint_time:
            exact_time(self.endpoint_time)
            if self.decision_time and exact_time(self.endpoint_time) <= exact_time(self.decision_time):
                raise AuditError("Decision must precede the irreversible endpoint")
        if not self.evidence or len({e.source_id for e in self.evidence}) != len(self.evidence):
            raise AuditError("Missing evidence or duplicate sources")
        for e in self.evidence:
            e.validate(baseline=self.step == 0)
            if e.available_at and self.decision_time and exact_time(e.available_at) > exact_time(self.decision_time):
                raise AuditError("Future evidence is not allowed")
            if for_execution:
                if not self.baseline_reviewed or not e.reviewed:
                    raise AuditError("Baseline/paragraph review has not been completed")
                if self.temporal_policy == "STRICT" and (not self.decision_time or e.time_basis != "EXACT" or not e.available_at):
                    raise AuditError("Strict replay requires verified availability")
                if e.time_basis == "UNKNOWN":
                    raise AuditError("Unknown availability must be resolved by review")

    def visible(self):
        self.validate()
        return {
            "case_id": self.case_id,
            "step": self.step,
            "as_of": self.decision_time,
            "irreversible_boundary": self.endpoint,
            "temporal_policy": self.temporal_policy,
            "evidence": [e.visible() for e in self.evidence],
            "agent_previous_decisions": deepcopy(self.agent_history),
            "domains_without_indexed_evidence": sorted(DOMAINS - {domain for e in self.evidence for domain in [e.domain, *e.associated_domains]}),
        }


def validate_output(raw, state):
    # No fence stripping, JSON repair, enum coercion or replacement clinical action.
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AuditError("Unparseable model output") from exc
    if list(Draft202012Validator(OUTPUT_SCHEMA).iter_errors(value)):
        raise AuditError("Output violates the original strict JSON schema")
    if value["case_id"] != state.case_id or value["step"] != state.step:
        raise AuditError("Wrong case or decision step")
    allowed = {e.source_id for e in state.evidence}
    cited = set(value["source_ids"])
    gaps = value["decision_gaps"]
    if len({g["gap_id"] for g in gaps}) != len(gaps):
        raise AuditError("Duplicate gap identifiers")
    for gap in gaps:
        cited.update(gap["source_ids"])
    if not cited <= allowed:
        raise AuditError("Citation absent from currently allowed evidence")
    action = value["action"]
    name = action["canonical_action"]
    if name in TERMINAL_MODES:
        if value["mode"] != TERMINAL_MODES[name]:
            raise AuditError("Terminal mode/action mismatch")
        if action["expected_information_gain"] != "NOT_APPLICABLE":
            raise AuditError("Terminal action cannot claim new information gain")
        if action["target_gap_id"] is not None or action["target_site"] is not None:
            raise AuditError("Terminal action cannot request an acquisition target")
    else:
        ids = {g["gap_id"] for g in gaps}
        if value["mode"] != "PAUSE_FOR_EVIDENCE" or action["target_gap_id"] not in ids or not action["target_site"]:
            raise AuditError("Evidence acquisition requires one existing gap and a target site")
        if action["expected_information_gain"] == "NOT_APPLICABLE":
            raise AuditError("Acquisition requires an information-gain estimate")
    return value


@dataclass
class Observation:
    event_id: str
    clinical_event_id: str
    event_type: str
    target_site: str
    resolves_domains: list[str]
    evidence: list[Evidence]
    available_at: str | None
    time_basis: str
    audited: bool = False
    after_baseline: bool = False
    after_incision: bool = False
    after_definitive_resection: bool = False
    before_irreversible_boundary: bool = False
    order_reviewed: bool = False
    compatible_actions: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value):
        try:
            data = dict(value)
            data["evidence"] = [Evidence(**e) for e in data["evidence"]]
            return cls(**data)
        except (TypeError, KeyError) as exc:
            raise AuditError("Invalid observation structure") from exc


def normalize_site(site):
    for canonical, aliases in ACTION_MAP["site_aliases"].items():
        if site in aliases:
            return canonical
    return site


class LoggedEnvironment:
    def __init__(self, observations):
        self.observations = list(observations)
        self.revealed_clinical_events = set()

    def transition(self, state, output):
        action = output["action"]
        domain = next(g["domain"] for g in output["decision_gaps"] if g["gap_id"] == action["target_gap_id"])
        eligible, needs_review = [], False
        for event in self.observations:
            if event.event_type in ACTION_MAP["never_reveal"] or event.clinical_event_id in self.revealed_clinical_events:
                continue
            expected = ACTION_MAP["event_actions"].get(event.event_type)
            match = "EXACT" if expected == action["canonical_action"] else "COMPATIBLE" if action["canonical_action"] in event.compatible_actions else None
            if not match or normalize_site(event.target_site) != normalize_site(action["target_site"]) or domain not in event.resolves_domains:
                continue
            flags = [event.audited, event.after_baseline, event.after_incision, event.after_definitive_resection, event.before_irreversible_boundary, event.order_reviewed]
            if any(type(flag) is not bool for flag in flags) or not event.event_id or not event.clinical_event_id:
                needs_review = True
                continue
            if event.after_definitive_resection or not event.before_irreversible_boundary:
                continue
            if state.endpoint == "BEFORE_ANY_INCISION" and event.after_incision:
                continue
            if not event.audited or not event.evidence or any(not e.reviewed for e in event.evidence):
                needs_review = True
                continue
            try:
                for e in event.evidence:
                    e.validate()
                if {e.source_id for e in event.evidence} & {e.source_id for e in state.evidence}:
                    raise AuditError("Observation reuses an already visible source")
                if event.time_basis == "EXACT":
                    if not state.decision_time:
                        needs_review = True
                        continue
                    if exact_time(event.available_at) <= exact_time(state.decision_time):
                        continue
                    if state.endpoint_time and exact_time(event.available_at) >= exact_time(state.endpoint_time):
                        continue
                    if state.baseline_time and exact_time(event.available_at) > exact_time(state.baseline_time) + timedelta(days=state.max_window_days):
                        continue
                    for e in event.evidence:
                        if not e.available_at or not exact_time(state.decision_time) < exact_time(e.available_at) <= exact_time(event.available_at):
                            raise AuditError("Observation contains evidence not yet available")
                elif event.time_basis not in {"EXAM_PROXY", "DOCUMENT_PROXY", "NARRATIVE_ORDER"} or state.temporal_policy != "REVIEWED_PROXY" or not event.order_reviewed or not event.after_baseline:
                    raise AuditError("Non-exact observation order is unreviewed")
                if state.temporal_policy == "STRICT" and any(e.time_basis != "EXACT" for e in event.evidence):
                    raise AuditError("Strict transition cannot contain proxy-timed evidence")
            except AuditError:
                needs_review = True
                continue
            eligible.append((event, match))
        if not eligible:
            return (Status.REVIEW_REQUIRED if needs_review else Status.COUNTERFACTUAL_UNOBSERVED), None, None
        # Narrative-order candidates need an adjudicated order, not list-order selection.
        if len(eligible) > 1 and any(e.time_basis != "EXACT" for e, _ in eligible):
            return Status.REVIEW_REQUIRED, None, None
        eligible.sort(key=lambda item: exact_time(item[0].available_at) if item[0].available_at else datetime.max)
        event, match = eligible[0]
        self.revealed_clinical_events.add(event.clinical_event_id)
        return Status.READY, event, match


class Policy(Protocol):
    def decide(self, state: dict) -> str: ...


class StateMachine:
    def __init__(self, baseline, environment, log_path=None):
        self.state = deepcopy(baseline)
        self.state.baseline_time = self.state.decision_time
        self.environment = environment
        self.status = Status.READY
        self.trace = []
        self.log_path = Path(log_path) if log_path else None
        self.terminal_output = None

    def record(self, **data):
        item = {"step": self.state.step, "status": self.status.value, **data}
        self.trace.append(item)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def run(self, policy):
        if self.status != Status.READY:
            raise AuditError("An ended episode cannot be restarted")
        try:
            self.state.validate(for_execution=True)
        except AuditError as exc:
            self.status = Status.REVIEW_REQUIRED
            self.record(reason=str(exc))
            return self.status
        while self.status == Status.READY:
            self.status = Status.DECIDING
            payload = self.state.visible()
            self.record(event="policy_input", visible_state=payload)
            try:
                raw = policy.decide(deepcopy(payload))
                if getattr(policy, "last_metadata", None):
                    self.record(event="provider_metadata", metadata=policy.last_metadata)
                self.record(event="raw_output", raw_output=raw)
                output = validate_output(raw, self.state)
            except Exception as exc:
                if isinstance(getattr(exc, "raw_output", None), str):
                    self.record(event="raw_output", raw_output=exc.raw_output)
                if getattr(policy, "last_metadata", None):
                    self.record(event="provider_metadata", metadata=policy.last_metadata)
                self.status = Status.SYSTEM_FAILURE
                # Never echo provider exception bodies (may contain sensitive data).
                reason = str(exc) if isinstance(exc, AuditError) else type(exc).__name__
                self.record(reason=reason, reliability_disposition="DEFER_TO_EXPERT")
                return self.status
            self.record(event="validated_output", output=output)
            action = output["action"]["canonical_action"]
            if action in TERMINAL_MODES:
                self.status = Status.TERMINAL
                self.terminal_output = output
                self.record(event="terminal", action=action)
                return self.status
            if self.state.step >= 2:
                self.status = Status.DEPTH_LIMIT_REACHED
                self.record(reason="Third acquisition is forbidden; no clinical action substituted")
                return self.status
            self.status = Status.WAITING_FOR_OBSERVATION
            self.record(event="request_observation", action=output["action"])
            status, observation, match = self.environment.transition(self.state, output)
            self.status = status
            if observation is None:
                self.record(event="replay_stop", reason=status.value)
                return self.status
            self.state.agent_history.append({"step": self.state.step, "mode": output["mode"], "action": output["action"], "decision_gaps": output["decision_gaps"]})
            self.state.evidence.extend(deepcopy(observation.evidence))
            self.state.step += 1
            # A narrative boundary stays imprecise; do not invent a timestamp.
            self.state.decision_time = observation.available_at
            self.record(event="observation_revealed", event_id=observation.event_id, clinical_event_id=observation.clinical_event_id, match=match, time_basis=observation.time_basis)
        return self.status
