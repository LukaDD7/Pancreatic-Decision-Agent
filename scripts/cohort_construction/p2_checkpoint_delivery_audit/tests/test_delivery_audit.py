"""Actual frozen packet -> FHIR -> HTML -> audit -> Ready Resource controls."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest

from scripts.cohort_construction.p2_checkpoint_delivery_audit.audit import (
    DIMENSIONS, build_delivery_audit_request, inspect_delivery_artifacts, validate_delivery_audit,
)
from scripts.cohort_construction.p2_checkpoint_delivery_audit.chain import run_delivery_audit_chain, validate_delivery_audit_chain
from scripts.cohort_construction.p2_checkpoint_delivery_audit.sampling import (
    freeze_ready_sampling_design, freeze_statistical_sampling_plan, evaluate_human_sample,
)
from scripts.cohort_construction.p2_checkpoint_delivery_audit.ready import build_ready_resource_manifest
from scripts.cohort_construction.p2_checkpoint_fhir.doctor import doctor_bundle_from_packet
from scripts.cohort_construction.p2_checkpoint_material_governance.assembly import material_item, pending_workbench
from scripts.cohort_construction.p2_checkpoint_material_governance.episode import build_episode_material_set
from scripts.cohort_construction.p2_checkpoint_material_governance.review import run_review_chain
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import fictional_graph, _pass_only
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import prepare_candidate_materials
from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_material_governance import review_for
from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256
from scripts.cohort_construction.p2_checkpoint_packet.tests.test_compiler import packet, semantic, frozen_questionnaire
from scripts.cohort_construction.p2_checkpoint_packet.compiler import freeze_checkpoint_packet
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit import (
    build_graph_audit_request, run_graph_audit_chain,
)
from scripts.cohort_construction.p2_checkpoint_reconstruction_audit.tests.test_reconstruction_audit import passing_graph_response
from scripts.cohort_construction.p2_checkpoint_review_app.build import build_reviewer_html


def passing_delivery(request):
    return dict(format='episode-delivery-audit-0.1.0', review_id='SYNTHETIC-DELIVERY-AUDIT',
        target=deepcopy(request['target']), **request['response_binding'], reviewer_type='llm_episode_delivery_audit',
        dimensions=[dict(dimension=dimension, applicability='APPLICABLE', status='RESOLVED', reason='虚构交付审计')
                    for dimension in DIMENSIONS], findings=[], revision_requests=[],
        status='AUTO_DELIVERY_AUDIT_PASS', required_actions=[])


class DeliveryAuditTests(unittest.TestCase):
    def material_set(self):
        graph, sources = fictional_graph()
        tasks, failures = prepare_candidate_materials(graph, sources,
            decision_task_id='SYNTHETIC-TASK', visibility_policy_version='SYNTHETIC-POLICY')
        self.assertFalse(failures)
        calls = {}
        def ask(task, request, schema):
            target = request['target']['checkpoint_id']
            calls[target] = calls.get(target, 0) + 1
            if calls[target] > 1:
                return _pass_only(request)
            from scripts.cohort_construction.p2_checkpoint_material_governance.tests.test_candidate_assembly import _passing_answer
            current = next(t for t in tasks if t['compiled']['target']['checkpoint_id'] == target)
            answer = _passing_answer(current, current['manifest'], current['compiled'], current['candidate_sources'], current['excluded_sources'])
            answer.update(request['response_binding'])
            return answer
        items = []
        for task in tasks:
            chain = run_review_chain(task['manifest'], task['compiled'], ask,
                candidate_sources=task['candidate_sources'], excluded_sources=task['excluded_sources'], task_context=task['task_context'])
            self.assertEqual(chain['state'], 'AUTO_REVIEW_PASS')
            items.append(material_item(task, chain))
        reference = dict(episode_id='EPISODE-01', candidate_version=graph['graph_version'],
            input_sha256=canonical_sha256(graph), graph_snapshot=graph)
        return build_episode_material_set(reference, items)

    def staging_deliveries(self, material):
        deliveries = []
        for item in material['checkpoint_materials']:
            compiled = item['compiled']
            audit = semantic(compiled)
            audit['target'] = deepcopy(compiled['target'])
            frozen = freeze_checkpoint_packet(compiled, audit, frozen_questionnaire(),
                packet_id='PK-' + compiled['target']['checkpoint_id'], packet_version='1')
            assignment = 'SYNTHETIC-STAGING-' + compiled['target']['checkpoint_id']
            bundle = doctor_bundle_from_packet(frozen, episode_id=material['episode_reference']['episode_id'])
            html = build_reviewer_html(bundle, assignment_id=assignment, expected_packet_sha256=frozen['packet_sha256'])
            deliveries.append(dict(checkpoint_id=compiled['target']['checkpoint_id'], packet=frozen, bundle=bundle,
                html=html, assignment_id=assignment, staging_only=True))
        return deliveries

    def test_actual_fhir_html_bound_to_episode_and_no_release(self):
        material = self.material_set()
        deliveries = self.staging_deliveries(material)
        request = build_delivery_audit_request(material, deliveries)
        self.assertTrue(request['audit_scope']['actual_artifacts_not_preview'])
        self.assertFalse(request['actual_deliveries'][0]['delivery_gate']['doctor_assignment_created'])
        response = passing_delivery(request)
        self.assertEqual(validate_delivery_audit(request, response)['status'], 'AUTO_DELIVERY_AUDIT_PASS')
        self.assertFalse(inspect_delivery_artifacts(deliveries)['no_clinical_or_leakage_clearance'] is False)
        delivery_chain = run_delivery_audit_chain(request, lambda task, payload, schema: passing_delivery(payload))
        graph, sources = fictional_graph()
        graph_request = build_graph_audit_request(graph, sources,
            processing_units=[dict(unit_id='ALL', source_ids=[source['source_id'] for source in sources])])
        graph_chain = run_graph_audit_chain(graph_request, lambda task, payload, schema: passing_graph_response(payload))
        ready = build_ready_resource_manifest(resource_id='SYNTHETIC-READY', graph_audit=graph_chain,
            material_set=material, delivery_audit=delivery_chain,
            staging_delivery=dict(doctor_assignment_created=False, clinical_release_permitted=False))
        self.assertEqual(ready['resource_state'], 'READY_FOR_HUMAN_SAMPLING')
        self.assertFalse(ready['clinical_release_permitted'])
        # A bare human/automatic status pair is deliberately insufficient for
        # clinical eligibility: no frozen population/statistics/protocol/authorization.
        ineligible = build_ready_resource_manifest(resource_id='SYNTHETIC-READY', graph_audit=graph_chain,
            material_set=material, delivery_audit=delivery_chain,
            staging_delivery=dict(doctor_assignment_created=False, clinical_release_permitted=False),
            human_sample_result=dict(status='HUMAN_SAMPLE_ACCEPT'),
            clinical_protocol=dict(frozen=True, D01_D11_frozen=True, questionnaire_frozen=True, assignment_and_blinding_frozen=True),
            external_authorization=dict(format='ready-resource-external-governance-authorization-0.1.0', active=True,
                automatic_sampling_release_authorized=True))
        self.assertEqual(ineligible['resource_state'], 'READY_FOR_HUMAN_SAMPLING')
        self.assertFalse(ineligible['clinical_release_permitted'])


def automatic_ready_inputs(test):
    material = test.material_set()
    deliveries = test.staging_deliveries(material)
    request = build_delivery_audit_request(material, deliveries)
    delivery = run_delivery_audit_chain(request, lambda task, payload, schema: passing_delivery(payload))
    graph, sources = fictional_graph()
    graph_request = build_graph_audit_request(graph, sources,
        processing_units=[dict(unit_id='ALL', source_ids=[source['source_id'] for source in sources])])
    graph_chain = run_graph_audit_chain(graph_request, lambda task, payload, schema: passing_graph_response(payload))
    return material, delivery, graph_chain


def qualified_sample_inputs(material, delivery):
    actual = delivery['initial_request']['actual_deliveries']
    population = [dict(unit_id=item['checkpoint_id'], packet_id=item['packet']['packet_id'],
        packet_sha256=item['packet']['packet_sha256'], episode_material_set_sha256=material['set_sha256'],
        delivery_input_sha256=delivery['delivery_input_sha256'], stratum_id='SYNTHETIC',
        patient_key='SYNTHETIC-PATIENT-' + item['checkpoint_id'], episode_id=material['episode_reference']['episode_id'])
        for item in actual]
    criteria = dict(estimands=[dict(estimand_id='SYNTHETIC-DELIVERY-DEFECT', defect_type='synthetic_defect')],
        stratum_weights=dict(SYNTHETIC=1), cluster_unit='patient',
        severity_thresholds=dict(**{'SYNTHETIC-DELIVERY-DEFECT': dict(metric='point_estimate', maximum=0, confidence_level=0.95)}),
        missing_and_unresolved=dict(policy='complete_response_required'),
        expansion_and_full_review_triggers=[dict(trigger_id='SYNTHETIC-EXPANSION', on='SAMPLING_FAILED',
            action='NEW_EXPANSION_PLAN_REQUIRED')])
    identity = dict(plan_id='SYNTHETIC-PLAN', seed=7, allocations=dict(SYNTHETIC=len(population)))
    design = freeze_ready_sampling_design(design_id='SYNTHETIC-DESIGN-ONLY',
        pipeline_binding_sha256=canonical_sha256(dict(material=material['set_sha256'], delivery=delivery['delivery_input_sha256'])),
        population_manifest_sha256=canonical_sha256(population), plan_sha256=canonical_sha256(identity),
        statistical_acceptance_criteria=criteria)
    plan = freeze_statistical_sampling_plan(design=design, population=population, **identity)
    responses = []
    for unit in population:
        responses.append(dict(**{key: unit[key] for key in ('unit_id', 'packet_id', 'packet_sha256',
            'episode_material_set_sha256', 'delivery_input_sha256', 'stratum_id', 'patient_key', 'episode_id')},
            assignment_id='ASSIGN-' + unit['unit_id'], design_sha256=design['design_sha256'],
            frozen_plan_sha256=plan['frozen_plan_sha256'], assignment_manifest_sha256=plan['assignment_manifest_sha256'],
            reviewer_id='SYNTHETIC-ACTUAL-HUMAN-DECLARATION-NOT-A-REAL-REVIEW', reviewer_type='human',
            independence_attested=True, reviewed_at=datetime(2026, 10, 29, tzinfo=timezone.utc).isoformat(),
            status='HUMAN_SAMPLE_ACCEPT', defects=dict(synthetic_defect=0)))
    sample = evaluate_human_sample(design=design, plan=plan, responses=responses)
    return design, plan, sample


class ReadyResourceBindingTests(DeliveryAuditTests):
    def test_external_authorization_is_bound_to_actual_design_result_and_protocol(self):
        material, delivery, graph = automatic_ready_inputs(self)
        design, sampling_plan, sample = qualified_sample_inputs(material, delivery)
        questionnaire = delivery['initial_request']['actual_deliveries'][0]['packet']['questionnaire']
        protocol = dict(frozen=True, D01_D11_frozen=True, questionnaire_frozen=True,
            assignment_and_blinding_frozen=True, protocol_sha256=canonical_sha256(dict(synthetic_protocol=True)),
            episode_material_set_sha256=material['set_sha256'],
            questionnaire_bindings=[dict(canonical=questionnaire['canonical'], version=questionnaire['version'],
                sha256=questionnaire['sha256'])], synthetic_no_real_protocol_frozen=True)
        authorization = dict(format='ready-resource-external-governance-authorization-0.1.0',
            authorization_id='SYNTHETIC-ONLY-NOT-A-REAL-APPROVAL', active=True, release_approval=True,
            pipeline_scope_approved=True, population_scope_approved=True, actual_statistics_reviewed=True,
            clinical_protocol_approved=True, automatic_sampling_release_authorized=True,
            episode_material_set_sha256=material['set_sha256'], delivery_input_sha256=delivery['delivery_input_sha256'],
            pipeline_binding_sha256=sample['pipeline_binding_sha256'], design_sha256=design['design_sha256'],
            population_manifest_sha256=sample['population_manifest_sha256'],
            human_result_sha256=sample['human_result_sha256'], protocol_sha256=protocol['protocol_sha256'])
        arguments = dict(resource_id='SYNTHETIC-READY', graph_audit=graph, material_set=material,
            delivery_audit=delivery, staging_delivery=dict(doctor_assignment_created=False, clinical_release_permitted=False),
            sampling_design=design, sampling_plan=sampling_plan, human_sample_result=sample, clinical_protocol=protocol,
            external_authorization=authorization)
        eligible = build_ready_resource_manifest(**arguments)
        self.assertEqual(eligible['resource_state'], 'CLINICAL_PACKET_ELIGIBLE')
        self.assertFalse(eligible['clinical_release_permitted'])
        self.assertTrue(eligible['actual_release_requires_separate_execution'])
        # Synthetic interface fixture only: these strings are not a real clinical
        # protocol, real human result or authorization to distribute patient packets.
        changed_result = deepcopy(sample)
        changed_result['human_result_sha256'] = '0' * 64
        changed = dict(arguments, human_sample_result=changed_result)
        self.assertEqual(build_ready_resource_manifest(**changed)['resource_state'], 'READY_FOR_HUMAN_SAMPLING')
        self.assertFalse(build_ready_resource_manifest(**changed)['statistical_assessment_replayed'])
        changed_authorization = deepcopy(authorization)
        changed_authorization['human_result_sha256'] = '1' * 64
        self.assertEqual(build_ready_resource_manifest(
            **dict(arguments, external_authorization=changed_authorization))['resource_state'], 'READY_FOR_CLINICAL_PILOT')

    def test_graph_or_delivery_chain_cannot_be_replaced_by_a_status_or_hash(self):
        material, delivery, graph = automatic_ready_inputs(self)
        staging = dict(doctor_assignment_created=False, clinical_release_permitted=False)
        for graph_input, delivery_input in ((dict(status='AUTO_GRAPH_AUDIT_PASS'), delivery),
                (graph, passing_delivery(delivery['initial_request']))):
            with self.assertRaises((ValueError, KeyError)):
                build_ready_resource_manifest(resource_id='SYNTHETIC-READY', graph_audit=graph_input,
                    material_set=material, delivery_audit=delivery_input, staging_delivery=staging)
        replay = validate_delivery_audit_chain(delivery)
        self.assertTrue(replay['actual_fhir_html_verified'])
        wrong_material = deepcopy(material)
        wrong_material['episode_reference']['episode_id'] = 'EPISODE-OTHER'
        with self.assertRaisesRegex(ValueError, 'GRAPH_MATERIAL_MISMATCH|DELIVERY_MATERIAL_SET_MISMATCH|EPISODE_GRAPH_MEMBERSHIP_REQUIRED'):
            build_ready_resource_manifest(resource_id='SYNTHETIC-READY', graph_audit=graph,
                material_set=wrong_material, delivery_audit=delivery, staging_delivery=staging)

    def test_researcher_or_hidden_fhir_html_content_is_flagged(self):
        frozen = packet()
        bundle = doctor_bundle_from_packet(frozen)
        html = build_reviewer_html(bundle, assignment_id='SYNTHETIC-STAGING', expected_packet_sha256=frozen['packet_sha256'])
        delivery = dict(checkpoint_id='CP-fictional-1', packet=frozen, bundle=bundle, html=html, staging_only=True)
        self.assertFalse(inspect_delivery_artifacts([delivery])['findings'])
        leaked = deepcopy(delivery)
        leaked['bundle']['entry'][0]['resource']['extension'] = [{'url': 'urn:researcher', 'valueString': 'compiled_partition'}]
        self.assertTrue(inspect_delivery_artifacts([leaked])['findings'])
        hidden = deepcopy(delivery)
        hidden['html'] = hidden['html'].replace('</body>', '<span style="display:none">决定乙。</span></body>')
        findings = inspect_delivery_artifacts([hidden])
        self.assertTrue(any(item['code'] == 'HTML_HIDDEN_CONTENT_REQUIRES_REMOVAL' for item in findings['blocking_findings']))
        # Actual hidden markup is not cleared by deterministic checks or LLM schema.
        self.assertIn('决定乙。', hidden['html'])
        self.assertEqual(hidden['packet']['materials'][0]['text'].count('决定乙。'), 0)

    def test_delivery_chain_binds_artifacts_and_rejects_target_or_html_swap(self):
        material = self.material_set()
        deliveries = self.staging_deliveries(material)
        request = build_delivery_audit_request(material, deliveries)
        chain = run_delivery_audit_chain(request, lambda task, payload, schema: passing_delivery(payload))
        self.assertEqual(chain['state'], 'AUTO_DELIVERY_AUDIT_PASS')
        replay = validate_delivery_audit_chain(chain)
        self.assertTrue(replay['actual_fhir_html_verified'])
        self.assertFalse(replay['clinical_semantics_certified'])
        wrong_target = deepcopy(deliveries)
        wrong_target[0]['checkpoint_id'] = 'EVENT-02'
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_CHECKPOINT|COMPILED_PACKET_TARGET_MISMATCH'):
            build_delivery_audit_request(material, wrong_target)
        wrong_packet = deepcopy(deliveries)
        wrong_packet[0]['packet'] = deepcopy(deliveries[1]['packet'])
        with self.assertRaisesRegex(ValueError, 'COMPILED_PACKET_TARGET_MISMATCH|PACKET_FHIR_IDENTITY_MISMATCH'):
            build_delivery_audit_request(material, wrong_packet)
        wrong_html = deepcopy(deliveries)
        wrong_html[0]['html'] = deliveries[1]['html']
        with self.assertRaisesRegex(ValueError, 'HTML_PACKET_ASSIGNMENT_MISMATCH'):
            build_delivery_audit_request(material, wrong_html)
        hidden = deepcopy(deliveries)
        hidden[0]['html'] = hidden[0]['html'].replace('</body>', '<span style="display:none">当前决定：先行血管重建评估。</span></body>')
        # Bypassing the renderer identity precheck cannot manufacture a PASS from
        # an LLM response: replay rebuilds the request and deterministic inspection.
        forged_request = deepcopy(request)
        forged_request['actual_deliveries'][0]['html'] = hidden[0]['html']
        with self.assertRaisesRegex(ValueError, 'INPUT_RECONSTRUCTION_MISMATCH|UNRESOLVED_PASS|HTML_PACKET_ASSIGNMENT_MISMATCH'):
            validate_delivery_audit_chain(run_delivery_audit_chain(forged_request,
                lambda task, payload, schema: passing_delivery(payload)))

    def test_delivery_failure_history_cannot_be_relabelled_pass(self):
        request = build_delivery_audit_request(self.material_set(), self.staging_deliveries(self.material_set()))
        def fail(task, payload, schema):
            raise RuntimeError('synthetic delivery provider failure')
        chain = run_delivery_audit_chain(request, fail)
        self.assertEqual(chain['state'], 'AUTO_DELIVERY_AUDIT_HOLD')
        self.assertFalse(chain['rounds'])
        self.assertEqual(validate_delivery_audit_chain(chain)['stop_reason'], 'EXECUTION_FAILED')
        forged = deepcopy(chain)
        forged.update(state='AUTO_DELIVERY_AUDIT_PASS', stop_reason='AUTO_PASS')
        with self.assertRaises(ValueError):
            validate_delivery_audit_chain(forged)


if __name__ == '__main__':
    unittest.main()
