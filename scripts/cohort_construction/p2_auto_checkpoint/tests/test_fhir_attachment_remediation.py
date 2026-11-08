"""P2AUTO-023 synthetic generic FHIR attachment loss counterexamples."""
import base64
import unittest

from scripts.cohort_construction.p2_auto_checkpoint import fhir_io
from scripts.cohort_construction.p2_pathway_replay.core import clock


def inline(text):
    return {'contentType': 'text/plain', 'data': base64.b64encode(text.encode()).decode()}


def generic(resource_type='DocumentReference', attachment_container=None):
    resource = dict(resourceType=resource_type, id='SYNTHETIC',
                    subject={'reference': 'Patient/SYNTHETIC'},
                    type={'text': '病程记录'} if resource_type == 'DocumentReference' else None,
                    code={'text': 'Synthetic source'} if resource_type != 'DocumentReference' else None)
    resource = {key: value for key, value in resource.items() if value is not None}
    if attachment_container is not None:
        if resource_type == 'DocumentReference':
            resource['content'] = [{'attachment': attachment} for attachment in attachment_container]
        else:
            resource['presentedForm'] = attachment_container
    return dict(resourceType='Bundle', type='collection', entry=[
        dict(resource=dict(resourceType='Patient', id='SYNTHETIC')),
        dict(resource=resource)])


class GenericFhirAttachmentTests(unittest.TestCase):
    def test_P2AUTO023_multi_inline_and_mixed_generic_resources_fail_closed(self):
        cases = [
            ('DocumentReference', [inline('第一部分'), inline('第二部分')]),
            ('DocumentReference', [inline('内联原文'), {'url': 'https://example.invalid/original'}]),
            ('DocumentReference', [dict(inline('内联原文'), url='https://example.invalid/original')]),
            ('DiagnosticReport', [inline('所见'), inline('结论')]),
            ('Observation', [inline('内联'), {'url': 'https://example.invalid/original'}]),
        ]
        for resource_type, attachments in cases:
            with self.subTest(resource_type=resource_type, attachments=attachments):
                with self.assertRaisesRegex(
                        ValueError, '^FHIR_GENERIC_MULTI_OR_MIXED_ATTACHMENT_UNSUPPORTED$'):
                    fhir_io.import_sources(generic(resource_type, attachments))

    def test_P2AUTO023_generic_single_inline_still_imports_verbatim(self):
        text = '记录时间：2022-09-03 10:00\n既往2022-01-01 10:00曾于外院检查。'
        restored, receipt = fhir_io.import_sources(generic(attachment_container=[inline(text)]))
        self.assertEqual(restored[0]['text'], text)
        self.assertEqual(restored[0]['clocks']['body_record_time']['value'], '2022-09-03 10:00:00')
        self.assertEqual(receipt['resources_imported'], 1)

    def test_P2AUTO023_adapter_single_inline_roundtrip_remains_lossless(self):
        source = dict(source_id='SYNTHETIC-DOCUMENT', patient_uid='SYNTHETIC', kind='document',
                      family='documents_all', title='病程记录', text='记录时间：2022-09-03 10:00\n原文完整保留。',
                      clocks=dict(body_record_time=clock('2022-09-03 10:00'), source_create_time=clock(None)),
                      clock=clock('2022-09-03 10:00'), clock_basis='body_record_time',
                      provenance=dict(source_record_key='SYNTHETIC'), original_fields={}, flags=[])
        source['source_sha256'] = fhir_io.hashlib.sha256(source['text'].encode()).hexdigest()
        bundle = fhir_io.build([source], 'SYNTHETIC', [], [])
        restored, _ = fhir_io.import_sources(bundle)
        self.assertEqual(restored[0]['source_id'], source['source_id'])
        self.assertEqual(restored[0]['text'], source['text'])
        self.assertEqual(restored[0]['clocks'], source['clocks'])


if __name__ == '__main__':
    unittest.main()
