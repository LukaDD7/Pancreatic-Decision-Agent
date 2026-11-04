"""Researcher-only graph export. Never call this to build a doctor assignment."""
from __future__ import annotations
import copy
import json
from scripts.cohort_construction.p2_checkpoint_packet import canonical_sha256
from .common import BASE, SYSTEM, extension, fail, full_url, reference, resource_id, validate_fhir_r4

RELATION_DEFINITIONS = {
    "same_management_process": {
        "asserting_actor": "复核阶段LLM提议；程序核验支持证据并决定是否合并候选Episode",
        "meaning": "source_event_id所指管理记录与target_event_id所指管理记录属于同一连续管理过程的候选关系",
        "effect": "只有程序核验支持的关系可参与候选Episode连通性合并；不证明两条记录是同一临床决策",
    },
    "same_event": {
        "asserting_actor": "复核阶段LLM提议；程序核验具体管理判断同一性的来源证据",
        "meaning": "两条管理记录表述同一项具体临床管理判断的候选身份关系",
        "effect": "支持时可参与候选事件身份归并；不将重复表述当作新增独立决策点",
    },
    "evidence_comparison_previous": {
        "asserting_actor": "复核阶段LLM指定既往管理判断比较对象；程序核验是否提供、是否在先及最终对象分类",
        "meaning": "target_event_id所指当前记录的患者信息变化判断，以source_event_id所指既往管理判断为比较对象",
        "effect": "仅定义比较依赖；单独存在不构成同一候选Episode或同一临床决策的证明",
    },
}


def _payload_extensions(payload):
    return [extension("research-payload-json", json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                          separators=(",", ":"), allow_nan=False)),
            extension("research-payload-sha256", canonical_sha256(payload))]


def _basic(kind, identity, payload, extra=()):
    return {"resourceType": "Basic", "id": resource_id("research", identity),
            "identifier": [{"system": BASE + "/identifier/" + kind, "value": identity}],
            "code": {"coding": [{"system": SYSTEM, "code": kind}]},
            "extension": [extension("audience", "researcher-only"),
                          extension("assertion-status", "algorithm-candidate-not-clinical-reference"),
                          *_payload_extensions(payload), *extra]}


def researcher_bundle_from_graph(graph: dict) -> dict:
    """Preserve research JSON and expose local typed FHIR resource references.

    graph requires graph_id, graph_version, events, episodes, relations. Resource
    Basic is used for the research constructs; no clinical EpisodeOfCare,
    Procedure, Condition, or physician Reference is manufactured from candidates.
    """
    required = {"graph_id", "graph_version", "events", "episodes", "relations"}
    if not isinstance(graph, dict) or set(graph) != required:
        fail("FHIR_RESEARCH_GRAPH_CONTRACT")
    if any(not isinstance(graph[k], str) or not graph[k] for k in ("graph_id", "graph_version")):
        fail("FHIR_RESEARCH_GRAPH_ID_VERSION")
    if any(not isinstance(graph[k], list) or not all(isinstance(v, dict) for v in graph[k])
           for k in ("events", "episodes", "relations")):
        fail("FHIR_RESEARCH_GRAPH_ARRAYS")
    events, episodes, relations = graph["events"], graph["episodes"], graph["relations"]
    event_urls, episode_urls, entries = {}, {}, []
    graph_hash = canonical_sha256(graph)
    for event in events:
        eid = event.get("event_id")
        if not isinstance(eid, str) or not eid or eid in event_urls:
            fail("FHIR_RESEARCH_EVENT_ID_UNIQUE")
        event_urls[eid] = full_url("research-event", graph_hash + ":" + eid)
    for episode in episodes:
        eid = episode.get("episode_id")
        if not isinstance(eid, str) or not eid or eid in episode_urls:
            fail("FHIR_RESEARCH_EPISODE_ID_UNIQUE")
        episode_urls[eid] = full_url("research-episode", graph_hash + ":" + eid)
    manifest = _basic("research-graph", graph["graph_id"], {
        "graph_id": graph["graph_id"], "graph_version": graph["graph_version"],
        "graph_sha256": graph_hash, "relation_definitions": RELATION_DEFINITIONS})
    entries.append({"fullUrl": full_url("research-graph", graph_hash), "resource": manifest})
    for event in events:
        extra = []
        episode_id = event.get("episode_id")
        if episode_id:
            if episode_id not in episode_urls:
                fail("FHIR_RESEARCH_EVENT_EPISODE_UNRESOLVED")
            extra.append(reference("candidate-episode", episode_urls[episode_id]))
        entries.append({"fullUrl": event_urls[event["event_id"]],
                        "resource": _basic("research-management-record", event["event_id"], event, extra)})
    for episode in episodes:
        extra = []
        for key, ext in (("candidate_ids", "member-management-record"), ("checkpoint_ids", "candidate-checkpoint")):
            ids = episode.get(key, [])
            if (not isinstance(ids, list) or not all(isinstance(v, str) and v for v in ids)
                    or len(set(ids)) != len(ids)):
                fail("FHIR_RESEARCH_EPISODE_MEMBER_IDS")
            for eid in ids:
                if eid not in event_urls:
                    fail("FHIR_RESEARCH_EPISODE_MEMBER_UNRESOLVED")
                extra.append(reference(ext, event_urls[eid]))
        if not set(episode.get("checkpoint_ids", [])).issubset(set(episode.get("candidate_ids", []))):
            fail("FHIR_RESEARCH_CHECKPOINT_NOT_EPISODE_MEMBER")
        entries.append({"fullUrl": episode_urls[episode["episode_id"]],
                        "resource": _basic("research-candidate-episode", episode["episode_id"], episode, extra)})
    for i, relation in enumerate(relations):
        kind, source, target = (relation.get(k) for k in ("relation", "source_event_id", "target_event_id"))
        if kind not in RELATION_DEFINITIONS:
            fail("FHIR_RESEARCH_RELATION_TYPE_UNSUPPORTED")
        if source not in event_urls or target not in event_urls:
            fail("FHIR_RESEARCH_RELATION_TARGET_UNRESOLVED")
        rid = graph["graph_id"] + ":R" + str(i) + ":" + canonical_sha256(relation)[:16]
        extra = [extension("relation-type", kind, "valueCode"),
                 reference("source-management-record", event_urls[source]),
                 reference("target-management-record", event_urls[target])]
        entries.append({"fullUrl": full_url("research-relation", graph_hash + ":" + rid),
                        "resource": _basic("research-graph-relation", rid, relation, extra)})
    bundle = {"resourceType": "Bundle", "id": resource_id("researchgraph", graph_hash), "type": "collection",
              "identifier": {"system": BASE + "/identifier/research-graph", "value": graph_hash}, "entry": entries}
    validate_fhir_r4(bundle)
    return bundle
