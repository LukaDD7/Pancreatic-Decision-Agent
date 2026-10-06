# Action Utility and Counterfactual Review Rubric v0.1

## Why two review passes are required

Outcome-only scoring creates hindsight bias: if the patient later has liver metastasis, it is too easy to declare liver MRI “correct” even when the T0 evidence did not justify it.

Therefore use two independent layers.

## Pass A — Prospective appropriateness (outcome blinded)
Reviewer sees only T0 evidence.

For each action, score 0–3:

- `3 Strongly appropriate`: directly addresses a material unresolved gap; reasonable next action before irreversible intervention.
- `2 Reasonable`: clinically defensible but not clearly preferred; may be broader, lower-yield or partially redundant.
- `1 Weak`: possible but poorly targeted, low expected information gain, or burden probably exceeds expected value.
- `0 Inappropriate`: not justified by T0 evidence, redundant, wrong target, or would delay/complicate care without reasonable benefit.

Also rate terminal choices `CONTINUE` and `EXIT` on the same 0–3 scale.

## Pass B — Outcome-informed preventability (outcome unblinded)
Reviewer now sees index-operation findings and relevant outcome mechanism.

For each evidence action, score 0–3:

- `3 High`: the action directly targets the actual failure mechanism and had a realistic chance to detect/resolve it before surgery.
- `2 Intermediate`: plausible chance of detecting/resolving the mechanism, but sensitivity/availability/timing is less favorable.
- `1 Low`: only indirect or low-probability chance.
- `0 None`: would not reasonably address the actual mechanism.

This score is **not** a statement that the action causally would have prevented the outcome.

## Burden score

0–2:
- `0 Low`: review/reinterpretation or low-burden investigation.
- `1 Moderate`: added imaging/biopsy with material time/cost burden.
- `2 High`: invasive staging or action with meaningful procedural burden.

Do not collapse burden into appropriateness during annotation; preserve all three dimensions.

## Level labels

Suggested display labels after both passes:

- `LEVEL_HIGH`: appropriateness >=2 AND preventability =3.
- `LEVEL_MEDIUM`: appropriateness >=2 AND preventability =2.
- `LEVEL_LOW`: appropriateness >=1 AND preventability =1.
- `LEVEL_NONE`: preventability =0 or appropriateness=0.

These are derived labels, not manually assigned gold answers.

## Example — eventual liver metastasis

Do NOT automatically label `LIVER_MRI` as Level-high.

Review:
- Was there a T0 liver-specific unresolved signal?
- Was the lesion likely detectable by liver MRI given size/location?
- Was it superficial/peritoneal disease where laparoscopy might be more informative?
- Was appropriate liver imaging already recent/negative?
- Was there enough time for the test before surgery?

Possible result:
- Liver MRI: appropriateness 3, preventability 3.
- Staging laparoscopy: 2, 2 or 3 depending on surface disease.
- PET-CT: 2, 1–2.
- Generic repeat CT: 1–2, 1–2.
- Continue without clarification: 0–1 if the unresolved signal was material.

## Example — completed curative resection with suspicious liver wording

A hard negative may yield:
- Continue: appropriateness 2–3.
- Expert radiology review: 2–3.
- Liver MRI: 1–3 depending on actual T0 ambiguity.
- Staging laparoscopy: 0–2.

Thus a negative outcome does not force every extra test to score zero. The research question is decision quality under uncertainty, not retrospective outcome matching.
