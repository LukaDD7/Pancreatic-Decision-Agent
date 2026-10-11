"""Phase A lineage and replay: fictional sources, no network or patient data."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pancreatic_agent import phase_a
from pancreatic_agent.evaluation.client import Completion, ModelSettings
from pancreatic_agent.evaluation import runner
from pancreatic_agent.four_stage import save, sha

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    monkeypatch.setattr(phase_a, "protected_output", lambda p: Path(p).resolve())
    monkeypatch.setattr(runner, "protected_output", lambda p: Path(p).resolve())
    source = phase_a.read(ROOT / "examples/hewv_fictional_bundle.json")
    review = phase_a.read(ROOT / "examples/hewv_fictional_review.json")["S001"]
    record = phase_a.read(ROOT / "examples/phase_a_fictional_source_review.json")
    directory, manifest = phase_a.new_dataset(tmp_path / "dataset")
    manifest["cases"].append(phase_a.freeze_case(directory, "S001", source, review, record))
    phase_a.finish_dataset(directory, manifest)
    return directory, source, review, record


def test_frozen_source_and_review_rebuild_four_samples(frozen):
    directory, *_ = frozen
    manifest = phase_a.verify_dataset(directory)
    assert manifest["slots"] == manifest["runnable_samples"] == 4
    case = manifest["cases"][0]
    assert not Path(case["packet_path"]).is_absolute()
    assert len(case["samples"]) == 4
    assert all(s["source_review_record_sha256"] for s in case["samples"])


def test_changed_record_stops_before_model_call(frozen, tmp_path):
    directory, *_ = frozen
    path = directory / "cases/S001_E001/01_context.md"
    path.write_text("Injected later outcome")
    class NoCalls:
        def complete(self, *args, **kwargs):
            pytest.fail("Tampered dataset reached model")
    with pytest.raises(ValueError, match="artifact changed"):
        phase_a.run_dataset(directory, tmp_path / "runs", ModelSettings("https://example.invalid/v1", "fake"),
                            execute=True, client=NoCalls())


def test_unresolved_slots_remain_in_dataset_and_are_skipped(frozen, tmp_path):
    _, source, review, record = frozen
    review = deepcopy(review)
    review["checkpoints"][0]["source_ids"] = []
    record = {**record, "review_manifest_sha256": sha(review)}
    directory, manifest = phase_a.new_dataset(tmp_path / "unresolved")
    manifest["cases"].append(phase_a.freeze_case(directory, "S001", source, review, record))
    phase_a.finish_dataset(directory, manifest)
    batch, result = phase_a.run_dataset(directory, tmp_path / "runs",
                                        ModelSettings("https://example.invalid/v1", "fake"))
    assert manifest["slots"] == 4 and manifest["unresolved_samples"] == 1
    assert result["cases"][0]["summary"]["samples"] == 3
    run = batch / result["cases"][0]["run_path"]
    assert not (run / "requests/stage_01_repeat_01.json").exists()
    assert phase_a.read(run / "sample_lineage.json")["sample_ids"] == ["S001_E001_Q2", "S001_E001_Q3", "S001_E001_Q4"]


def test_invalid_output_stays_in_batch_and_does_not_become_pause(frozen, tmp_path):
    directory, *_ = frozen
    class BadAnswers:
        def complete(self, messages, on_attempt=None):
            assert len(messages) == 2
            return Completion("not JSON", "stop", {}, [{"attempt": 1}])
    batch, result = phase_a.run_dataset(directory, tmp_path / "runs",
                                        ModelSettings("https://example.invalid/v1", "fake"),
                                        execute=True, client=BadAnswers())
    assert result["status"] == "COMPLETED_WITH_INVALID_SAMPLES"
    summary = result["cases"][0]["summary"]
    assert summary["attempted_samples"] == 4 and summary["valid_samples"] == 0
    run = batch / result["cases"][0]["run_path"]
    row = phase_a.read(run / "results/stage_01_repeat_01.json")
    assert row["raw_output"] == "not JSON" and row["decision"] is None


def test_review_binding_and_historical_provenance_are_explicit(frozen):
    _, source, review, record = frozen
    with pytest.raises(ValueError, match="Source review record"):
        phase_a.validate_review_record({**record, "source_bundle_sha256": "0" * 64}, source, review)
    review = {**review, "reviewer": "Codex探索审阅"}
    historical = phase_a.historical_review_record(review)
    assert historical["model_id"] is historical["reviewed_at"] is None
    assert historical["transcript_available"] is False
    assert historical["independent_clinician_approved"] is False


def test_api_review_artifacts_are_saved_with_payload_hashes(frozen, tmp_path):
    _, source, review, record = frozen
    record = {**record, "reviewer_kind": "api_llm", "model_id": "fictional-review-model"}
    request = {"messages": [{"role": "user", "content": "Fictional review request"}]}
    response = {"review": "Fictional source review conclusion"}
    record["request_artifact"] = {"payload": request, "sha256": sha(request)}
    record["response_artifact"] = {"payload": response, "sha256": sha(response)}
    directory, manifest = phase_a.new_dataset(tmp_path / "api-review")
    manifest["cases"].append(phase_a.freeze_case(directory, "S001", source, review, record))
    phase_a.finish_dataset(directory, manifest)
    saved = phase_a.read(directory / "cases/S001_E001/source_review_record.json")
    assert saved["request_artifact"]["payload"] == request
    saved["response_artifact"]["payload"] = "Changed conclusion"
    with pytest.raises(ValueError, match="hashed request/response"):
        phase_a.validate_review_record(saved, source, review)


def test_modified_packet_even_with_new_file_hash_must_reconstruct(frozen):
    directory, *_ = frozen
    folder = directory / "cases/S001_E001"
    packets = phase_a.read(folder / "packets.json")
    packets["packets"][0]["question"] = "Injected answer in question"
    save(folder / "packets.json", packets)
    manifest = phase_a.read(directory / "dataset.json")
    manifest["cases"][0]["files_sha256"]["packets.json"] = sha((folder / "packets.json").read_text())
    save(directory / "dataset.json", manifest)
    with pytest.raises(ValueError, match="source and review reconstruction"):
        phase_a.verify_dataset(directory)
