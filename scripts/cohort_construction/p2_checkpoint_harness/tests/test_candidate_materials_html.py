"""Synthetic regression tests for the private candidate Checkpoint renderer."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from scripts.cohort_construction.p2_checkpoint_harness.stages.candidate_materials import (
    candidate_material_manifest_sha256,
)
from scripts.cohort_construction.p2_checkpoint_harness.stages.candidate_materials_html import (
    _form_sha256,
    render_html,
    write_html,
)


def action_space():
    return dict(
        schema_version="management-action-plan-0.2.0",
        modes=["ADVANCE", "HOLD_FOR_EVIDENCE", "REDIRECT"],
        plan_statuses=["ACTIONS_PROPOSED", "NO_NEW_ACTION", "ABSTAIN"],
        action_plan_schema=dict(
            required=["plan_status", "actions", "rationale"],
            properties=dict(actions=dict(items=dict(required=["action_type", "target"]))),
        ),
        answer_prefilled=False,
        note="动作空间不是程序推荐答案。",
    )


def source(source_id, title, text):
    return dict(
        source_id=source_id, title=title, kind="diagnostic_report",
        clock=dict(value="2026-03-02 12:00", precision="minute"),
        clocks=dict(
            exam_time=dict(value="2026-03-02 08:00"),
            report_time=dict(value="2026-03-02 12:00"),
            verified_time=dict(value="2026-03-02 13:00"),
        ),
        text=text, presentation_scope="synthetic_masked_projection",
    )


def packet(checkpoint_id, kind, *, title, text, original, index=1):
    other = "CP-POST" if checkpoint_id == "CP-PRE" else "CP-PRE"
    previous_id = None if index == 1 else other
    next_id = other if index == 1 else None
    return dict(
        checkpoint_id=checkpoint_id, kind=kind, content_sha256="content-" + checkpoint_id,
        management_question="当时应推进、暂缓取证还是调整管理路径？",
        evaluation=dict(
            clock=dict(value="2026-03-02 08:00", precision="minute"),
            time_basis="exam_time" if kind == "pre_exam_evaluation" else "report_time",
            role=kind,
        ),
        action_space=action_space(),
        candidate_actions=[dict(
            action_type="HOLD_FOR_EVIDENCE", target=checkpoint_id + "-TARGET",
            reason="合成候选动作理由", rationale="当时上下文提示需要补充证据。",
            actor="discovery_agent", review_source="synthetic_discovery",
        )],
        discovery_review_status="REVIEWED",
        target=dict(source_ids=[checkpoint_id + "-SOURCE"], title=title,
                    document_profile="preoperative_summary"),
        included_sources=[
            source(checkpoint_id + "-SOURCE", title, text),
            source(checkpoint_id + "-CONTEXT", "同期病史与体检",
                   "当时可见上下文：合成病史、体检及既有检验。"),
        ],
        excluded_sources=[],
        researcher_target_originals=[source(
            checkpoint_id + "-ORIGINAL", "研究审核原始文书", original)],
        masked_projection=dict(
            reason="避免目标检查选择、结果或后续管理答案泄漏。",
            masked_spans=[dict(
                source_id=checkpoint_id + "-SOURCE", start=0, end=6,
                quote="不得展示的MASK原文", mask_reason="TARGET_ANSWER_LEAKAGE")],
            whole_source_ids=[], cross_document_reviewed=True,
        ),
        related_checkpoints=[dict(
            checkpoint_id=other, relation="evidence_arrival_reassessment",
            explanation="报告到达后的再评价。")],
        episode_id="EP-SYN",
        episode_navigation=dict(
            index=index, total=2, previous_checkpoint_id=previous_id,
            next_checkpoint_id=next_id),
        display_order=dict(index=index, total=2),
    )


def material(packets):
    return dict(
        case_id="SYNTHETIC-CASE", format="synthetic-candidate-material-0.3",
        packets=packets, summary=dict(candidate_checkpoint_count=len(packets)),
        migrations=[dict(
            event_id="OLD-1", migration_type="candidate_span",
            migration_status="RETAINED", quote="不得嵌入的迁移原始quote")],
    )


def render_material(value):
    return render_html(value, candidate_material_manifest_sha256(value))


_DOM_SHIM = r"""
const assert=require('node:assert/strict');
class Element {
 constructor(){this.value='';this.children=[];this.dataset={};this.disabled=false;this.hidden=false;this.required=false;this.listeners={};}
 addEventListener(type,listener){(this.listeners[type]||(this.listeners[type]=[])).push(listener);}
 append(child){this.children.push(child);}
 replaceChildren(){this.children=[];}
 click(){if(!this.disabled)for(const listener of this.listeners.click||[])listener({currentTarget:this});}
}
const elements={};
for(const id of ['kind','checkpoint','material','position','export','add-action'])elements[id]=new Element();
let packetButtons=[];
elements.material.innerHTML='';
Object.defineProperty(elements.material,'innerHTML',{
 get(){return this._html;},
 set(value){
  this._html=value;packetButtons=[];
  for(const match of value.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)){
   const button=new Element();button.textContent=match[2].replace(/<[^>]+>/g,'');
   for(const attribute of match[1].matchAll(/([\w-]+)(?:="([^"]*)")?/g)){
    const name=attribute[1],value=attribute[2]||'';
    if(name==='disabled')button.disabled=true;
    if(name.startsWith('data-'))button.dataset[name.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=value;
   }
   if(button.dataset.goto!==undefined)packetButtons.push(button);
  }
 }
});
const actionForm=new Element(),actionFields={},actionsElement=new Element(),evidenceFieldElement=new Element();
actionsElement.querySelectorAll=selector=>selector==='.action-row'?actionsElement.children:[];
for(const name of ['plan_status','mode','rationale','evidence_to_obtain']){
 const field=new Element();field.value=name==='plan_status'?'ACTIONS_PROPOSED':name==='mode'?'ADVANCE':'';
 actionFields[`[name="${name}"]`]=field;
}
actionForm.querySelector=selector=>selector==='.actions'?actionsElement:
 selector==='.evidence-to-obtain-field'?evidenceFieldElement:actionFields[selector]||null;
