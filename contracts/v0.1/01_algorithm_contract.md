# Algorithm Contract v0.1

## 1. Intended research object

We evaluate an offline clinical decision policy, not a free-form consultant.

For case `i` at decision step `t`:

`S_it -> PI -> A_it -> (logged observation if available) -> S_i,t+1 -> ... -> terminal decision`

where:

- `S_it`: source-grounded patient decision state available at that time.
- `PI`: frozen agent policy.
- `A_it`: exactly one canonical action.
- environment transition: only a real, time-valid, audited logged event may be revealed.

No synthetic clinical result is generated for an unobserved action.

## 2. Episode anchor

### v0.1 current anchor
`T0_anchor = first strong suspicious distant-metastasis signal` from the current 100-case construction.

This is a dataset anchor, not automatically the final decision-to-operate timestamp.

### future preferred anchor
`T_surgery_lock = latest documented time at which the team commits to curative-intent surgery, before incision and before any newly acquired evidence used to reverse that decision.`

All claims in v0.1 must say `T0_anchor`, not imply `final preoperative decision point` unless separately verified.

## 3. State contract

The agent receives a `DecisionState` with five layers.

### 3.1 Evidence layer
Every material fact must have:
- value
- epistemic status: `present | unknown | conflicting`
- source id(s)
- source time(s)
- raw excerpt where available

### 3.2 Clinical state layer
Minimum domains:
- `disease_identity`: evidence available at T0 only; never use post-hoc analysis stratum as input.
- `anatomy`: tumor location/size, vascular relations, resectability evidence.
- `distant_disease`: site-specific status for liver, peritoneum/omentum, lung/pleura, adrenal, bone, other.
- `biology`: pathology if already available, CA19-9 and context, other relevant markers.
- `condition`: performance status, nutritional context, comorbidity, biliary drainage when available.
- `temporal_validity`: age/recency of key staging evidence and whether availability time is exact or proxied.

### 3.3 Uncertainty layer
The agent must construct a ranked list of `DecisionGap` objects.

Each gap contains:
- `gap_id`
- `domain`: `M_LIVER | M_PERITONEAL | M_LUNG | M_OTHER | LOCAL_RESECTABILITY | HISTOLOGY | FITNESS | DATA_FRESHNESS | OTHER`
- `current_status`: `UNKNOWN | POSSIBLE | PROBABLE | CONFIRMED_NEGATIVE | CONFIRMED_POSITIVE | CONFLICTING`
- `decision_consequence_if_true`
- `evidence_for[]`
- `evidence_against[]`
- `why_unresolved`
- `priority`: `LOW | MEDIUM | HIGH | CRITICAL`

The agent may not request a test unless it names the gap that test is intended to resolve.

### 3.4 Action-readiness layer
The agent assigns one mode:
- `CONTINUE_CURATIVE_PATH`
- `PAUSE_FOR_EVIDENCE`
- `EXIT_CURATIVE_PATH`
- `DEFER_TO_EXPERT` (reliability gate only)

Interpretation:
- `CONTINUE_CURATIVE_PATH`: current evidence does not justify additional staging before continuing the curative pathway.
- `PAUSE_FOR_EVIDENCE`: at least one unresolved, actionable gap could materially alter management and should be resolved first.
- `EXIT_CURATIVE_PATH`: current T0 evidence already establishes a state incompatible with the planned curative pathway under the applicable clinical context.
- `DEFER_TO_EXPERT`: policy behavior is not reliable enough to make an autonomous recommendation; this is not a clinical conclusion.

### 3.5 Trace layer
The agent outputs source-grounded reasons, but chain-of-thought is not stored. Store only concise structured rationale and provenance.

## 4. Canonical action space

Each step emits exactly one action.

### 4.1 Terminal actions
- `CONTINUE_NO_NEW_STAGING`
- `EXIT_CURATIVE_PATH`
- `DEFER_EXPERT_REVIEW`

### 4.2 Evidence-acquisition actions
- `LIVER_MRI`
- `STAGING_LAPAROSCOPY`
- `RESTAGING_CT`
- `OTHER_CROSS_SECTIONAL_MR`
- `PET_CT`
- `BIOPSY_FNA`
- `EXPERT_RADIOLOGY_REVIEW`
- `MDT_REVIEW`
- `OTHER_TARGETED_EVIDENCE`

Every evidence action must also specify:
- target gap
- target site
- expected management consequence if positive/negative
- expected information gain: `LOW | MEDIUM | HIGH`

Do not output bundles such as `MRI + PET + SL` at one step. Sequentiality requires one next-best action.

## 5. Policy loop

Pseudo-algorithm:

