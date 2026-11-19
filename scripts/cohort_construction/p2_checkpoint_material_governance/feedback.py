"""Research-only Checkpoint feedback, never a formal blind-review packet.

Only the already projected doctor_view text enters the returned artifact. The
whole graph, original researcher sources, model reasons, operation ledgers and
other Checkpoints are not embedded. A draft does not create a SemanticAudit,
clinical Questionnaire, FHIR package, doctor assignment or clinical Reference.
"""
from copy import deepcopy
import html
import json

from scripts.cohort_construction.p2_checkpoint_packet.compiler import canonical_sha256

FORMAT = 'checkpoint-research-feedback-draft-0.1.0'
FORM_VERSION = 'research-material-readability-feedback-0.1.0'
FORM = [
    dict(field_id='readability', label='材料可读性', type='enum',
         options=['READABLE_WITH_LIMITATIONS', 'NOT_READABLE', 'NOT_ASSESSED']),
    dict(field_id='concerns', label='遗漏、时点或泄漏疑虑及修订建议', type='text'),
    dict(field_id='reviewer_declaration', label='实际反馈人声明（不由程序预填）', type='text'),
]


def build_research_feedback_draft(material_set, *, source_limitations=()):
    """Derive a visible-text-only draft from a replayed Episode material set."""
    from scripts.cohort_construction.p2_checkpoint_material_governance.episode import (
        validate_episode_material_set,
    )
    validate_episode_material_set(material_set)
    checkpoints = []
    for item in material_set['checkpoint_materials']:
        chain = item['auto_review_chain']
        target = chain['compiled']['target']
        view = chain['doctor_view']
        if (view['target'] != target or view['compiled_sha256'] != chain['compiled']['compiled_sha256']
                or view['researcher_original_included'] is not False):
            raise ValueError('RESEARCH_FEEDBACK_DOCTOR_VIEW_BINDING_INVALID')
        if target['checkpoint_id'] != item['checkpoint_id']:
            raise ValueError('RESEARCH_FEEDBACK_CHECKPOINT_IDENTITY_INVALID')
        checkpoints.append(dict(
            checkpoint_id=target['checkpoint_id'], target=deepcopy(target),
            automatic_material_state=chain['state'], stop_reason=chain['stop_reason'],
            compiled_sha256=chain['compiled']['compiled_sha256'],
            doctor_view_sha256=view['view_sha256'],
            visible_materials=[dict(source_id=m['source_id'], source_version=m['source_version'],
                text=m['text'], sha256=m['sha256']) for m in view['materials']],
            no_visible_material=all(not m['text'] or m['text'] == '[资料按方案遮蔽]'
                for m in view['materials']),
            human_or_clinical_status='NOT_ASSESSED',
        ))
    limitations = [
        '研究性未审定试阅稿；自动材料结果不是独立SemanticAudit或临床Reference。',
        '医生投影中MASK和REVIEW均不开放；未处理REVIEW不等于明确阴性或已审定遮蔽。',
        '未提供冻结的临床Acceptable Mode Set问卷；本稿仅收集材料可读性和修订反馈。',
        '候选图、目标时点和Episode归属未由本稿认证。',
    ] + [str(value) for value in source_limitations]
    result = dict(format=FORMAT, episode_id=material_set['episode_reference']['episode_id'],
        material_set_sha256=material_set['set_sha256'],
        candidate_graph_sha256=material_set['episode_reference']['input_sha256'],
        checkpoints=checkpoints, feedback_form_version=FORM_VERSION,
        feedback_form=deepcopy(FORM), research_limitations=limitations,
        formal_blind_packet_created=False, fhir_bundle_created=False,
        semantic_audit_created=False, clinical_questionnaire_frozen=False,
        doctor_assignment_created=False, clinical_reference_created=False,
        clinical_release_permitted=False)
    result['draft_sha256'] = canonical_sha256(result)
    return result