actionForm.querySelectorAll=selector=>selector==='.action-row'?actionsElement.children:[];
actionForm.checkValidity=()=>true;actionForm.reportValidity=()=>{};
const document={
 createElement(){return new Element();},
 querySelector(selector){
  if(selector.startsWith('#'))return elements[selector.slice(1)]||null;
  if(selector==='.action-form')return actionForm;
  if(selector==='.actions')return actionsElement;
  return null;
 },
 querySelectorAll(selector){
  if(selector==='[data-goto]')return packetButtons;
  if(selector==='[data-workflow]'||selector==='.action-row')return [];
  const match=selector.match(/^\[data-navigation="([^"]+)"\]$/);
  return match?packetButtons.filter(button=>button.dataset.navigation===match[1]):[];
 }
};
const location={hash:''};
const history={replaceState(_state,_title,hash){location.hash=hash;}};
const storage=new Map();
const localStorage={getItem(key){return storage.has(key)?storage.get(key):null;},setItem(key,value){storage.set(key,value);}};
const window={scrollTo(){}};
function testField(value=''){const field=new Element();field.value=value;return field;}
function testActionRow(action={}){
 const row=new Element(),fields={};
 for(const name of ['action_type','target','timing','action_id','uncertainty_addressed','dependencies','conditions'])
  fields[`[name="${name}"]`]=testField(Array.isArray(action[name])?action[name].join('\n'):action[name]||'');
 row.querySelector=selector=>fields[selector]||null;return row;
}
function testResponseForm(status='ACTIONS_PROPOSED',actions=[]){
 const form=new Element(),fields={
  '[name="plan_status"]':testField(status),'[name="mode"]':testField('ADVANCE'),
  '[name="rationale"]':testField('合成理由'),'[name="evidence_to_obtain"]':testField('')};
 const rows=actions.map(testActionRow);form.querySelector=selector=>fields[selector]||null;
 form.querySelectorAll=selector=>selector==='.action-row'?rows:[];return form;
}
"""


def execute_page(page, assertions):
    script = re.search(r"<script>([\s\S]*)</script>", page).group(1)
    return subprocess.run(
        [shutil.which("node")], input=_DOM_SHIM + script + assertions,
        text=True, capture_output=True, check=True, timeout=10,
    )


def rendered_packet_html(page, checkpoint_id="CP-PRE"):
    script = re.search(r"<script>([\s\S]*)</script>", page).group(1)
    payload = json.loads(re.search(r"const DATA=(.*);\nconst caseId=", script).group(1))
    return next(item["html"] for item in payload if item["checkpoint_id"] == checkpoint_id)


class CandidateMaterialsHtmlTests(unittest.TestCase):
    def test_render_recomputes_manifest_and_rejects_caller_or_material_tamper(self):
        value = material([packet(
            "CP-PRE", "pre_exam_evaluation", title="检查前管理评价",
            text="[已遮蔽]", original="合成研究原文")])
        expected = candidate_material_manifest_sha256(value)
        page = render_html(value, expected)
        self.assertIn(expected, page)
        with self.assertRaisesRegex(ValueError, "CANDIDATE_MATERIAL_MANIFEST_SHA256_MISMATCH"):
            render_html(value, "caller-supplied-wrong-hash")
        value["summary"]["candidate_checkpoint_count"] = 999
        with self.assertRaisesRegex(ValueError, "CANDIDATE_MATERIAL_MANIFEST_SHA256_MISMATCH"):
            render_html(value, expected)

    def test_form_hash_binds_blank_form_and_schema_not_candidate_answers(self):
        value = material([packet(
            "CP-PRE", "pre_exam_evaluation", title="检查前管理评价",
            text="[已遮蔽]", original="合成研究原文")])
        before = _form_sha256(value["packets"])
        value["packets"][0]["candidate_actions"][0]["target"] = "变化的研究候选动作"
        self.assertEqual(_form_sha256(value["packets"]), before)
        value["packets"][0]["action_space"]["action_plan_schema"]["required"].append("tampered")
        self.assertNotEqual(_form_sha256(value["packets"]), before)

    def test_state_transition_header_exposes_goal_phase_cycle_and_new_evidence(self):
        post = packet(
            "CP-POST", "AFTER_EVIDENCE_ARRIVAL", title="新证据到达后状态",
            text="合成新病理报告。", original="合成新病理报告。", index=2)
        post.update(management_goal=dict(label="持续根治切除评估"),
                    phase="INITIAL_RESECTABILITY_ASSESSMENT",
                    action_cycle_id="AC-SYN", evidence_state_id="ES-SYN",
                    support_source_ids=["S-CONTEXT"])
        packet_page = rendered_packet_html(render_material(material([post])),
                                           "CP-POST")
        self.assertIn("持续根治切除评估", packet_page)
        self.assertIn("INITIAL_RESECTABILITY_ASSESSMENT", packet_page)
        self.assertIn("AC-SYN", packet_page)
        self.assertIn("ES-SYN", packet_page)
        self.assertIn("当前报告作为新证据完整可见", packet_page)
        self.assertIn("S-CONTEXT", packet_page)
        self.assertNotIn("pre_exam_evaluation", packet_page)

    def test_task_header_shows_upstream_fields_actual_sources_and_pending_review(self):
        pre = packet(
            "CP-PRE", "pre_exam_evaluation", title="检查前管理评价",
            text="[已遮蔽]", original="合成研究原文")
        pre["task_header"] = dict(
            next_key_surgical_step="上游手术步骤",
            allowed_time_precision=["date", "minute"],
            knowledge_version="SYNTHETIC-KNOWLEDGE-V1")
        packet_page = rendered_packet_html(render_material(material([pre])))
        header = packet_page.split('id="workflow-original"', 1)[0]
        self.assertIn("下一关键手术步骤", header)
        self.assertIn("上游手术步骤", header)
        self.assertIn("允许时间精度", header)
        self.assertIn("date, minute", header)
        self.assertIn("知识版本", header)
        self.assertIn("SYNTHETIC-KNOWLEDGE-V1", header)
        self.assertIn("实际 included source IDs", header)
        self.assertIn("CP-PRE-SOURCE, CP-PRE-CONTEXT", header)

        post = packet(
            "CP-POST", "AFTER_EVIDENCE_ARRIVAL", title="新证据状态",
            text="合成报告", original="合成报告", index=2)
        missing = rendered_packet_html(render_material(material([post])), "CP-POST")
        self.assertIn("下一关键手术步骤", missing)
        self.assertGreaterEqual(missing.count("待核"), 3)

    def test_clock_degradation_and_same_report_clock_are_visible(self):
        pre = packet(
            "CP-PRE", "pre_exam_evaluation", title="检查前管理评价",
            text="[已遮蔽]", original="合成研究原文")
        pre["evaluation"]["clock_degradation"] = dict(
            degraded=True, basis="report_time_fallback_for_pre_exam_evaluation",
            same_clock_as_evidence_arrival=True,
            reason="REPORT_VISIBILITY_USED_FOR_PRE_EXAM_EVALUATION")
        page = rendered_packet_html(render_material(material([pre])))
        self.assertIn("时钟降级：report_time_fallback_for_pre_exam_evaluation", page)
        self.assertIn("与证据到达评价同钟：是", page)

    def test_non_primary_checkpoint_is_explicitly_excluded_from_main_scoring(self):
        post = packet(
            "CP-POST", "AFTER_EVIDENCE_ARRIVAL", title="术后新证据状态",
            text="合成术后复查报告。", original="合成术后复查报告。", index=2)
        post["evaluation"].update(
            primary_evaluation_eligible=False,
            scope="NON_PRIMARY_P2_POSTOPERATIVE_TRAJECTORY",
            scope_reason="AFTER_OPERATION_START_NON_PRIMARY_P2_TRAJECTORY")
        post["action_task_review_flags"] = ["ACTION_TASK_MISMATCH_REVIEW_REQUIRED"]
        page = rendered_packet_html(render_material(material([post])), "CP-POST")
        self.assertIn("否：非本版主要评价", page)
        self.assertIn("ACTION_TASK_MISMATCH_REVIEW_REQUIRED", page)
        self.assertIn("候选动作原样保留", page)
        self.assertIn("不进入术前路径推进／NAT再评估主评分", page)
        self.assertIn("NON_PRIMARY_P2_POSTOPERATIVE_TRAJECTORY", page)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for DOM execution")
    def test_export_partitions_primary_and_non_primary_responses(self):
        post = packet(
            "CP-POST", "AFTER_EVIDENCE_ARRIVAL", title="术后新证据状态",
            text="合成术后复查报告。", original="合成术后复查报告。", index=2)
        post["evaluation"].update(
            primary_evaluation_eligible=False,
            scope="NON_PRIMARY_P2_POSTOPERATIVE_TRAJECTORY",
            scope_reason="AFTER_OPERATION_START_NON_PRIMARY_P2_TRAJECTORY")
        page = render_material(material([post]))
        execute_page(page, r"""