1. `S_t = build_state(allowed_evidence_up_to_t)`
2. `G_t = identify_and_rank_gaps(S_t)`
3. If `confirmed contraindicating evidence` under current context: choose `EXIT_CURATIVE_PATH`.
4. Else if no `HIGH/CRITICAL` actionable gap: choose `CONTINUE_NO_NEW_STAGING`.
5. Else rank candidate actions for the highest-priority gap using ordinal expected utility:
   - ability to resolve the named gap
   - probability that the result changes management (qualitative in v0.1)
   - procedural burden/invasiveness
   - delay/cost burden
   - redundancy with evidence already available
6. Choose one action.
7. Offline environment checks whether a compatible audited logged T1 exists.
   - if yes: reveal only that event, update state, continue.
   - if no: mark branch `COUNTERFACTUAL_UNOBSERVED`; do not fabricate a result; end replay and score the proposed action by post-review utility.
8. Maximum acquisition depth in v0.1: `2` actions.
9. End with a terminal mode.

## 6. Logged-transition contract

### 6.1 Valid T1 observation
A T1 event may be revealed only if:
- timestamp is strictly after T0 and within the configured window;
- source is audited as a real event, not a retrospective mention;
- it is not a resection specimen/final post-operative pathology result masquerading as a diagnostic test;
- the event class is compatible with the requested action;
- if site specificity matters, the event targets the same uncertainty domain/site.

### 6.2 Action-event compatibility
Three levels:
- `EXACT`: same canonical modality/action and relevant target.
- `COMPATIBLE`: broader/narrower real test plausibly resolves the same gap; requires adjudication rule or manual flag.
- `NO_MATCH`: different question or modality.

Examples:
- requested `STAGING_LAPAROSCOPY` + audited staging-laparoscopy result -> EXACT.
- requested `LIVER_MRI` + MR explicitly performed to characterize liver lesions -> EXACT/COMPATIBLE depending on report metadata.
- requested `LIVER_MRI` + generic pancreas MR with no liver characterization -> NO_MATCH unless manually adjudicated.
- requested diagnostic action + `open_exploration_result` -> NO_MATCH. Open exploration is an endpoint/failure event, not a preoperative evidence-acquisition action.

## 7. Reliability gate

Inspired by decision-time behavioral consistency evaluation, run each T0 policy call `N=5` times under a fixed sampling configuration.

Compute:
- `mode_consistency = max_count(mode)/5`
- `action_consistency = max_count(canonical_action)/5`

v0.1 should report performance across prespecified gates (for example 0.6, 0.8 and 1.0) rather than selecting the best threshold on the same 100 cases.

If the chosen gate is not met, output `DEFER_TO_EXPERT` for selective-policy analyses.

## 8. Primary comparators / ablations

Use identical T0 evidence for all systems.

- `B0 Always-Continue`: no extra evidence acquisition.
- `B1 Static clinical-rule/checklist`: deterministic guideline/risk-rule baseline where applicable.
- `B2 One-shot LLM`: same evidence, one terminal recommendation, no explicit state/gap loop.
- `B3 State-only`: explicit DecisionState + gaps, but no sequential T1 replay.
- `A Full sequential agent`: state + gap + one-action policy + logged T1 update + stop/defer gate.

Core ablation inspired by state-aware clinical agents:
`Full state-aware sequential agent` vs `same base model without explicit gap/state controller`.

## 9. Evaluation object — no single gold action

A case may have multiple clinically reasonable next actions. Therefore the reference is set-valued / utility-valued.

For each case and each canonical action `a`, store:
- `prospective_appropriateness_i(a)`: reviewer sees T0 only, 0–3.
- `outcome_informed_preventability_i(a)`: reviewer later sees the actual failure mechanism/outcome, 0–3.
- `burden_i(a)`: 0–2.

The system is not evaluated by exact match to a single test label.

## 10. Primary pilot metrics

Because the cohort is enriched and not prevalence-representative, do NOT make overall accuracy/PPV the headline.

Report by trajectory stratum:
1. `failure_interception_rate`: among adjudicated failed-interception cases, fraction not assigned `CONTINUE_NO_NEW_STAGING`.
2. `over_escalation_rate`: among adjudicated safe-to-continue cases, fraction assigned `PAUSE/EXIT` without adequate T0 justification.
3. `mean_prospective_action_appropriateness`.
4. `mean_preventability_score` among failure cases.
5. `target_gap_accuracy` against adjudicated key uncertainty.
6. `source_grounding_rate`.
7. `mode/action behavioral consistency`.
8. `logged_replay_resolution_rate`: when an action matches logged T1, fraction in which the new evidence resolves/changes the named gap as adjudicated.
9. selective-policy curves: interception vs coverage under consistency gates.

## 11. Non-goals for v0.1

- No RL training on 100 cases.
- No claim of optimal treatment policy.
- No synthetic MRI/SL/PET result generation.
- No causal claim that the proposed action would definitely have prevented the observed outcome.
- No use of postoperative disease label, final pathology or long-term follow-up as T0 input.
- No claim that `actual observed next test = gold next test`.
