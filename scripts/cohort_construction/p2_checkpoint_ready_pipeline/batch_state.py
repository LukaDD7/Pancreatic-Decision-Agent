"""Shared deterministic terminal-state projection for pipeline wrappers.

This reports saved orchestration state only. It never authorizes clinical use.
"""


def summary_state(run):
    """Preserve the exact graph/material/delivery hold of a completed run."""
    chain = run.get('graph_audit_chain')
    if chain is not None and chain['state'] != 'AUTO_GRAPH_AUDIT_PASS':
        return chain['state'], chain.get('stop_reason')
    if run.get('delivery_gate') is not None and run.get('episode_material_set') is not None \
            and all(item['state'] == 'AUTO_REVIEW_PASS'
                    for item in run['episode_material_set']['checkpoint_materials']):
        return 'DELIVERY_GATE_HOLD', ';'.join(run['delivery_gate']['reason_codes'])
    return run['state'], next((stage.get('reason_code') for stage in reversed(run['stages'])
        if stage.get('reason_code')), None)