assert.equal(current.primary_evaluation_eligible,false);
assert.equal(current.evaluation_scope,'NON_PRIMARY_P2_POSTOPERATIVE_TRAJECTORY');
const partitioned=partitionResponses([
 {checkpoint_id:'PRE',primary_evaluation_eligible:true},
 {checkpoint_id:'POST',primary_evaluation_eligible:false}
]);
assert.deepEqual(partitioned.responses.map(item=>item.checkpoint_id),['PRE']);
assert.deepEqual(partitioned.non_primary_p2_review_responses.map(item=>item.checkpoint_id),['POST']);
""")

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for DOM execution")
    def test_response_supports_optional_action_semantics_no_new_action_and_abstain(self):
        pre = packet(
            "CP-PRE", "pre_exam_evaluation", title="检查前管理评价",
            text="[已遮蔽]", original="合成研究原文")
        page = render_material(material([pre]))
        execute_page(page, r"""
const proposed=response(testResponseForm('ACTIONS_PROPOSED',[{
 action_type:'MANAGE',target:'合成目标',action_id:'A-1',uncertainty_addressed:'待明确问题',
 dependencies:['前置动作'],conditions:['适用条件']
}]));
assert.equal(proposed.response_status,'SUBMITTED');
assert.equal(proposed.plan_status,'ACTIONS_PROPOSED');
assert.equal(proposed.actions.length,1);
assert.equal('timing' in proposed.actions[0],false);
assert.equal(proposed.actions[0].action_id,'A-1');
assert.deepEqual(proposed.actions[0].dependencies,['前置动作']);
const proposedForm=testResponseForm('ACTIONS_PROPOSED',[{action_type:'MANAGE',target:'目标'}]);
proposedForm.querySelector('[name="evidence_to_obtain"]').value='增强CT\nCA19-9';
assert.deepEqual(response(proposedForm).evidence_to_obtain,['增强CT','CA19-9']);
const noNewForm=testResponseForm('NO_NEW_ACTION',[{action_type:'OLD',target:'不得导出'}]);
noNewForm.querySelector('[name="evidence_to_obtain"]').value='不得带回检查';
const noNew=response(noNewForm);
assert.deepEqual(noNew.actions,[]);assert.deepEqual(noNew.evidence_to_obtain,[]);
assert.equal(noNew.mode,'ADVANCE');
const abstainForm=testResponseForm('ABSTAIN',[{action_type:'OLD',target:'不得导出'}]);
abstainForm.querySelector('[name="evidence_to_obtain"]').value='技术拒答不得带回检查';
const abstain=response(abstainForm);
assert.deepEqual(abstain.actions,[]);assert.deepEqual(abstain.evidence_to_obtain,[]);
assert.equal('mode' in abstain,false);
assert.equal(abstain.plan_status,'ABSTAIN');
actionFields['[name="evidence_to_obtain"]'].value='切换后必须清空';
actionFields['[name="plan_status"]'].value='NO_NEW_ACTION';
for(const listener of actionFields['[name="plan_status"]'].listeners.change||[])listener();
assert.equal(actionsElement.hidden,true);assert.equal(elements['add-action'].hidden,true);
assert.equal(evidenceFieldElement.hidden,true);
assert.equal(actionFields['[name="evidence_to_obtain"]'].disabled,true);
assert.equal(actionFields['[name="evidence_to_obtain"]'].value,'');
actionFields['[name="plan_status"]'].value='ABSTAIN';
for(const listener of actionFields['[name="plan_status"]'].listeners.change||[])listener();
assert.equal(actionFields['[name="mode"]'].disabled,true);
assert.equal(actionFields['[name="mode"]'].required,false);
actionFields['[name="plan_status"]'].value='ACTIONS_PROPOSED';
for(const listener of actionFields['[name="plan_status"]'].listeners.change||[])listener();
assert.equal(actionsElement.hidden,false);assert.equal(actionFields['[name="mode"]'].disabled,false);
assert.equal(evidenceFieldElement.hidden,false);
assert.equal(actionFields['[name="evidence_to_obtain"]'].disabled,false);
""")

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for DOM execution")
    def test_export_and_restore_require_matching_manifest_form_version_and_form_hash(self):
        post = packet(
            "CP-POST", "AFTER_EVIDENCE_ARRIVAL", title="术后新证据状态",
            text="合成术后复查报告。", original="合成术后复查报告。", index=2)
        post["evaluation"].update(
            primary_evaluation_eligible=False,
            scope="NON_PRIMARY_P2_POSTOPERATIVE_TRAJECTORY",
            scope_reason="AFTER_OPERATION_START_NON_PRIMARY_P2_TRAJECTORY")
        page = render_material(material([post]))
        execute_page(page, r"""
