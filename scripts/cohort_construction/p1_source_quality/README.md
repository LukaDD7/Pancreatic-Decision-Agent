# P1 source-quality algorithm releases

This directory contains exact code/configuration snapshots and aggregate release receipts. Patient records, identifiers, frozen cohort indexes and detailed audit outputs remain in the local `private/` directory and are not part of these releases.

- `p1_quality_v0.1`: initial source-representation screen; 32 synthetic tests.
- `p1_quality_v0.2`: targeted parser and review-triage fixes; 51 synthetic tests.
- `p1_quality_v0_3`: relevance/usability reconstruction from all P0; no complete-report gate and no default P2 gate. See its README and `docs/P1重新纳入规则与结果-v0.3-20261021.md`.
- `p1_quality_v0_3_1`: preserved prior patch release; known-defect regression coverage and three label fixes. See `docs/P1-v0.3.1-变更原因与回归覆盖-20261021.md` and `regression_guard/README.md`.
- `p1_quality_v0_3_2`: storage migration patch. It preserves v0.3.1 extraction modules byte-for-byte, reads current `private/` inputs, requires owner-only new outputs and new run IDs, and migrates all prior regression coverage. It does not redefine cohort membership, P2 eligibility or clinical Reference.

The original private release directories remain the research run locations. To execute these exported snapshots, supply the delivered batches and private frozen index explicitly. Write outputs to each release's ignored `results/` subdirectory; the exported version comparator expects this sibling layout. Both versions require Python 3.12 and PyArrow.

```bash
/opt/anaconda3/bin/python3.12 -m unittest discover -s scripts/cohort_construction/p1_source_quality/p1_quality_v0.2 -p 'test_*.py'
/opt/anaconda3/bin/python3.12 scripts/cohort_construction/p1_source_quality/p1_quality_v0.2/run_quality.py --data-root data/PDAC/batches --index private/p1_quality_v0.2/inputs/P0_index_snapshot.csv --output scripts/cohort_construction/p1_source_quality/p1_quality_v0.2/results
```

These are technical source-representation screens, not individual surgical recommendations. Clinical validation and Episode eligibility remain separate.

## 2026-10-21 adversarial audit

The current research definition emphasizes research relevance and record usability. Frozen v0.2's complete-report gate does not implement that definition. See `docs/P1规则对抗审计-v0.1-20261021.md` and `adversarial_audit_20261021/README.md` for reproducible counterexamples, aggregate rule exposure, and proposed changes. This audit does not change cohort membership or introduce a new screening release.

The later v0.3 release implements the reconstruction separately. v0.2 and its output remain frozen; do not run the old command above expecting the revised rules.
