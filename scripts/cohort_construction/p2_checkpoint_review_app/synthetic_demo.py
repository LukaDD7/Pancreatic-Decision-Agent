"""Fictional UI mechanism fixture, not a proposed clinical form or Reference."""
from scripts.cohort_construction.p2_checkpoint_packet import (
    build_source_manifest,compile_partition,canonical_sha256,freeze_checkpoint_packet,
)

HIDDEN_SENTINEL='SYNTHETIC_HIDDEN_DECISION_AND_OUTCOME_43fb93'


def synthetic_packet(*, literal_markup=False):
    first='〔虚构资料 · 用于界面演示〕\n记录时间：2020-01-02 09:00\n\n患者完成初步影像评估。血管接触范围仍需结合原始影像复核；当前文书未给出完整生物学评估。\n\n当前一般状态：可进行日常活动，进食情况已记录。\n'
    if literal_markup:first+='原文中的字面字符串：<img src="https://example.invalid/should-not-load" onerror="window.XSS_EXECUTED=true"> </script><script>window.XSS_EXECUTED=true</script>\n'
    second='〔虚构资料 · 既有检查记录〕\n检查日期：2020-01-01\n\n检查报告记载局部病灶及邻近血管接触情况。部分范围需复核。原文未提供本次资料之外的后续检查或管理结果。\n'
    manifest=build_source_manifest('M-UI-DEMO',[dict(source_id='DEMO-SOURCE-01',source_version='1',text=first+HIDDEN_SENTINEL),dict(source_id='DEMO-SOURCE-02',source_version='1',text=second)])
    target=dict(checkpoint_id='CP-UI-DEMO',decision_task_id='synthetic-management-review',visibility_policy_version='synthetic-policy-1')
    proposal=dict(schema_version='0.1.0',proposal_id='P-UI-DEMO',proposal_version='1',manifest_id=manifest['manifest_id'],target=target,
        source_bindings=[{k:s[k] for k in ('source_id','source_version','sha256')} for s in manifest['sources']],
        segments=[dict(segment_id='keep-1',source_id='DEMO-SOURCE-01',disposition='KEEP',quote_locator=dict(exact_quote=first)),
                  dict(segment_id='mask-1',source_id='DEMO-SOURCE-01',disposition='MASK',quote_locator=dict(exact_quote=HIDDEN_SENTINEL)),
                  dict(segment_id='keep-2',source_id='DEMO-SOURCE-02',disposition='KEEP',quote_locator=dict(exact_quote=second))])
    compiled=compile_partition(manifest,proposal)
    semantic=dict(schema_version='0.2.0',assessment_id='SYNTHETIC-AUDIT-ONLY',compiled_sha256=compiled['compiled_sha256'],target=target,status='PASS',reviewer_id='SYNTHETIC-AUDITOR-ONLY',reviewer_type='human',independence_attested=True,
        assessed_at='2020-01-02T09:00:00+00:00',audit_protocol_version='synthetic-ui-audit-1',
        review_scope=dict(scope_status='COMPLETE',included_source_ids=['DEMO-SOURCE-01','DEMO-SOURCE-02'],excluded_source_ids=[],scope_basis='Purely fictional UI fixture; no clinical review.'),
        decision_time_boundary=dict(basis='Fictional event order.',precision='event-order',exact_time_known=False,retrospective_reconstruction=True,uncertainties=[]),
        checks={k:True for k in ('segmentation_semantics_reviewed','permitted_information_scope_reviewed','cross_document_restatements_reviewed','required_source_coverage_reviewed','decision_time_boundary_reviewed','all_segments_explicitly_assessed')},
        segment_assessments=[dict(source_id=source['source_id'],start=span['start'],end=span['end'],source_sha256=source['source_sha256'],exact_quote=span['text'],original_disposition=span['disposition'],assessment='ACCEPT',rationale='Purely fictional UI acceptance.',issue_codes=[]) for source in compiled['sources'] for span in source['segments']],
        issue_records=[],source_coverage_assessments=[dict(source_id=sid,disposition='REVIEWED_INCLUDED',rationale='Fictional UI fixture.',availability_basis='Fictional event order.') for sid in ('DEMO-SOURCE-01','DEMO-SOURCE-02')],required_actions=[])
    def choice(link,text,values):
        return dict(linkId=link,type='choice',text=text,required=True,repeats=True,answerOption=[dict(valueCoding=dict(system='urn:synthetic:review-options',code=code,display=label)) for code,label in values])
    resource=dict(resourceType='Questionnaire',url='urn:synthetic:checkpoint-ui-form',version='demo-1',status='active',experimental=True,
        title='当前时点的管理路径评估',description='虚构资料与演示选项，仅用于验证原文阅读、多选、条件题和意见回收。请勿用于临床评审。',
        item=[dict(linkId='mode-section',type='group',text='01  可接受的 Mode',item=[choice('acceptable-modes','您认为哪些 Mode 可以接受？',[('mode-a','演示模式 A'),('mode-b','演示模式 B'),('mode-u','暂不能判断（演示选项）')])]),
              dict(linkId='action-section',type='group',text='02  可接受的动作',item=[choice('acceptable-actions','哪些动作可以纳入您的意见？',[('action-a','演示动作甲'),('action-b','演示动作乙'),('action-u','暂不能判断（演示选项）')])]),
              dict(linkId='information-section',type='group',text='03  资料与说明',item=[dict(linkId='information-sufficient',type='boolean',text='目前资料是否足以形成意见？',required=True),
                  dict(linkId='additional-information',type='text',text='还需要哪些信息？',required=True,enableWhen=[dict(question='information-sufficient',operator='=',answerBoolean=False)]),
                  dict(linkId='review-comment',type='text',text='其他说明（可选）',maxLength=2000)])])
    q=dict(canonical=resource['url'],version=resource['version'],sha256=canonical_sha256(resource),frozen=True,resource=resource)
    return freeze_checkpoint_packet(compiled,semantic,q,packet_id='PK-UI-DEMO',packet_version='1')