assert(key(current.checkpoint_id).includes(manifestSha));
let output=exportData();
assert.deepEqual(output.responses,[]);
assert.deepEqual(output.non_primary_p2_review_responses,[]);
assert.equal(output.unsubmitted_checkpoints.length,1);
assert.equal(output.unsubmitted_checkpoints[0].response_status,'UNSUBMITTED');
const saved=response(testResponseForm('NO_NEW_ACTION'));
assert(saved.form_sha256.length===64);
assert(key(current.checkpoint_id).includes(saved.form_version));
assert(key(current.checkpoint_id).includes(saved.form_sha256));
localStorage.setItem(key(current.checkpoint_id),JSON.stringify(saved));
output=exportData();
assert.equal(output.form_sha256,saved.form_sha256);
assert.equal(output.non_primary_p2_review_responses.length,1);
assert.equal(output.non_primary_p2_review_responses[0].response_status,'SUBMITTED');
assert.deepEqual(output.unsubmitted_checkpoints,[]);
localStorage.setItem(key(current.checkpoint_id),JSON.stringify({...saved,material_manifest_sha256:'old-material'}));
actionFields['[name="rationale"]'].value='不得恢复';restore(current.checkpoint_id);
assert.equal(actionFields['[name="rationale"]'].value,'不得恢复');
assert.equal(exportData().non_primary_p2_review_responses.length,0);
assert.equal(exportData().unsubmitted_checkpoints.length,1);
localStorage.setItem(key(current.checkpoint_id),JSON.stringify({...saved,form_version:'old-form'}));
assert.equal(exportData().non_primary_p2_review_responses.length,0);
localStorage.setItem(key(current.checkpoint_id),JSON.stringify({...saved,form_sha256:'stale-form'}));
actionFields['[name="rationale"]'].value='stale form不得恢复';restore(current.checkpoint_id);
assert.equal(actionFields['[name="rationale"]'].value,'stale form不得恢复');
assert.equal(exportData().non_primary_p2_review_responses.length,0);
assert.equal(exportData().unsubmitted_checkpoints.length,1);
localStorage.setItem(key(current.checkpoint_id),JSON.stringify({...saved,evidence_to_obtain:['偷带检查']}));
assert.equal(exportData().non_primary_p2_review_responses.length,0);
localStorage.setItem('candidate-action-draft-'+caseId+'-'+current.checkpoint_id,JSON.stringify(saved));
assert.equal(exportData().non_primary_p2_review_responses.length,0);
localStorage.setItem(key(current.checkpoint_id),JSON.stringify(saved));restore(current.checkpoint_id);
assert.equal(actionFields['[name="plan_status"]'].value,'NO_NEW_ACTION');
assert.equal(actionFields['[name="rationale"]'].value,'合成理由');
assert.equal(exportData().non_primary_p2_review_responses.length,1);
""")

    def test_evidence_adjudication_shows_comparator_reason_quotes_and_merged_support(self):
        post = packet(
            "CP-POST", "AFTER_EVIDENCE_ARRIVAL", title="新证据到达后状态",
            text="合成新病理报告。", original="合成新病理报告。", index=2)
        post["included_sources"].append(source(
            "S-PATH", "合成病理", "检查所见：未见远处转移，新病理。"))
        post.update(
            global_adjudication=dict(
                checkpoint_id="CP-POST", disposition="RETAIN_CP",
                evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id="CP-PRE",
                rationale="病理结果改变了可见证据状态。",
                evidence_supports=[dict(source_id="S-PATH", start=4, end=16,
                                        quote="未见远处转移，新病理")]),
            supporting_adjudications=[dict(
                checkpoint_id="CP-REPEATED", disposition="SUPPORT_ONLY",
                evolution="SAME_ROUND_SUPPORT", comparator_checkpoint_id="CP-POST",
                rationale="转入记录只是复述EUS+FNA和择期手术，没有新增患者证据。",
                evidence_supports=[])],
        )
        packet_page = rendered_packet_html(
            render_material(material([post])), "CP-POST")
        header = packet_page.split('id="workflow-original"', 1)[0]
        research = packet_page.split('id="workflow-research"', 1)[1]
        self.assertIn("比较的最近保留 CP：CP-PRE", packet_page)
        self.assertIn("NEW_EVIDENCE_ROUND", packet_page)
        self.assertNotIn("病理结果改变了可见证据状态", header)
        self.assertIn("病理结果改变了可见证据状态", research)
        self.assertIn("来源 S-PATH", packet_page)
        self.assertIn("未见远处转移，新病理", header)
        self.assertIn("归并到本 CP 的支持记录：CP-REPEATED", packet_page)
        self.assertNotIn("只是复述EUS+FNA和择期手术", header)
        self.assertIn("只是复述EUS+FNA和择期手术", research)
        self.assertIn("SAME_ROUND_SUPPORT", packet_page)

    def test_adjudication_rationale_and_masked_quote_stay_researcher_only(self):
        pre = packet(
            "CP-PRE", "BEFORE_ACQUISITION", title="证据获取前状态",
            text="[目标检查已遮蔽]", original="研究目标检查原文")
        pre["global_adjudication"] = dict(
            checkpoint_id="CP-PRE", disposition="REVIEW_REQUIRED",
            evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id="CP-EARLIER",
            rationale="未来MRI未见远处转移，不得进入当前任务头。",
            evidence_supports=[dict(source_id="CP-PRE-SOURCE", start=0, end=9,
                                    quote="未来MRI未见远处转移")])
        packet_page = rendered_packet_html(render_material(material([pre])))
        header = packet_page.split('id="workflow-original"', 1)[0]
        research = packet_page.split('id="workflow-research"', 1)[1]
        self.assertNotIn("未来MRI未见远处转移", header)
        self.assertIn("引文未进入本 CP 遮蔽后可见资料", header)
        self.assertIn("Agent 指定的比较 CP：CP-EARLIER", header)
        self.assertNotIn("比较的最近保留 CP：CP-EARLIER", header)
        self.assertNotIn("Agent 指定的新增患者证据（逐字引文）", header)
        self.assertIn("未来MRI未见远处转移", research)
        self.assertIn("不得进入当前任务头", research)

    def test_masked_citation_span_stays_hidden_when_quote_repeats_elsewhere(self):
        post = packet(
            "CP-POST", "AFTER_EVIDENCE_ARRIVAL", title="新证据到达后状态",
            text="[目标管理判断已遮蔽]\n复查MRI示未见远处转移。", original="合成原文")
        post["included_sources"].append(source(
            "S-DUPLICATE", "重复文本来源",
            "[目标管理判断已遮蔽]\n复查MRI示未见远处转移。"))
        post["masked_projection"]["masked_spans"].append(dict(
            source_id="S-DUPLICATE", start=0, end=13, mask_reason="TARGET"))
        post["global_adjudication"] = dict(
            checkpoint_id="CP-POST", disposition="RETAIN_CP",
            evolution="NEW_EVIDENCE_ROUND", comparator_checkpoint_id="CP-PRE",
            rationale="重复引文反例", evidence_supports=[dict(
                source_id="S-DUPLICATE", start=2, end=8, quote="未见远处转移")])
        packet_page = rendered_packet_html(
            render_material(material([post])), "CP-POST")
        header = packet_page.split('id="workflow-original"', 1)[0]
        adjudication = header.split('class="adjudication"', 1)[1]
        self.assertNotIn("未见远处转移", adjudication)
        self.assertIn("引文未进入本 CP 遮蔽后可见资料", adjudication)

    def test_before_acquisition_candidate_action_is_researcher_only_not_default_header(self):
        pre = packet(
            "CP-PRE", "BEFORE_ACQUISITION", title="证据获取前状态",
            text="[目标检查及报告已遮蔽]", original="合成目标检查安排")
        pre["candidate_actions"] = [dict(
            action_type="ORDER_DIAGNOSTIC_TEST", target="胰腺增强MRI",
            reason="报告中的血管接触结果不得进入检查前任务头",
            actor="discovery_llm", review_source="synthetic_discovery")]
        packet_page = rendered_packet_html(render_material(material([pre])))
        header = packet_page.split('id="workflow-original"', 1)[0]
        research = packet_page.split('id="workflow-research"', 1)[1]
        self.assertNotIn("胰腺增强MRI", header)
        self.assertNotIn("血管接触结果", header)
        self.assertIn("已移至“研究原文”页", header)
        self.assertIn("胰腺增强MRI", research)
        self.assertIn("研究审核，不是医生投影", research)

    def test_target_created_after_cutoff_is_explicit_without_restoring_its_text(self):
        pre = packet(
            "CP-PRE", "BEFORE_ACQUISITION", title="目标状态",
            text="当时上下文仍可见。", original="截止后创建的目标原文")
        pre["included_sources"] = [pre["included_sources"][1]]
        pre["target"]["created_after_cutoff_source_ids"] = ["CP-PRE-SOURCE"]
        packet_page = rendered_packet_html(render_material(material([pre])))

        self.assertIn("目标原文截止后创建，未进入当时可见资料", packet_page)
        self.assertIn("CP-PRE-SOURCE", packet_page)
        self.assertIn("当时可见上下文：合成病史、体检及既有检验。", packet_page)

    def test_pre_exam_header_has_no_exam_title_and_first_screen_has_required_fields(self):
        pre = packet(
            "CP-PRE", "pre_exam_evaluation", title="检查前管理评价",
            text="当时病情稳定。[目标检查计划已遮蔽]", original="研究原始医嘱已合成遮蔽。")
        page = render_material(material([pre]))
        packet_page = rendered_packet_html(page)

        self.assertNotIn("胰腺MR平扫+增强", page)
        self.assertIn("检查前管理评价", packet_page)
        self.assertIn("管理问题", packet_page)
        self.assertIn("时间依据", packet_page)
        self.assertIn("exam_time", packet_page)
        self.assertIn("MASK 范围、理由与遮蔽后全文", packet_page)
        self.assertIn('name="mode"', packet_page)
        self.assertIn('name="plan_status"', packet_page)
        self.assertIn('name="action_type"', packet_page)
        self.assertIn('name="target"', packet_page)
        self.assertIn('name="action_id"', packet_page)
        self.assertIn('name="uncertainty_addressed"', packet_page)
        self.assertIn('name="dependencies"', packet_page)
        timing = re.search(r'<input name="timing"[^>]*>', packet_page).group(0)
        self.assertNotIn("required", timing)
        self.assertIn("ABSTAIN 仅表示技术拒答，不是第四种临床 Mode", packet_page)
        self.assertIn("研究审核，不是医生投影", page)
        self.assertIn("私有研究审核：含未遮原文，不是医生盲评文件。", page)
        self.assertNotIn("class='notice'", page)
        self.assertNotIn("迁移 summary", page)
        self.assertNotIn('class="migration-summary"', page)
        self.assertIn("grid-template-columns:repeat(3,minmax(0,1fr))", page)
        self.assertIn(".task-grid>div{min-width:0}", page)
        self.assertNotIn("不得展示的MASK原文", page)
        self.assertNotIn("不得嵌入的迁移原始quote", page)

    def test_first_screen_shows_candidate_actions_and_all_visible_sources(self):
        pre = packet(
            "CP-PRE", "pre_exam_evaluation", title="检查前管理评价",
            text="[目标检查结果已遮蔽]", original="合成研究原文")
        page = render_material(material([pre]))
        packet_page = rendered_packet_html(page)
        first_screen = packet_page.split('id="workflow-original"', 1)[1].split(
            'id="workflow-research"', 1)[0]

        self.assertIn("Agent候选动作（不是答案/Reference）", packet_page)
        self.assertIn("HOLD_FOR_EVIDENCE", packet_page)
        self.assertIn("CP-PRE-TARGET", packet_page)
        self.assertIn("合成候选动作理由", packet_page)
        self.assertIn("当时上下文提示需要补充证据。", packet_page)
        self.assertIn("发现复核状态：REVIEWED", packet_page)
        self.assertIn("目标来源", first_screen)
        self.assertIn("上下文来源", first_screen)
        self.assertIn("[目标检查结果已遮蔽]", first_screen)
        self.assertIn("当时可见上下文：合成病史、体检及既有检验。", first_screen)
        self.assertNotIn("技术失败警示", first_screen)
        self.assertLess(packet_page.index("评价时点"), packet_page.index("Agent候选动作"))
        self.assertLess(packet_page.index("Agent候选动作"), packet_page.index('id="workflow-original"'))
        self.assertIn('data-workflow="mask"', packet_page)
        self.assertIn('data-workflow="research"', packet_page)
        self.assertIn('data-workflow="relations"', packet_page)
        self.assertIn('data-workflow="action"', packet_page)

    def test_visible_sources_use_daily_folds_and_current_document_profile(self):
        pre = packet(
            "CP-PRE", "pre_exam_evaluation", title="术前小结",
            text="[已遮蔽]", original="合成研究原文")
        pre["included_sources"][1]["clock"]["value"] = "2026-03-01 09:00"

        packet_page = rendered_packet_html(render_material(material([pre])))

        self.assertIn("当前CP原始文书类型", packet_page)
        self.assertIn("术前小结（preoperative_summary）", packet_page)
        self.assertIn("2026-03-02（24小时） · 1 份来源 · 当前CP评价日", packet_page)
        self.assertIn("2026-03-01（24小时） · 1 份来源", packet_page)
        groups = re.findall(r'<details class="day-source-group"([^>]*)>', packet_page)
        self.assertEqual(len(groups), 2)
        self.assertIn("open", groups[0])
        self.assertNotIn("open", groups[1])

    def test_adjacent_episode_member_without_direct_edge_has_readable_relation(self):
        pre = packet(
            "CP-PRE", "pre_exam_evaluation", title="检查前管理评价",
            text="[已遮蔽]", original="合成研究原文")
        pre["related_checkpoints"] = []

        page = render_material(material([pre]))

        self.assertIn("关系：未分类", page)
        self.assertIn("说明：未记录直接关系", page)

    def test_missing_candidate_actions_has_visible_technical_failure(self):
        pre = packet(
            "CP-PRE", "pre_exam_evaluation", title="检查前管理评价",
            text="[已遮蔽]", original="合成研究原文")
        del pre["candidate_actions"]
        page = render_material(material([pre]))

        self.assertIn("Agent候选动作（不是答案/Reference）", page)
        self.assertIn("技术失败警示", page)
        self.assertIn("未提供有效的 candidate_actions", page)
        self.assertIn("不表示临床上没有可行动作", page)

    def test_post_report_shows_synthetic_report_and_is_offline(self):
        post = packet(
            "CP-POST", "evidence_arrival_reassessment", title="报告后再评价",
            text="合成报告原文：胰腺病灶边界清楚，未见远处转移。",
            original="合成报告原文：胰腺病灶边界清楚，未见远处转移。", index=2)
        page = render_material(material([post]))

        self.assertIn("合成报告原文：胰腺病灶边界清楚，未见远处转移。", page)
        self.assertIn("report_time", page)
        self.assertIn("verified", page)
        self.assertNotIn("http", page.lower())
        self.assertIn("一次显示一个候选 Checkpoint", page)
        self.assertIn("按任务类型筛选", page)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for DOM execution")
    def test_top_and_bottom_patient_navigation_cross_episode_boundaries(self):
        first = packet(
            "CP-PRE", "pre_exam_evaluation", title="第一 Episode 管理评价",
            text="[已遮蔽]", original="合成研究原文")
        second = packet(
            "CP-POST", "management_decision", title="第二 Episode 管理评价",
            text="[已遮蔽]", original="合成研究原文", index=2)
        first["episode_id"] = "EP-ONE"
        second["episode_id"] = "EP-TWO"
        for item in (first, second):
            item["display_order"]["total"] = 2
            item["episode_navigation"] = dict(
                index=1, total=1, previous_checkpoint_id=None, next_checkpoint_id=None)
        page = render_material(material([second, first]))

        packet_page = rendered_packet_html(page)
        self.assertIn("← 本患者上一 CP", packet_page)
        self.assertIn("本患者 CP 1 / 2", packet_page)
        self.assertIn("本患者下一 CP →", packet_page)
        self.assertIn('aria-label="本患者显示顺序节点切换"', packet_page)
        self.assertNotIn('data-navigation="episode-next"', packet_page)
        self.assertNotIn('data-navigation="episode-previous"', packet_page)
        self.assertNotIn("全病例", page)
        execute_page(page, r"""
