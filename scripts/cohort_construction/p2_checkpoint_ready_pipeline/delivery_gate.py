"""Explicit, replayable hold before staging delivery compilation.

A development run may have real automatic material results without an
independent SemanticAudit or a frozen clinical Questionnaire.  This gate keeps
those results without fabricating a packet, a human identity, or a delivery
request.  It is never a release authorization.
"""
from copy import deepcopy


REASONS = frozenset({'PENDING_INDEPENDENT_SEMANTIC_AUDIT', 'QUESTIONNAIRE_NOT_FROZEN'})


def freeze_delivery_gate(gate):
    if gate is None:
        return None
    if not isinstance(gate, dict):
        raise ValueError('READY_PIPELINE_DELIVERY_GATE_FIELDS_INVALID')
    if set(gate) != {'reason_codes'}:
        if set(gate) != {'reason_codes', 'automatic_material_pass_is_not_human_audit',
                         'staging_delivery_created', 'clinical_release_permitted'}:
            raise ValueError('READY_PIPELINE_DELIVERY_GATE_FIELDS_INVALID')
        if (gate['automatic_material_pass_is_not_human_audit'] is not True
                or gate['staging_delivery_created'] is not False
                or gate['clinical_release_permitted'] is not False):
            raise ValueError('READY_PIPELINE_DELIVERY_GATE_GOVERNANCE_CHANGED')
    reasons = gate['reason_codes']
    if (not isinstance(reasons, list) or not reasons
            or any(not isinstance(reason, str) for reason in reasons)
            or set(reasons) - REASONS):
        raise ValueError('READY_PIPELINE_DELIVERY_GATE_REASON_INVALID')
    return dict(reason_codes=sorted(set(reasons)), automatic_material_pass_is_not_human_audit=True,
        staging_delivery_created=False, clinical_release_permitted=False)


def delivery_gate_hold(run, output, persist):
    """Retain actual material chains; no SemanticAudit factory is invoked."""
    if run.get('delivery_gate') is None:
        return False
    run['stages'].append(dict(stage='DELIVERY_GATE', state='HOLD',
        reason_code=';'.join(run['delivery_gate']['reason_codes'])))
    if persist:
        from .persistence import write
        write(output / 'delivery-gate.json', deepcopy(run['delivery_gate']))
    return True
