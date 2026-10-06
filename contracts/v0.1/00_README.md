# Pancreatic Sequential Decision Agent — Contract Pack v0.1

Date: 2026-10-06
Status: research prototype / retrospective offline evaluation only

## Research question
Can a state-aware agent determine whether a patient on a planned curative pancreatic-surgery pathway should (1) continue without additional staging, (2) pause and acquire one targeted piece of evidence, or (3) exit the curative pathway because current evidence already establishes a contraindicating state — and can it do so with higher clinical utility than static rules or a one-shot LLM?

This is **not** primarily a metastasis classifier. The object of study is a sequential decision policy under incomplete and potentially conflicting evidence.

## Contract philosophy
This pack follows a `Spec -> Agent -> Oracle -> Audit` workflow.

- Human-authored contracts own the semantics.
- Codex implements the contracts; it must not silently redefine states, actions, labels, or evaluation rules.
- Post-T0 outcomes are an oracle/evaluation source, never normal T0 model input.
- Logged retrospective trajectories do not provide outcomes for unobserved counterfactual actions.
- Unknown, absent, suspected, probable and confirmed are distinct states.

## Files
- `01_algorithm_contract.md`: executable research/algorithm contract.
- `02_definitions.md`: terminology and label definitions.
- `03_invariants.md`: non-negotiable data and algorithm invariants.
- `04_pilot_plan_100_cases.md`: how to use the current 100-case package.
- `05_action_utility_rubric.md`: two-stage expert adjudication and counterfactual utility rubric.
- `06_agent_output.schema.json`: strict machine-readable output schema.
- `07_codex_tasks.md`: recommended implementation order and acceptance tests.

## Literature design anchors
The design borrows *algorithmic principles*, not clinical labels, from:

1. Tu et al./AMIE multimodal state-aware reasoning, Nature Medicine 2026, DOI: 10.1038/s41591-026-04371-0 — persistent patient state, prioritized knowledge gaps, phase transition driven by evolving uncertainty.
2. Ferber et al., MIRA, Nature 2026, DOI: 10.1038/s41586-026-10675-5 — typed tools/actions, interleaved observation-action traces, explicit planning.
3. Liévin et al., AMIE disease management, Nature 2026, DOI: 10.1038/s41586-026-10764-5 — persistent multi-visit state, structured constrained plans, guideline-grounded management reasoning.
4. Zhang et al., on-premise medical AI agents, Nature Medicine 2026, DOI: 10.1038/s41591-026-04609-x — decision-time behavioral consistency and selective deferral.
5. Liu et al., MoChiAgent, Nature Medicine 2026, DOI: 10.1038/s41591-026-04694-y — LLM orchestrator separated from quantitative/predictive tools; sequential tool routing and traceable evidence.

## Current 100-case package boundaries
- `patient_states_100_pre_t0.jsonl` is the allowed T0 state source for the first pilot.
- `patient_source_bundles_100.jsonl` contains post-T0 information and MUST NOT be passed wholesale to the T0 agent.
- Current T0 is an **anchor** based mainly on the first strong suspicious distant-metastasis signal, not necessarily the final documented pre-incision surgical decision time.
- Imaging availability currently uses examination time as a proxy because report sign/review timestamps are unavailable.
- Post-T0 pathology and follow-up are evaluation sources only.
