"""Synthetic regression tests for the private candidate Checkpoint renderer."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from scripts.cohort_construction.p2_checkpoint_harness.stages.candidate_materials_html import (
    render_html,
    write_html,
)


def action_space():
    return dict(
        modes=["ADVANCE", "HOLD_FOR_EVIDENCE", "REDIRECT"],
        action_plan_schema=dict(
            required=["mode", "actions", "rationale"],
            properties=dict(actions=dict(minItems=1)),
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
        checkpoint_id=checkpoint_id, kind=kind,
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


_DOM_SHIM = r"""
const assert=require('node:assert/strict');
class Element {
 constructor(){this.value='';this.children=[];this.dataset={};this.disabled=false;this.listeners={};}
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
const actionForm=new Element();
const document={
 createElement(){return new Element();},
 querySelector(selector){
  if(selector.startsWith('#'))return elements[selector.slice(1)]||null;
  if(selector==='.action-form')return actionForm;
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
const localStorage={getItem(){return null;},setItem(){}};
const window={scrollTo(){}};
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
    def test_state_transition_header_exposes_goal_phase_cycle_and_new_evidence(self):
        post = packet(
            "CP-POST", "AFTER_EVIDENCE_ARRIVAL", title="新证据到达后状态",
            text="合成新病理报告。", original="合成新病理报告。", index=2)
        post.update(management_goal=dict(label="持续根治切除评估"),
                    phase="INITIAL_RESECTABILITY_ASSESSMENT",
                    action_cycle_id="AC-SYN", evidence_state_id="ES-SYN",
                    support_source_ids=["S-CONTEXT"])
        packet_page = rendered_packet_html(render_html(material([post]), "manifest-sha"),
                                           "CP-POST")
        self.assertIn("持续根治切除评估", packet_page)
        self.assertIn("INITIAL_RESECTABILITY_ASSESSMENT", packet_page)
        self.assertIn("AC-SYN", packet_page)
        self.assertIn("ES-SYN", packet_page)
        self.assertIn("当前报告作为新证据完整可见", packet_page)
        self.assertIn("S-CONTEXT", packet_page)
        self.assertNotIn("pre_exam_evaluation", packet_page)

    def test_evidence_adjudication_shows_comparator_reason_quotes_and_merged_support(self):
        post = packet(
            "CP-POST", "AFTER_EVIDENCE_ARRIVAL", title="新证据到达后状态",
            text="合成新病理报告。", original="合成新病理报告。", index=2)
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
            render_html(material([post]), "manifest-sha"), "CP-POST")
        self.assertIn("比较的最近保留 CP：CP-PRE", packet_page)
        self.assertIn("NEW_EVIDENCE_ROUND", packet_page)
        self.assertIn("病理结果改变了可见证据状态", packet_page)
        self.assertIn("来源 S-PATH", packet_page)
        self.assertIn("未见远处转移，新病理", packet_page)
        self.assertIn("归并到本 CP 的支持记录：CP-REPEATED", packet_page)
        self.assertIn("只是复述EUS+FNA和择期手术", packet_page)
        self.assertIn("SAME_ROUND_SUPPORT", packet_page)

    def test_before_acquisition_candidate_action_is_researcher_only_not_default_header(self):
        pre = packet(
            "CP-PRE", "BEFORE_ACQUISITION", title="证据获取前状态",
            text="[目标检查及报告已遮蔽]", original="合成目标检查安排")
        pre["candidate_actions"] = [dict(
            action_type="ORDER_DIAGNOSTIC_TEST", target="胰腺增强MRI",
            reason="报告中的血管接触结果不得进入检查前任务头",
            actor="discovery_llm", review_source="synthetic_discovery")]
        packet_page = rendered_packet_html(render_html(material([pre]), "manifest-sha"))
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
        packet_page = rendered_packet_html(render_html(material([pre]), "manifest-sha"))

        self.assertIn("目标原文截止后创建，未进入当时可见资料", packet_page)
        self.assertIn("CP-PRE-SOURCE", packet_page)
        self.assertIn("当时可见上下文：合成病史、体检及既有检验。", packet_page)

    def test_pre_exam_header_has_no_exam_title_and_first_screen_has_required_fields(self):
        pre = packet(
            "CP-PRE", "pre_exam_evaluation", title="检查前管理评价",
            text="当时病情稳定。[目标检查计划已遮蔽]", original="研究原始医嘱已合成遮蔽。")
        page = render_html(material([pre]), "manifest-sha")
        packet_page = rendered_packet_html(page)

        self.assertNotIn("胰腺MR平扫+增强", page)
        self.assertIn("检查前管理评价", packet_page)
        self.assertIn("管理问题", packet_page)
        self.assertIn("时间依据", packet_page)
        self.assertIn("exam_time", packet_page)
        self.assertIn("MASK 范围、理由与遮蔽后全文", packet_page)
        self.assertIn('name="mode"', packet_page)
        self.assertIn('name="action_type"', packet_page)
        self.assertIn('name="target"', packet_page)
        self.assertIn('name="timing"', packet_page)
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
        page = render_html(material([pre]), "manifest-sha")
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

        packet_page = rendered_packet_html(render_html(material([pre]), "manifest-sha"))

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

        page = render_html(material([pre]), "manifest-sha")

        self.assertIn("关系：未分类", page)
        self.assertIn("说明：未记录直接关系", page)

    def test_missing_candidate_actions_has_visible_technical_failure(self):
        pre = packet(
            "CP-PRE", "pre_exam_evaluation", title="检查前管理评价",
            text="[已遮蔽]", original="合成研究原文")
        del pre["candidate_actions"]
        page = render_html(material([pre]), "manifest-sha")

        self.assertIn("Agent候选动作（不是答案/Reference）", page)
        self.assertIn("技术失败警示", page)
        self.assertIn("未提供有效的 candidate_actions", page)
        self.assertIn("不表示临床上没有可行动作", page)

    def test_post_report_shows_synthetic_report_and_is_offline(self):
        post = packet(
            "CP-POST", "evidence_arrival_reassessment", title="报告后再评价",
            text="合成报告原文：胰腺病灶边界清楚，未见远处转移。",
            original="合成报告原文：胰腺病灶边界清楚，未见远处转移。", index=2)
        page = render_html(material([post]), "manifest-sha")

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
        page = render_html(material([second, first]), "manifest-sha")

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
        with tempfile.TemporaryDirectory() as directory:
            receipt = write_html(material([pre]), {"manifest_sha256": "abc123"}, directory)
            index = Path(directory) / "index.html"
            saved_receipt = json.loads((Path(directory) / "render-receipt.json").read_text())

            self.assertTrue(index.is_file())
            self.assertEqual(receipt, saved_receipt)
            self.assertEqual(receipt["network_calls"], 0)
            self.assertEqual(receipt["provider_requests"], 0)
            self.assertFalse(receipt["doctor_release"])
            self.assertIn("abc123", index.read_text())


if __name__ == "__main__":
    unittest.main()
