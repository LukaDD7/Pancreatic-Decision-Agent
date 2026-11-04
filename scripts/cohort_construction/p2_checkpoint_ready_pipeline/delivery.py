"""Create private staging packets; supplied SemanticAudits are never synthesized."""
from scripts.cohort_construction.p2_checkpoint_fhir.doctor import doctor_bundle_from_packet
from scripts.cohort_construction.p2_checkpoint_packet.compiler import freeze_checkpoint_packet
from scripts.cohort_construction.p2_checkpoint_review_app.build import build_reviewer_html


def build_staging_deliveries(material_set, semantic_audits, questionnaire):
    if not isinstance(semantic_audits, dict):
        raise ValueError('READY_PIPELINE_INDEPENDENT_SEMANTIC_AUDITS_REQUIRED')
    deliveries = []
    for item in material_set['checkpoint_materials']:
        checkpoint_id = item['compiled']['target']['checkpoint_id']
        if checkpoint_id not in semantic_audits:
            raise ValueError('READY_PIPELINE_SEMANTIC_AUDIT_MISSING:' + checkpoint_id)
        # This compiler validates independent scope, target, compiled hash and
        # attestation. Automatic material review is not a substitute input.
        packet = freeze_checkpoint_packet(item['compiled'], semantic_audits[checkpoint_id],
            questionnaire, packet_id='PK-' + checkpoint_id, packet_version='1')
        episode_id = material_set['episode_reference']['episode_id']
        bundle = doctor_bundle_from_packet(packet, episode_id=episode_id)
        assignment_id = 'STAGING-' + checkpoint_id
        html = build_reviewer_html(bundle, assignment_id=assignment_id,
            expected_packet_sha256=packet['packet_sha256'])
        deliveries.append(dict(checkpoint_id=checkpoint_id, packet=packet, bundle=bundle,
            html=html, assignment_id=assignment_id, staging_only=True,
            doctor_assignment_created=False, external_release_authorized=False))
    return deliveries
