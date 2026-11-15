"""Offline candidate-CP reprojection and private HTML preview; no provider calls."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .stages.candidate_checkpoints import project_candidate_checkpoints
from .stages.candidate_materials import (
    project_candidate_materials, write_candidate_material_set,
)
from .stages.candidate_materials_html import write_html
from .stages.diagnostic_events import build_diagnostic_events


def _stage_items(raw, stage, key):
    return (raw.get(stage) or {}).get(key) or []


def _organization_items(raw, key):
    if isinstance(raw, list):
        return raw
    value = raw.get(key)
    if value is None and key == "relations":
        value = raw.get("recorded")
    if not isinstance(value, list):
        raise ValueError("CANDIDATE_ORGANIZATION_LIST_REQUIRED:" + key)
    return value


def render_candidate_materials(*, case_result, source_bundle, output,
                               candidate_relations=None, candidate_episodes=None):
    from ..p2_auto_checkpoint.fhir_io import import_sources

    raw = json.loads(Path(case_result).read_text(encoding="utf-8"))
    bundle = json.loads(Path(source_bundle).read_text(encoding="utf-8"))
    sources, _ = import_sources(bundle)
    diagnostic_events = build_diagnostic_events(sources, raw["evidence_inventory"])
    projection = project_candidate_checkpoints(
        case_id=raw["case_id"], candidates=raw["candidate_spans"], sources=sources,
        diagnostic_events=diagnostic_events)
    cp_relations = cp_episodes = None
    if candidate_relations is not None or candidate_episodes is not None:
        if candidate_relations is None or candidate_episodes is None:
            raise ValueError("CANDIDATE_RELATIONS_AND_EPISODES_REQUIRED_TOGETHER")
        cp_relations = _organization_items(
            json.loads(Path(candidate_relations).read_text(encoding="utf-8")), "relations")
        cp_episodes = _organization_items(
            json.loads(Path(candidate_episodes).read_text(encoding="utf-8")), "episodes")
    material = project_candidate_materials(
        case_id=raw["case_id"], sources=sources, candidates=raw["candidate_spans"],
        projection=projection,
        relations=_stage_items(raw, "relations", "recorded"),
        episodes=_stage_items(raw, "episodes", "episodes"),
        candidate_relations=cp_relations, candidate_episodes=cp_episodes)
    material["input_provenance"] = dict(
        case_result=str(case_result),
        case_result_sha256=hashlib.sha256(Path(case_result).read_bytes()).hexdigest(),
        source_bundle=str(source_bundle),
        source_bundle_sha256=hashlib.sha256(Path(source_bundle).read_bytes()).hexdigest(),
        candidate_relations=(dict(
            path=str(candidate_relations),
            sha256=hashlib.sha256(Path(candidate_relations).read_bytes()).hexdigest())
            if candidate_relations else None),
        candidate_episodes=(dict(
            path=str(candidate_episodes),
            sha256=hashlib.sha256(Path(candidate_episodes).read_bytes()).hexdigest())
            if candidate_episodes else None))
    output = Path(output)
    if output.exists():
        raise FileExistsError("MATERIAL_OUTPUT_ALREADY_EXISTS:" + str(output))
    output.mkdir(mode=0o700, parents=True)
    manifest = write_candidate_material_set(material, output)
    result = write_html(material, manifest, output)
    result["summary"] = material["summary"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-result", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--candidate-relations", type=Path)
    parser.add_argument("--candidate-episodes", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(render_candidate_materials(
        case_result=args.case_result, source_bundle=args.sources, output=args.output,
        candidate_relations=args.candidate_relations,
        candidate_episodes=args.candidate_episodes),
        ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
