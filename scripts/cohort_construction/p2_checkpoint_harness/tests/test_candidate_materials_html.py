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
        target=dict(source_ids=[checkpoint_id + "-SOURCE"], title=title),
        included_sources=[source(checkpoint_id + "-SOURCE", title, text)],
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


class CandidateMaterialsHtmlTests(unittest.TestCase):
    def test_pre_exam_header_has_no_exam_title_and_first_screen_has_required_fields(self):
        pre = packet(
            "CP-PRE", "pre_exam_evaluation", title="检查前管理评价",
            text="当时病情稳定。[目标检查计划已遮蔽]", original="研究原始医嘱已合成遮蔽。")
        page = render_html(material([pre]), "manifest-sha")

        self.assertNotIn("胰腺MR平扫+增强", page)
        self.assertIn("检查前管理评价", page)
        self.assertIn("管理问题", page)
        self.assertIn("时间依据", page)
        self.assertIn("exam_time", page)
        self.assertIn("MASK 范围、理由与遮蔽后全文", page)
        self.assertIn('name="mode"', page)
        self.assertIn('name="action_type"', page)
        self.assertIn('name="target"', page)
        self.assertIn('name="timing"', page)
        self.assertIn("研究审核，不是医生投影", page)
        self.assertIn("迁移 summary", page)
        self.assertNotIn("不得展示的MASK原文", page)
        self.assertNotIn("不得嵌入的迁移原始quote", page)

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
    def test_top_and_bottom_sequence_navigation_cross_episode_boundaries(self):
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

        execute_page(page, r"""
assert.equal(current.checkpoint_id,'CP-PRE');
assert.equal(current.sequence_navigation.next_checkpoint_id,'CP-POST');
let next=document.querySelectorAll('[data-navigation="sequence-next"]');
assert.equal(next.length,2);
assert(next.every(button=>button.dataset.goto==='CP-POST'&&!button.disabled));
assert(document.querySelectorAll('[data-navigation="episode-next"]').every(button=>button.disabled));
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
assert(document.querySelectorAll('[data-navigation="episode-previous"]').every(button=>button.disabled));
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
