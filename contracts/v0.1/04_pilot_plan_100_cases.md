# 100-case Pilot Plan v0.1

## 1. What the current 100 cases can support

Current package characteristics:
- 100 cases with pre-T0 structured states.
- 4 observed trajectory groups:
  - 45: suspicious metastasis -> continued curative resection.
  - 26: suspicious metastasis -> operation continued but curative resection downgraded/abandoned.
  - 26: curative resection + synchronous distant-lesion treatment.
  - 3: planned curative surgery -> staging laparoscopy -> curative resection cancelled.
- 85/100 have at least one post-T0 candidate evidence event.
- 76/100 have exact T0 plus at least one T1 candidate.
- 30 are A-tier cases with potentially decision-changing post-T0 evidence.
- 46 are B-tier cases mainly with post-T0 imaging candidates.
- 24 are C-tier cases with no T1 candidate or date-only T0.

The package is ideal for a **phase-0/phase-1 algorithm test**, not for training or for estimating clinical prevalence.

## 2. Primary vs exploratory strata

### Primary readiness candidate cohort (74 cases before adjudication)
Include:
- 45 continued single curative resection.
- 26 surgery continued but downgraded/abandoned.
- 3 staging-laparoscopy interceptions.

These provide the clearest contrast between continuing, missed interception and successful interception.

Important: the 45 “continued” cases are only candidate negatives until T0-blinded review confirms that no additional staging was clinically warranted.

### Context-dependent exploratory cohort (26 cases)
`curative resection + synchronous distant-lesion treatment`.

Keep for qualitative/action-specific analyses. Do not force into a binary `should operate / should not operate` label without disease-specific review.

## 3. Pilot phases

### Phase P0 — Contract and parser smoke test (all 100)
Goal: verify the algorithm implementation, not clinical performance.

For each case:
1. Load only `patient_states_100_pre_t0.jsonl`.
2. Run the policy once deterministically and 5 times for consistency analysis.
3. Validate JSON schema.
4. Validate source ids.
5. Validate that every PAUSE action targets a named gap.
6. Record action distribution and parser/defer failures.

Deliverables:
- `policy_outputs_t0.jsonl`
- `consistency_summary.csv`
- `contract_violations.csv`

### Phase P1 — Reference adjudication (recommended first 50)
Use the existing audit strategy:
- all 30 A-tier cases;
- 20 stratified B-tier cases.

Reviewer pass A — outcome blinded:
- identify top 1–3 T0 DecisionGaps;
- rate `CONTINUE`, `PAUSE` candidate actions and `EXIT` for prospective appropriateness;
- mark whether additional evidence is clinically warranted.

Reviewer pass B — outcome unblinded:
- identify actual failure/endpoint mechanism;
- for each canonical action, rate preventability/detection plausibility;
- identify which actions are Level-high, Level-medium, Level-low or none.

After duplicate/event audit, target 40–45 formal episodes.

### Phase P2 — Static policy evaluation
Evaluate T0 decisions without revealing T1.

Compare:
- Always-Continue.
- Static rule/checklist.
- One-shot LLM.
- State-only LLM.
- Full agent first action.

Primary reporting:
- failure interception rate.
- over-escalation rate.
- T0 prospective appropriateness.
- target-gap accuracy.
- behavioral consistency.

### Phase P3 — Logged sequential replay
Candidate pool:
- 57 cases in the primary 74-case cohort currently have exact T0 + at least one T1 candidate.
- formal replay should use only cases whose T1 has passed event-level audit.

Procedure:
1. Agent acts at T0.
2. If action matches an audited T1: reveal that T1 only.
3. Rebuild state.
4. Agent makes second action/terminal decision.
5. Maximum 2 evidence-acquisition steps.
6. If action does not match an observed T1: mark counterfactual-unobserved and stop replay.

Key metric:
Does newly revealed evidence resolve the gap the agent claimed it was targeting?

### Phase P4 — Outcome-informed action utility
For unmatched counterfactual actions, do not simulate results.

Use expert outcome-informed preventability ratings to ask:
- was the proposed modality mechanistically capable of detecting the eventual failure?
- was it appropriate based on T0 evidence?
- would it reasonably have been available before the irreversible intervention?

This is where `liver MRI for eventual liver metastasis` may be high utility, but not automatically. Lesion size, location (surface vs deep), pre-T0 imaging signal and timing all matter.

## 4. Recommended v0.1 action hierarchy

Do not hard-code a global ranking such as `MRI > SL > PET`.

Use a target-specific matrix:

### Liver uncertainty
Candidates: liver MRI, restaging CT, PET-CT, staging laparoscopy (especially surface disease), biopsy/FNA, expert review.

### Peritoneal/omental uncertainty
Candidates: staging laparoscopy, targeted biopsy if feasible, expert imaging review, selected cross-sectional staging.

### Lung/pleural uncertainty
Candidates: chest CT/restaging imaging, PET-CT, biopsy when management-changing.

### Local resectability uncertainty
Candidates: expert pancreatic radiology review, dedicated cross-sectional restaging, vascular/surgical MDT review.

The reference is per-case utility, not a universal modality ranking.

## 5. Minimal baselines

### Rule baseline 1 — signal severity
- confirmed contraindicating evidence -> exit/confirm.
- unresolved strong suspicious signal -> pause.
- otherwise continue.

### Rule baseline 2 — site-targeted simple policy
Map the current strongest unresolved site to one canonical test using a fixed hand-written table.

### One-shot LLM
Give the same T0 evidence, ask for one of the terminal modes and optional one test. No explicit state/gap controller.

The sequential agent must beat these simple baselines to justify agentic complexity.

## 6. Evaluation without prevalence claims

Because the cohort is enriched:
- report macro/stratum-level results;
- show confusion/utility separately for failed interception, successful interception, proceed candidates and context-dependent cases;
- do not claim real-world sensitivity/specificity until a consecutive cohort is assembled.

## 7. Most informative initial experiments

### E1 State-aware ablation
Full explicit gap/state controller vs one-shot same-model baseline.
Question: does explicit state/gap control improve clinically appropriate PAUSE/CONTINUE decisions?

### E2 Sequential-update ablation
T0-only vs T0 -> matched T1 -> updated decision.
Question: does the policy correctly use new evidence rather than merely repeat its initial conclusion?

### E3 Reliability gate
5 stochastic runs/case; measure action consistency and selective coverage.
Question: are errors concentrated in behaviorally unstable cases, allowing meaningful defer-to-expert gating?

### E4 Counterfactual utility
For failed-interception cases, score the agent's proposed action against the post-outcome mechanism-specific utility matrix.
Question: does the agent propose a clinically plausible interception action even when that action was not historically performed?

### E5 Hard-negative analysis
Focus on completed-resection cases that had similarly strong preoperative suspicious language.
Question: can the agent avoid the trivial strategy “suspicious wording => stop surgery”?

## 8. What success would mean in this pilot

A useful signal would be:
- higher failure interception than one-shot/static baselines;
- no large rise in unnecessary pauses among hard negatives;
- higher action utility on failed-interception cases;
- meaningful improvement after matched T1 updates;
- errors enriched in low-consistency cases so a reliability gate has value.

The pilot does not need to prove clinical benefit. It needs to establish that the sequential decision formulation is coherent, falsifiable and empirically distinguishable from simpler alternatives.
