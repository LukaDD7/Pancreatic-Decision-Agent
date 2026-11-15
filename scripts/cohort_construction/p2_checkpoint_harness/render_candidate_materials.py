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


def render_candidate_materials(*, case_result, source_bundle, output):
    from ..p2_auto_checkpoint.fhir_io import import_sources

    raw = json.loads(Path(case_result).read_text(encoding="utf-8"))
    bundle = json.loads(Path(source_bundle).read_text(encoding="utf-8"))
    sources, _ = import_sources(bundle)
    diagnostic_events = build_diagnostic_events(sources, raw["evidence_inventory"])
    projection = project_candidate_checkpoints(
        case_id=raw["case_id"], candidates=raw["candidate_spans"], sources=sources,
        diagnostic_events=diagnostic_events)
    material = project_candidate_materials(
        case_id=raw["case_id"], sources=sources, candidates=raw["candidate_spans"],
        projection=projection, relations=raw["relations"]["recorded"],
        episodes=raw["episodes"]["episodes"])
    material["input_provenance"] = dict(
        case_result=str(case_result),
        case_result_sha256=hashlib.sha256(Path(case_result).read_bytes()).hexdigest(),
        source_bundle=str(source_bundle),
        source_bundle_sha256=hashlib.sha256(Path(source_bundle).read_bytes()).hexdigest())
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
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(render_candidate_materials(
        case_result=args.case_result, source_bundle=args.sources, output=args.output),
        ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