assert.equal(current.checkpoint_id,'CP-PRE');
assert.equal(current.sequence_navigation.next_checkpoint_id,'CP-POST');
let next=document.querySelectorAll('[data-navigation="sequence-next"]');
assert.equal(next.length,2);
assert(next.every(button=>button.dataset.goto==='CP-POST'&&!button.disabled));
assert.equal(document.querySelectorAll('[data-navigation="episode-next"]').length,0);
next[0].click();
assert.equal(current.checkpoint_id,'CP-POST');
assert.equal(cpSelect.value,'CP-POST');
assert.equal(location.hash,'#CP-POST');
document.querySelectorAll('[data-navigation="sequence-previous"]')[0].click();
assert.equal(current.checkpoint_id,'CP-PRE');
next=document.querySelectorAll('[data-navigation="sequence-next"]');
next[1].click();
assert.equal(current.checkpoint_id,'CP-POST');
assert.equal(cpSelect.value,'CP-POST');
assert.equal(location.hash,'#CP-POST');
assert.equal(document.querySelectorAll('[data-navigation="episode-previous"]').length,0);
""")

    def test_write_html_reuses_index_and_receipt_contract(self):
        pre = packet(
            "CP-PRE", "pre_exam_evaluation", title="检查前管理评价",
            text="[已遮蔽]", original="合成研究原文")
        value = material([pre])
        manifest_sha = candidate_material_manifest_sha256(value)
        with tempfile.TemporaryDirectory() as directory:
            receipt = write_html(value, {"manifest_sha256": manifest_sha}, directory)
            index = Path(directory) / "index.html"
            saved_receipt = json.loads((Path(directory) / "render-receipt.json").read_text())

            self.assertTrue(index.is_file())
            self.assertEqual(receipt, saved_receipt)
            self.assertEqual(receipt["network_calls"], 0)
            self.assertEqual(receipt["provider_requests"], 0)
            self.assertFalse(receipt["doctor_release"])
            self.assertIn(manifest_sha, index.read_text())
            self.assertEqual(receipt["form_sha256"], _form_sha256(value["packets"]))


if __name__ == "__main__":
    unittest.main()
