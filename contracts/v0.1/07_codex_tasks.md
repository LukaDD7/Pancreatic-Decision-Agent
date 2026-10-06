# Codex Implementation Tasks v0.1

## Rule 0
Read `00_README.md`, `01_algorithm_contract.md`, `02_definitions.md`, and `03_invariants.md` before writing code.
Do not change contract semantics to fit existing code.

## Milestone 1 — Dataset adapters
Implement:
- `load_t0_states(path) -> list[CaseState]`
- `load_t1_candidates(path) -> dict[case_id, list[T1Event]]`
- `load_outcomes(path) -> dict[case_id, Outcome]` (evaluation module only)

Acceptance tests:
- T0 adapter cannot access outcome object.
- all 100 case ids resolve.
- source ids referenced by a state exist in that state.

## Milestone 2 — Canonical types
Implement typed models/enums for:
- DecisionGap
- AgentMode
- CanonicalAction
- AgentOutput
- T1Event
- MatchLevel
- EpisodeStatus

Use the JSON schema as external validation.

## Milestone 3 — Policy interface
```python
class SequentialPolicy(Protocol):
    def decide(self, state: CaseState, step: int) -> AgentOutput: ...
```

Provide:
- `OneShotPolicy`
- `StateAwarePolicy`
- `AlwaysContinuePolicy`
- `StaticRulePolicy`

The first two may call an LLM; the latter two must be deterministic.

## Milestone 4 — Output validator
Checks:
- strict schema
- source id validity
- exactly one canonical action
- PAUSE must point to an existing gap
- terminal mode/action consistency
- no unsupported source ids

On violation: record failure + DEFER; never silently patch the clinical action.

## Milestone 5 — Offline environment
Implement:
```python
class LoggedReplayEnvironment:
    def transition(self, case_id, state, action) -> TransitionResult:
        ...
```

Possible status:
- `OBSERVED_EXACT`
- `OBSERVED_COMPATIBLE`
- `COUNTERFACTUAL_UNOBSERVED`
- `TERMINAL`
- `INVALID_ACTION`

No result synthesis.

## Milestone 6 — Event/action matcher
Initial deterministic mappings:
- staging_laparoscopy_result -> STAGING_LAPAROSCOPY
- verified PET-CT -> PET_CT
- CT -> RESTAGING_CT when clinically/time compatible
- MR -> OTHER_CROSS_SECTIONAL_MR by default; upgrade to LIVER_MRI only when liver-targeting is explicit/adjudicated
- biopsy/FNA -> BIOPSY_FNA when target site/question matches
- MDT -> MDT_REVIEW
- open exploration -> never a diagnostic match

Keep mapping rules in a versioned config file, not hidden in prompts.

## Milestone 7 — Episode runner
Pseudo:
```python
state = t0_state
for step in range(0, 3):
    out = policy.decide(state, step)
    validate(out)
    if terminal(out): break
    tr = env.transition(case_id, state, out.action)
    log(tr)
    if tr.status == COUNTERFACTUAL_UNOBSERVED: break
    state = update_state(state, tr.observation)
```

Store full trace as JSONL.

## Milestone 8 — Repeated-run consistency
Run each T0 policy 5 times.
Compute mode/action consistency and per-threshold coverage.

## Milestone 9 — Evaluation
Implement metrics without using outcomes inside policy execution:
- failure interception
- over-escalation
- prospective action appropriateness
- preventability utility
- target-gap agreement
- source grounding
- logged replay gap-resolution
- consistency/coverage curves

Reference ratings are separate annotation files.

## Milestone 10 — Characterization tests
Before changing any data logic, write regression tests for:
1. one failed-interception liver case;
2. one failed-interception peritoneal case;
3. one completed-resection hard negative;
4. one staging-laparoscopy successful interception;
5. one context-dependent resection+distant-lesion case;
6. one case with date-only/uncertain T0.

The tests should assert temporal boundaries and action/event matching, not expected LLM wording.

## Required run manifest
Every experiment must log:
- dataset version/hash
- policy version
- model id
- prompt version/hash
- temperature/top_p/seed
- consistency repetitions
- action mapping version
- evaluation rubric version
- case ids used

## Do not implement yet
- RL/offline policy optimization
- synthetic counterfactual test-result generation
- outcome-conditioned prompts
- automatic gold action generation from actual T1
