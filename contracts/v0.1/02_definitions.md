# Definitions v0.1

## Temporal definitions

### T0_anchor
Current dataset anchor: first strong suspicious distant-metastasis signal used to construct the pre-T0 state.

### T_surgery_lock
Preferred future research anchor: final documented commitment to curative-intent surgery before incision and before newly acquired evidence that later changes the plan.

### T1
The first audited post-T0 evidence event that is relevant to a prespecified unresolved decision gap. T1 is an observed event, not automatically a best action.

### T_terminal
The first event establishing the observed pathway endpoint for the episode (for example completed resection, staging-laparoscopy cancellation, open exploration/aborted resection, or other adjudicated endpoint).

## State definitions

### DecisionState
A source-grounded representation of what was knowable at the current time, including clinical facts, epistemic status, provenance, unresolved gaps and temporal validity.

### DecisionGap
An unresolved question that could materially change whether the current curative pathway should continue or what evidence should be acquired next.

### Unknown
No adequate evidence is available. Unknown is not equivalent to negative.

### Conflicting
Two or more contemporaneously available sources support materially incompatible clinical interpretations.

### Suspected / possible
Evidence raises a clinically relevant possibility but does not establish the state as true.

### Confirmed positive / negative
A stronger epistemic state supported by the required source class/criteria. The pilot must not silently upgrade “possible” to “confirmed”.

## Trajectory definitions

### Appropriate proceed candidate
A case in which curative surgery was completed and no adjudicated preoperative unresolved gap would have justified delaying the pathway. This label requires review; completed surgery alone is not sufficient.

### Failed interception
A case that continued to operative exploration and then had curative resection abandoned/downgraded because a clinically consequential contraindicating condition was discovered.

### Successful interception
A planned curative pathway was stopped or redirected before definitive resection because additional evidence identified a contraindicating condition. Staging laparoscopy that discovers metastatic disease and cancels resection is a canonical example.

### Context-dependent surgery
Curative/local resection proceeds despite distant disease or another unusual condition because disease biology and surgical strategy permit it (for example selected disease contexts). These cases are excluded from the primary binary readiness endpoint unless separately adjudicated.

### Non-therapeutic laparotomy / exploration
Major operative exploration that does not deliver the planned curative resection because metastatic or locally unresectable disease is discovered. Terminology must follow source documentation and adjudication; do not infer from procedure name alone.

## Action definitions

### CONTINUE_NO_NEW_STAGING
Continue the curative pathway without acquiring an additional staging investigation specifically to resolve a current gap.

### PAUSE_FOR_EVIDENCE
Do not yet commit to the next irreversible step; acquire exactly one targeted evidence item intended to resolve a named gap.

### EXIT_CURATIVE_PATH
Current evidence is already sufficient to conclude that the planned curative pathway should not proceed as designed. This is distinct from “pause”.

### DEFER_TO_EXPERT
System reliability is inadequate for autonomous recommendation. This is an AI governance action, not a medical diagnosis.

### Action utility
How useful a proposed action is for the actual decision problem. It is not synonymous with whether the test eventually happened.

### Prospective appropriateness
Clinical reasonableness of an action judged using T0 information only, with reviewer blinded to later outcomes.

### Outcome-informed preventability
Plausibility that the action could have revealed/resolved the actual later failure mechanism before the irreversible intervention, judged after unblinding to outcome. This is not a causal guarantee.

### Level-high action
Operational shorthand for an action with high prospective appropriateness and high mechanism-specific preventability. It is not a gold-label synonym.

## Offline evaluation definitions

### Logged trajectory
The sequence of real observed events in the retrospective record.

### Logged replay
Reveal an observed T1 only when it is compatible with the agent’s requested action. The agent can then update its state.

### Counterfactual-unobserved branch
The agent requests an action that was not observed in the historical record. No result is simulated. The branch is evaluated only at the action-utility level.

### Exact action match
Agent action and logged event have the same canonical class and clinically relevant target.

### Compatible action match
Not identical in naming but plausibly answers the same gap; requires explicit mapping/adjudication.

### No match
Logged event does not answer the requested gap or occurs only after the irreversible endpoint.

## Evaluation definitions

### Failure interception rate
Proportion of adjudicated failed-interception cases in which the agent does not simply continue without additional evidence.

### Over-escalation rate
Proportion of adjudicated safe-to-continue cases in which the agent unnecessarily pauses/exits.

### Behavioral consistency
Agreement of the same agent policy across repeated stochastic runs on mode and canonical action.

### Coverage
Fraction of cases retained for autonomous recommendation after applying a reliability threshold; deferred cases are excluded from autonomous coverage but still counted in the cohort.