def render_research_feedback_html(draft):
    """Render one static local file; no hidden originals or future resources."""
    from scripts.cohort_construction.p2_checkpoint_material_governance.feedback import (
        validate_research_feedback_draft,
    )
    validate_research_feedback_draft(draft)
    esc = html.escape
    parts = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>研究性材料试阅</title><style>',
        'body{font-family:system-ui,sans-serif;line-height:1.65;max-width:1000px;margin:24px auto;padding:0 16px;background:#f5f7f8;color:#172a32}',
        'main,section{background:#fff;border:1px solid #d4dfe3;border-radius:10px;padding:20px;margin:16px 0}',
        '.warning{background:#fff4d6;border-left:5px solid #a56800;padding:12px}.material{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f7f8;padding:16px;border-radius:8px}',
        'label{display:block;margin-top:16px;font-weight:600}textarea,select{box-sizing:border-box;width:100%;padding:10px;font:inherit}button{font:inherit;padding:10px 16px;margin-top:16px}output{display:block;white-space:pre-wrap;overflow-wrap:anywhere;margin-top:16px}code{overflow-wrap:anywhere}',
        '</style></head><body><main><h1>研究性材料试阅与可读性反馈</h1>',
        '<p class="warning"><strong>未审定研究结果，不是正式医生盲评包、临床Reference或诊疗建议。</strong><br>本文件只含当前医生投影中的文本，不含研究者完整原文、候选图、模型审核理由或其他Checkpoint资料。</p>',
        '<p>Episode：<code>' + esc(draft['episode_id']) + '</code><br>草稿版本：<code>' + esc(draft['draft_sha256']) + '</code><br>表单版本：<code>' + esc(draft['feedback_form_version']) + '</code></p>',
        '<h2>已知限制</h2><ul>' + ''.join('<li>' + esc(x) + '</li>' for x in draft['research_limitations']) + '</ul></main>']
    for cp in draft['checkpoints']:
        parts.append('<section data-checkpoint="' + esc(cp['checkpoint_id']) + '"><h2>候选Checkpoint <code>' + esc(cp['checkpoint_id']) + '</code></h2>')
        parts.append('<p>自动材料状态：' + esc(cp['automatic_material_state']) + '；这不是临床或人工结论。</p>')
        if cp['no_visible_material']:
            parts.append('<p class="warning">当前没有开放的临床原文。请仅反馈“不可读/需修订”，不要推测被遮内容。</p>')
        for material in cp['visible_materials']:
            parts.append('<h3>来源 <code>' + esc(material['source_id']) + '</code></h3><div class="material">' + esc(material['text']) + '</div>')
        parts.append('<label>材料可读性 <select id="readability-' + esc(cp['checkpoint_id']) + '"><option>NOT_ASSESSED</option><option>READABLE_WITH_LIMITATIONS</option><option>NOT_READABLE</option></select></label>')
        parts.append('<label>遗漏、时点或泄漏疑虑及修订建议 <textarea id="concerns-' + esc(cp['checkpoint_id']) + '" rows="5"></textarea></label>')
        parts.append('</section>')
    parts.append('<main><h2>回收反馈</h2><p>本页不联网、不提交服务器。填写后由实际反馈人在本机导出JSON，交回研究者回收。程序不预填身份或临床可接受动作。</p>')
    parts.append('<label>实际反馈人声明 <textarea id="reviewer-declaration" rows="3"></textarea></label><button type="button" id="export">导出研究性反馈JSON</button><output id="export-output"></output></main>')
    # Public binding metadata only. It contains no original clinical strings.
    binding = dict(draft_sha256=draft['draft_sha256'], material_set_sha256=draft['material_set_sha256'],
        candidate_graph_sha256=draft['candidate_graph_sha256'], episode_id=draft['episode_id'],
        feedback_form_version=draft['feedback_form_version'],
        checkpoints=[dict(checkpoint_id=c['checkpoint_id'], compiled_sha256=c['compiled_sha256'],
            doctor_view_sha256=c['doctor_view_sha256']) for c in draft['checkpoints']])
    script = '''<script>
document.getElementById('export').addEventListener('click',()=>{
 const binding=JSON.parse(document.getElementById('public-bindings').textContent);
 const responses=binding.checkpoints.map(c=>({checkpoint_id:c.checkpoint_id,compiled_sha256:c.compiled_sha256,doctor_view_sha256:c.doctor_view_sha256,readability:document.getElementById('readability-'+c.checkpoint_id).value,concerns:document.getElementById('concerns-'+c.checkpoint_id).value}));
 const response={format:'checkpoint-research-feedback-response-0.1.0',draft_sha256:binding.draft_sha256,material_set_sha256:binding.material_set_sha256,candidate_graph_sha256:binding.candidate_graph_sha256,episode_id:binding.episode_id,feedback_form_version:binding.feedback_form_version,responses,reviewer_declaration:document.getElementById('reviewer-declaration').value,clinical_reference_created:false};
 document.getElementById('export-output').textContent=JSON.stringify(response,null,2);
});
</script>'''
    parts.append('<script type="application/json" id="public-bindings">' + json.dumps(binding, ensure_ascii=False).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e') + '</script>')
    parts.append(script + '</body></html>')
    return ''.join(parts)


def validate_research_feedback_draft(draft):
    """Recompute every public artifact hash without consulting hidden sources."""
    if not isinstance(draft, dict) or draft.get('format') != FORMAT:
        raise ValueError('RESEARCH_FEEDBACK_DRAFT_FORMAT_INVALID')
    if not draft.get('draft_sha256') or canonical_sha256({k:v for k,v in draft.items() if k != 'draft_sha256'}) != draft['draft_sha256']:
        raise ValueError('RESEARCH_FEEDBACK_DRAFT_HASH_INVALID')
    for key in ('formal_blind_packet_created', 'fhir_bundle_created', 'semantic_audit_created',
                'clinical_questionnaire_frozen', 'doctor_assignment_created',
                'clinical_reference_created', 'clinical_release_permitted'):
        if draft.get(key) is not False:
            raise ValueError('RESEARCH_FEEDBACK_GOVERNANCE_CHANGED')
    if draft.get('feedback_form_version') != FORM_VERSION or draft.get('feedback_form') != FORM:
        raise ValueError('RESEARCH_FEEDBACK_FORM_CHANGED')
    if not isinstance(draft.get('checkpoints'), list) or not draft['checkpoints']:
        raise ValueError('RESEARCH_FEEDBACK_CHECKPOINTS_REQUIRED')
    for cp in draft['checkpoints']:
        if set(cp) != {'checkpoint_id', 'target', 'automatic_material_state', 'stop_reason',
                'compiled_sha256', 'doctor_view_sha256', 'visible_materials', 'no_visible_material',
                'human_or_clinical_status'}:
            raise ValueError('RESEARCH_FEEDBACK_CHECKPOINT_FIELDS_INVALID')
        if cp['checkpoint_id'] != cp['target']['checkpoint_id'] or cp['human_or_clinical_status'] != 'NOT_ASSESSED':
            raise ValueError('RESEARCH_FEEDBACK_CHECKPOINT_BINDING_INVALID')
        if cp['no_visible_material'] != all(not m['text'] or m['text'] == '[资料按方案遮蔽]' for m in cp['visible_materials']):
            raise ValueError('RESEARCH_FEEDBACK_VISIBILITY_FLAG_INVALID')
    return True
