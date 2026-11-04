"""Independent deterministic JS/Python comparison using only fictional forms.

Run as a module; a receipt records validity differences separately from labels.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from .. import ReviewValidationError, validate_questionnaire_response

BASE = Path(__file__).resolve().parent
JS = BASE.parents[1] / "p2_checkpoint_review_app" / "assets" / "reviewer.js"
NODE_SCRIPT = r"""
const api=require(process.argv[1]);
let raw='';process.stdin.on('data',d=>raw+=d);
process.stdin.on('end',()=>{
 const cases=JSON.parse(raw);
 const results=cases.map(caseJson=>{
   try{const c=api.parseJsonUnique(caseJson);
     if(c.special_number)c.response.item[0].answer[0].valueDecimal=Number(c.special_number);
     api.validateResponse(c.questionnaire,c.response);return {valid:true};}
   catch(e){return {valid:false,code:e.code||e.constructor.name};}
 });
 process.stdout.write(JSON.stringify(results));
});
"""


def _node():
    candidates = [os.environ.get("P2_REVIEW_NODE_BINARY"),
                  str(Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"),
                  shutil.which("node")]
    return next((candidate for candidate in candidates if candidate and Path(candidate).is_file()), None)


def _special_cases():
    q = {"resourceType": "Questionnaire", "url": "urn:synthetic:form", "version": "demo-1", "status": "active",
         "item": [{"linkId": "decimal", "type": "decimal"}]}
    r = {"resourceType": "QuestionnaireResponse", "questionnaire": "urn:synthetic:form|demo-1", "status": "completed",
         "item": [{"linkId": "decimal", "answer": [{"valueDecimal": 0}]}]}
    return [{"name": "nonfinite_" + token, "questionnaire": q, "response": r, "special_number": token}
            for token in ("NaN", "Infinity", "-Infinity")]


def run_comparison():
    node = _node()
    if not node:
        raise RuntimeError("NODE_RUNTIME_UNAVAILABLE")
    fixture_paths = [BASE / "fixtures" / name for name in ("cross_language_cases.json", "differential_mutations.json")]
    cases = [case for path in fixture_paths for case in json.loads(path.read_text())["cases"]] + _special_cases()
    # Preserve 1.0 / 1e0 lexical tokens through the transport. A plain JSON.parse
    # followed by JSON.stringify would erase that distinction before validation.
    raw_cases = [json.dumps(case, ensure_ascii=True, allow_nan=False) for case in cases]
    raw_number_path = BASE / "fixtures" / "raw_json_number_cases.json"
    raw_number_cases = json.loads(raw_number_path.read_text())["cases"]
    fixture_paths.append(raw_number_path)
    for case in raw_number_cases:
        raw_cases.append(case["raw_json"])
        cases.append({"name": "raw_" + case["name"], **json.loads(case["raw_json"])})
    proc = subprocess.run([node, "-e", NODE_SCRIPT, str(JS)], input=json.dumps(raw_cases, ensure_ascii=True, allow_nan=False),
                          text=True, capture_output=True, check=True, timeout=30)
    js_results = json.loads(proc.stdout)
    validity_differences, error_label_differences = [], []
    accepted = 0
    for case, js_result in zip(cases, js_results, strict=True):
        working = copy.deepcopy(case)
        if "special_number" in working:
            working["response"]["item"][0]["answer"][0]["valueDecimal"] = float(working["special_number"])
        try:
            validate_questionnaire_response(working["questionnaire"], working["response"])
            py_result = {"valid": True}
        except ReviewValidationError as error:
            py_result = {"valid": False, "code": error.code}
        accepted += py_result["valid"]
        diff = {"case": case["name"], "python": py_result, "javascript": js_result}
        if py_result["valid"] != js_result["valid"]:
            validity_differences.append(diff)
        elif not py_result["valid"] and py_result["code"] != js_result["code"]:
            error_label_differences.append(diff)
    return {"schema_version": "0.1.0", "synthetic_only": True, "random_seed": 20261028,
            "case_count": len(cases), "python_accepted": accepted, "python_rejected": len(cases) - accepted,
            "status": "PASS" if not validity_differences else "DIFFERENCES_FOUND",
            "validity_differences": validity_differences, "error_label_differences": error_label_differences,
            "source_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in [*fixture_paths, JS, BASE.parents[0] / "validation.py"]}}


if __name__ == "__main__":
    receipt = run_comparison()
    destination = BASE.parent / "differential_receipt_20261028.json"
    destination.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: receipt[key] for key in ("status", "case_count", "python_accepted", "python_rejected")}))
    print(json.dumps(receipt["validity_differences"], ensure_ascii=False))
    raise SystemExit(0 if receipt["status"] == "PASS" else 1)
