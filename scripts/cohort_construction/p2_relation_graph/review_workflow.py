"""Append-only checkpoint validity / structural repair / clinical reference ledger.

Consumes independently prepared, versioned blind packets. It does not infer
cutpoints, repair clinical content, define episodes or construct expert answers.
"""
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import statistics
from uuid import uuid4
import argparse

from jsonschema import Draft202012Validator
from .core import digest

ISSUES = {"INVALID_DUPLICATE", "INVALID_MIXED", "INVALID_BOUNDARY",
          "SUSPECTED_AVAILABLE_EVIDENCE_OMISSION", "FUTURE_LEAKAGE", "NOT_TARGET_DECISION", "UNCERTAIN"}


def _time(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not dt.tzinfo: raise ValueError("explicit timezone required")
    return dt


def state(rows):
    packets, gates, adjudications, references, exposures = {}, [], [], [], []
    for sequence, row in enumerate(rows):
        p = row["payload"]
        if row["kind"] == "PACKET": packets[p["version_id"]] = p
        elif row["kind"] == "GATE": gates.append(p)
        elif row["kind"] == "ADJUDICATION":
            adjudications.append(p)
            for v in p["parent_versions"]:
                exposures.append({"patient_alias": packets[v]["patient_alias"],
                                  "reviewer_id": p["adjudicator_id"], "reason": "STRUCTURAL_ADJUDICATION", '_sequence':sequence})
        elif row["kind"] == "REFERENCE": references.append({**p,'_sequence':sequence})
        elif row["kind"] == "EXPOSURE": exposures.append({**p,'_sequence':sequence})
    return packets, gates, adjudications, references, exposures


def gate_status(version, gates, required):
    records = [g for g in gates if g["version_id"] == version]
    if any(g["decision"] != "VALID" for g in records): return "FLAGGED"
    return "VALID" if len(records) >= required else "PENDING"


class ReviewLedger:
    def __init__(self, path: Path, *, required_valid_reviewers=2):
        self.path, self.required = path, required_valid_reviewers
        repo = Path(__file__).resolve().parents[3]
        # Temporary test directories can explicitly use the in-memory validator;
        # real on-disk ledgers always remain under ignored project directories.
        if not any(path.resolve().is_relative_to(repo/d) for d in ["private", "outputs"]):
            raise ValueError("review ledger must remain in private/ or outputs/")
        if required_valid_reviewers < 1: raise ValueError("invalid reviewer count")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def verify(rows):
        previous = None; seen = set()
        for row in rows:
            unsigned = {k: v for k, v in row.items() if k != "hash"}
            if row["hash"] != digest(unsigned) or row["previous_hash"] != previous or row["event_id"] in seen:
                raise ValueError("review ledger integrity failure")
            previous = row["hash"]; seen.add(row["event_id"])

    @staticmethod
    def validate(kind, p, rows, required):
        packets, gates, adjudications, refs, exposures = state(rows)
        if kind == "PACKET":
            if p["version_id"] in packets: raise ValueError("packet version is immutable")
            if p.get("preparation_status") != "SOURCE_AND_BOUNDARY_REVIEWED":
                raise ValueError("blind packet needs independent source/boundary preparation review")
            if not p.get("preparation_reviewer_id") or not p.get("protocol_version") or not p.get("packet_sha256"):
                raise ValueError("packet provenance incomplete")
            if not p.get("reference_form_version") or not p.get("reference_form_sha256"):
                raise ValueError("clinical form must be fixed before packet registration")
            _time(p["as_of"])
            parents = p.get("parent_versions", [])
            if parents:
                if any(v not in packets for v in parents): raise ValueError("unknown parent version")
                relevant = [a for a in adjudications if set(parents) <= set(a["parent_versions"])]
                if not relevant: raise ValueError("repair needs prior structural adjudication")
                if relevant[-1]["action"] in {"UNRESOLVED", "OFF_SCOPE"}:
                    raise ValueError("unresolved/off-scope adjudication cannot generate a clinical re-review packet")
                roots = set().union(*(set(packets[v]["root_proposals"]) for v in parents))
                if roots != set(p["root_proposals"]): raise ValueError("original proposal lineage must be retained")
                if any(packets[v]["patient_alias"] != p["patient_alias"] for v in parents): raise ValueError("cross-patient repair")
            elif p["root_proposals"] != [p["proposal_id"]]:
                raise ValueError("initial proposal must identify its original root")
            elif any(not old.get('parent_versions') and old['proposal_id'] == p['proposal_id'] for old in packets.values()):
                raise ValueError('new version of existing proposal requires explicit parent lineage')
            if any(k in p for k in ["agent_output", "model_confidence", "historical_action", "future_outcome"]):
                raise ValueError("research-only fields cannot enter blind packet registry")
            return
        if kind == "EXPOSURE":
            if not p.get("reviewer_id") or not p.get("patient_alias") or not p.get("reason"):
                raise ValueError("exposure provenance required")
            return
        if kind == "ADJUDICATION":
            parents = p["parent_versions"]
            if not parents or any(v not in packets for v in parents): raise ValueError("unknown adjudication parents")
            if not any(gate_status(v, gates, required) == "FLAGGED" for v in parents):
                raise ValueError("structural adjudication requires a flagged version")
            if len({packets[v]['patient_alias'] for v in parents}) != 1:
                raise ValueError('cross-patient structural adjudication')
            if not p.get("rule_version") or not p.get("adjudicator_id") or not p.get("rationale"):
                raise ValueError("adjudication audit fields required")
            if p["action"] not in {"MERGE", "SPLIT", "MOVE_BOUNDARY", "REBUILD_EVIDENCE", "OFF_SCOPE", "NO_CHANGE", "UNRESOLVED"}:
                raise ValueError("unknown structural action")
            if not isinstance(p.get("confirmed_error"), bool): raise ValueError("confirmed error must be explicit")
            roots = set().union(*(set(packets[v]['root_proposals']) for v in parents))
            errors = set(p.get('error_root_proposals', []))
            if not errors <= roots or bool(errors) != p['confirmed_error']:
                raise ValueError('confirmed errors must identify specific original proposals')
            if "clinical_reference" in p: raise ValueError("structural adjudication cannot supply clinical Reference")
            return
        if kind not in {"GATE", "REFERENCE"}: raise ValueError("unknown workflow event")
        version = p["version_id"]
        if version not in packets: raise ValueError("unknown packet version")
        packet = packets[version]; reviewer = p["reviewer_id"]
        if p["packet_sha256"] != packet["packet_sha256"]: raise ValueError("review bound to wrong packet")
        if any(e["patient_alias"] == packet["patient_alias"] and e["reviewer_id"] == reviewer for e in exposures):
            raise ValueError("reviewer exposed to research/structural material")
        ancestors, todo = set(), list(packet.get("parent_versions", []))
        while todo:
            v = todo.pop()
            if v not in ancestors:
                ancestors.add(v); todo.extend(packets[v].get("parent_versions", []))
        if any(g["reviewer_id"] == reviewer and g["version_id"] in ancestors for g in gates):
            raise ValueError("repaired packet requires another blinded reviewer")
        for g in gates:
            prior = packets[g["version_id"]]
            if g["reviewer_id"] == reviewer and prior["patient_alias"] == packet["patient_alias"] and _time(prior["as_of"]) > _time(packet["as_of"]):
                raise ValueError("reviewer already saw later patient state")
        if kind == "GATE":
            if any(g["version_id"] == version and g["reviewer_id"] == reviewer for g in gates):
                raise ValueError("gate response is immutable")
            if p["decision"] not in {"VALID", "REPAIR_REQUIRED", "UNCERTAIN"}: raise ValueError("invalid gate decision")
            if not set(p.get("issues", [])) <= ISSUES: raise ValueError("unknown gate issue")
            if (p["decision"] == "VALID") != (not p.get("issues")): raise ValueError("gate decision and issues disagree")
            if p['decision'] != 'VALID' and not (p.get('rationale') or p.get('source_refs')):
                raise ValueError('flagged gate needs an auditable explanation or source reference')
            if "clinical_reference" in p: raise ValueError("validity gate cannot include clinical answers")
        else:
            if any(version in a['parent_versions'] for a in adjudications):
                raise ValueError('structurally adjudicated parent version cannot receive new clinical scores')
            if gate_status(version, gates, required) != "VALID": raise ValueError("clinical form blocked by validity gate")
            if not any(g["version_id"] == version and g["reviewer_id"] == reviewer and g["decision"] == "VALID" for g in gates):
                raise ValueError("clinical reviewer must first complete validity gate")
            if any(r["version_id"] == version and r["reviewer_id"] == reviewer for r in refs):
                raise ValueError("clinical response is immutable")
            Draft202012Validator(p["form_schema"]).validate(p["clinical_reference"])
            if not p.get("form_version") or not p.get("form_sha256") or digest(p["form_schema"]) != p["form_sha256"]:
                raise ValueError("clinical form must be versioned and hash-bound")
            if p["form_version"] != packet["reference_form_version"] or p["form_sha256"] != packet["reference_form_sha256"]:
                raise ValueError("clinical form differs from pre-registered packet form")

    def append(self, kind, payload):
        json.dumps(payload, allow_nan=False)
        if "duration_seconds" in payload and (not isinstance(payload["duration_seconds"], (int, float))
                or not math.isfinite(payload["duration_seconds"]) or payload["duration_seconds"] < 0):
            raise ValueError("invalid human effort duration")
        with self.path.open("a+", encoding="utf-8") as f:
            os.chmod(self.path, 0o600); fcntl.flock(f, fcntl.LOCK_EX); f.seek(0)
            rows = [json.loads(line) for line in f if line.strip()]; self.verify(rows)
            if rows and rows[0].get("required_valid_reviewers") != self.required:
                raise ValueError("review policy cannot change within ledger")
            self.validate(kind, payload, rows, self.required)
            row = {"event_id": uuid4().hex, "kind": kind, "payload": payload,
                   "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                   "required_valid_reviewers": self.required, "previous_hash": rows[-1]["hash"] if rows else None}
            row["hash"] = digest(row)
            f.seek(0, 2); f.write(json.dumps(row, ensure_ascii=False)+"\n"); f.flush(); os.fsync(f.fileno())
            return row

    def read(self):
        if not self.path.exists(): return []
        with self.path.open() as f:
            fcntl.flock(f, fcntl.LOCK_SH); rows = [json.loads(line) for line in f if line.strip()]
        self.verify(rows); return rows


def summarize(rows, required=None):
    policy = rows[0]['required_valid_reviewers'] if rows else 2
    if required is not None and required != policy: raise ValueError('summary policy differs from registered review policy')
    required = policy
    packets, gates, adjudications, refs, exposures = state(rows)
    initial = [p for p in packets.values() if not p.get("parent_versions")]
    valid = sum(gate_status(p["version_id"], gates, required) == "VALID" for p in initial)
    flagged = sum(gate_status(p["version_id"], gates, required) == "FLAGGED" for p in initial)
    reviewed = valid+flagged; roots = {r for p in initial for r in p["root_proposals"]}
    corrected = set().union(*(set(a['error_root_proposals']) for a in adjudications if a['confirmed_error'])) if adjudications else set()
    affected = set().union(*(set(packets[v]['root_proposals']) for a in adjudications for v in a['parent_versions'])) if adjudications else set()
    retired = {v for a in adjudications for v in a['parent_versions']}
    durations = [g["duration_seconds"] for g in gates if "duration_seconds" in g]
    active = [r for r in refs if r['version_id'] not in retired and gate_status(r["version_id"], gates, required) == "VALID"
              and not any(e['_sequence'] < r['_sequence'] and e["reviewer_id"] == r["reviewer_id"]
                          and e["patient_alias"] == packets[r["version_id"]]["patient_alias"] for e in exposures)]
    return {"initial_proposals": len(initial), "initial_valid": valid, "initial_flagged": flagged,
            "initial_pending": len(initial)-reviewed,
            "first_pass_valid_over_all_proposed": valid/len(initial) if initial else None,
            "first_pass_valid_over_reviewed": valid/reviewed if reviewed else None,
            "confirmed_error_original_proposals": len(corrected & roots),
            "structurally_affected_original_proposals": len(affected & roots),
            "confirmed_error_rate": len(corrected & roots)/len(roots) if roots else None,
            "packet_versions": len(packets), "structural_actions": len(adjudications),
            "median_reported_gate_seconds": statistics.median(durations) if durations else None,
            "timed_gate_responses": len(durations), "gate_responses": len(gates),
            "clinical_responses_recorded": len(refs), "clinical_responses_currently_active": len(active),
            "clinical_responses_quarantined": len(refs)-len(active)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ledger',type=Path,required=True)
    p.add_argument('--event',type=Path,help='one JSON object with kind and payload; no file is rewritten')
    p.add_argument('--required-valid-reviewers',type=int,default=2)
    p.add_argument('--summary',type=Path)
    a=p.parse_args(); ledger=ReviewLedger(a.ledger,required_valid_reviewers=a.required_valid_reviewers)
    if a.event:
        event=json.loads(a.event.read_text());ledger.append(event['kind'],event['payload'])
    result=summarize(ledger.read())
    if a.summary:
        repo=Path(__file__).resolve().parents[3]
        if not any(a.summary.resolve().is_relative_to(repo/d) for d in ['private','outputs']):p.error('summary must remain private')
        with a.summary.open('x') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
