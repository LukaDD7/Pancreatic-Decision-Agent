# One conditional semantic proof refinement

This separate package leaves the running v0.5 code and results immutable.
It reads completed case artifacts and their original review request cache only.
It never reads annotation references, metric targets, other cases, or new facts.

An event is considered for one repair only when the model proposed first_anchor
or new_evidence, the discovered node has source support, and blocking validation
left the final status UNCERTAIN_CONTRACT_OR_SOURCE. The guard addresses required
proof failures or an explicit model function/checkpoint contradiction. Valid
candidates with unused bad citations, repeats, pure semantic uncertainty and
well-formed uncertain source availability alone do not trigger repair.
The same Qwen model receives the
original full document, ledger and evidence pool, restricted target event IDs,
its original proposal and precise validation feedback. It can correct quotes,
change its classification, or remain uncertain. It cannot add sources or events.

All repaired proposals use the frozen citation, availability, chronology and
selection guards. No quote is clipped into an observation. Duplicate or missing
targets and transport/schema failures retain the original event. The package
stores the original events, raw repair response, both request hashes, effective
receipts, unchanged evidence blocks and regenerated candidate FHIR annotations.
These artifacts are reconstruction candidates; blind_replay_ready remains false.

Before any repair call, the package authenticates the original cache key from
the exact nonsecret endpoint and request body, then checks each allowed block's
coordinates and masked raw text. The endpoint must contain no query, userinfo
or fragment. `refinement_manifest.json` records that endpoint, original artifact
hashes and preserved-file hashes. Discovery, unit inventory and source import
receipts are copied byte for byte, and prior knowledge is revalidated against
the original discovery events used by the primary review.

```sh
python -m scripts.cohort_construction.p2_auto_checkpoint_v05_refine.run \
  --input /absolute/path/to/completed_case_or_release \
  --output /absolute/path/to/new_refined_output \
  --cache /absolute/path/to/v05/model_cache \
  --config config/p2_auto_checkpoint_v05.json
```

There is at most one semantic repair call per original review request group.
The shared transport client may retry technical failures under its existing
policy; those attempts are recorded separately from semantic repair rounds.
