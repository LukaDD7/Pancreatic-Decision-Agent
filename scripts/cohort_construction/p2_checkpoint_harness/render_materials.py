"""Offline CLI: project frozen candidate results into a private readable preview.

Example (from the repository root, with PYTHONPATH set):
  python -m scripts.cohort_construction.p2_checkpoint_harness.render_materials \
    --case-result RUN/case_result.json --sources CASE/source_fhir_r4_bundle.json \
    --output NEW_DIRECTORY

No provider or external service is contacted. Existing directories and frozen
runs are never overwritten. The result is not a clinician blinded package.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .stages.materials import project_materials, write_material_set
from .stages.materials_html import write_html


def render_case_materials(*, case_result, source_bundle, output):
    from ..p2_auto_checkpoint.fhir_io import import_sources

    raw = json.loads(Path(case_result).read_text(encoding="utf-8"))
    bundle = json.loads(Path(source_bundle).read_text(encoding="utf-8"))
    sources, _ = import_sources(bundle)
    material = project_materials(
        case_id=raw["case_id"], sources=sources,
        candidates=raw["candidate_spans"], evidence_inventory=raw["evidence_inventory"],
        relations=raw["relations"]["recorded"], episodes=raw["episodes"]["episodes"])
    output = Path(output)
    if output.exists():
        raise FileExistsError("MATERIAL_OUTPUT_ALREADY_EXISTS:" + str(output))
    output.mkdir(mode=0o700, parents=True)
    manifest = write_material_set(material, output)
    return write_html(material, manifest, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-result", type=Path, required=True)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(render_case_materials(case_result=args.case_result,
                                            source_bundle=args.sources,
                                            output=args.output),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
