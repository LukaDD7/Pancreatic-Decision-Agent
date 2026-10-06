# Invariants v0.1

These are hard constraints. Codex must treat violations as test failures, not implementation choices.

## A. Temporal / leakage invariants

1. **No future leakage**: normal T0 inference may only use information whose allowed timestamp is <= T0 according to the temporal policy.
2. `patient_source_bundles_100.jsonl` MUST NOT be passed wholesale to the T0 model because it contains post-T0 data.
3. `patient_outcomes_100_post_t0.jsonl`, T1 candidate files, follow-up, final pathology and operative findings are oracle/evaluation data only.
4. Post-hoc `disease_stratum` is analysis-only unless that disease identity was independently established in pre-T0 evidence.
5. Same-day date-only pathology is excluded at T0 unless ordering/availability is unambiguously pre-T0.
6. Imaging examination time is a proxy for availability, not proof that the report was signed/read at that time. Preserve the `decision_timepoint_basis` flag in every evaluation.

## B. Epistemic invariants

7. `unknown != negative`.
8. `possible/suspicious != confirmed positive`.
9. Absence of mention does not automatically become a negative finding.
10. Conflicting evidence must remain conflicting until a valid resolution event occurs.
11. Every material clinical claim in agent output must cite at least one source id from the currently allowed state.
12. An output may not cite a source id that is absent from the input evidence set.

## C. Sequential-decision invariants

13. The agent chooses **one next action per step**. No unordered bundles of tests.
14. Every evidence-acquisition action must name a specific `DecisionGap` it is intended to resolve.
15. Maximum evidence-acquisition depth for v0.1 is 2.
16. Once a terminal action is emitted, the episode stops.
17. `open_exploration_result` cannot be used as if it were a preoperative diagnostic test. It is an endpoint/failure observation.
18. Actual historical T1 action is not the gold action merely because it occurred.
19. If the agent requests an unobserved counterfactual action, the environment MUST NOT generate/simulate a clinical result. Mark `COUNTERFACTUAL_UNOBSERVED`.
20. A logged T1 may be revealed only after compatibility checks and timestamp validation.

## D. Clinical-outcome invariants

21. Long-term postoperative M1 does not prove M1 was present at the index operation.
22. Final pathology cannot retroactively turn a prospectively reasonable decision into an error by itself.
23. A completed resection is not automatically a correct decision; a downgraded operation is not automatically avoidable. Both require adjudication.
24. Distant disease does not have identical surgical implications across pancreatic disease types; context-dependent cases require disease-specific review.
25. The 26 current `resection + synchronous distant-lesion treatment` trajectories are excluded from the primary binary readiness endpoint until adjudicated.

## E. Evaluation invariants

26. This 100-case cohort is enriched; do not report prevalence, PPV/NPV or overall accuracy as if it were a consecutive clinical population.
27. Report stratified performance by trajectory class.
28. Do not tune a consistency threshold and claim performance on the same cases without labeling it exploratory. Prespecify/report multiple gates in v0.1.
29. No single exact-match “best test” label is assumed. Reference actions are utility-valued/set-valued.
30. Prospective appropriateness must be rated with outcome blinded; preventability is rated only after outcome unblinding.

## F. Software / contract invariants

31. The JSON output schema is strict. Unknown enum values fail validation.
32. The implementation must preserve raw model output separately from parsed output for audit.
33. A parser failure is not silently repaired into a clinical recommendation; it is recorded as a system failure/defer.
34. All policy versions, prompts, model versions, temperatures and seeds must be logged.
35. Specs and tests are authoritative. Codex must not modify contract files or weaken tests merely to make CI pass.
36. Any discovered conflict between source data and the contract must be surfaced in an audit report, not silently resolved.
