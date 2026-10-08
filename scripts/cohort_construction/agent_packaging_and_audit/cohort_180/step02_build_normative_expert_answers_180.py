from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from scripts.cohort_construction.paths import data_root

ROOT = data_root()
BASE = ROOT / "outputs" / "preop_decision_cohort_180_20261007"
INPUT = BASE / "preop_agent_inputs_180.jsonl"
INDEX = BASE / "cohort_index_180.csv"
OUT_JSONL = BASE / "preop_normative_expert_answers_180_v2.jsonl"
OUT_CSV = BASE / "preop_normative_expert_answers_180_v2.csv"
OUT_MISSING = BASE / "normative_missing_information_180_v2.csv"
OUT_RULES = BASE / "normative_source_rules_v2.json"
OUT_AUDIT = BASE / "normative_answer_audit_v2.json"


GUIDELINE_RULES = {
    "EVIDENCE_FACT_BOUNDARY": {
        "source": "规则.docx",
        "section": "医学知识的作用边界/知识使用原则",
        "rule": "只使用原文明确事实；不从结节、血管接触等自动确诊、自动推导TNM或可切除性。",
    },
    "CPC2020_IMG_STAGING": {
        "source": "中国胰腺癌综合诊治指南（2020版）",
        "section": "影像学检查",
        "rule": "增强三维动态CT用于评估肿瘤、血管关系和可切除性；MRI有助于评估肝内转移，MRCP联合动态增强MRI有助于鉴别病变及胆胰管受累。",
    },
    "CPC2020_PATHOLOGY": {
        "source": "中国胰腺癌综合诊治指南（2020版）",
        "section": "病理学检查",
        "rule": "除拟行手术切除者外，其余患者在制定治疗方案前应尽量取得病理学诊断。",
    },
    "CPC2020_MDT_RESECTABILITY": {
        "source": "中国胰腺癌综合诊治指南（2020版）",
        "section": "胰腺癌的外科治疗/热点问题六",
        "rule": "术前应开展MDT讨论，并基于高质量影像评估可切除、交界可切除、局部进展或远处转移。",
    },
    "CPC2020_RESECTABLE_SURGERY": {
        "source": "中国胰腺癌综合诊治指南（2020版）",
        "section": "可切除胰腺癌的手术治疗",
        "rule": "可切除胰头癌推荐根治性胰十二指肠切除；可切除胰体尾癌推荐根治性胰体尾联合脾脏切除。",
    },
    "CPC2020_BORDERLINE": {
        "source": "中国胰腺癌综合诊治指南（2020版）",
        "section": "交界可切除胰腺癌的治疗",
        "rule": "体能状态良好的交界可切除胰腺癌首选术前新辅助治疗。",
    },
    "CPC2020_LOCALLY_ADVANCED": {
        "source": "中国胰腺癌综合诊治指南（2020版）",
        "section": "局部进展期胰腺癌的手术治疗",
        "rule": "局部进展期不推荐直接手术，首选转化治疗；治疗前需病理证据，治疗后无进展且体能良好者可腹腔镜优先探查。",
    },
    "CPC2020_METASTATIC": {
        "source": "中国胰腺癌综合诊治指南（2020版）",
        "section": "合并远处转移的胰腺癌的手术治疗",
        "rule": "不推荐对合并远处转移的胰腺癌直接行减瘤或根治性切除；以系统治疗和症状处理为主。",
    },
    "CPC2020_SUSPECTED_METASTASIS": {
        "source": "中国胰腺癌综合诊治指南（2020版）",
        "section": "热点问题六：可切除性评估",
        "rule": "高度怀疑远处转移而CT/MRI未证实时，推荐PET-CT/PET-MRI、可疑转移灶活检，必要时腹腔镜探查。",
    },
    "CPC2020_HIGH_RISK_RESECTABLE": {
        "source": "中国胰腺癌综合诊治指南（2020版）",
        "section": "可切除胰腺癌的化疗原则",
        "rule": "可切除但存在高CA19-9、较大原发灶、广泛淋巴结转移、严重消瘦或极度疼痛时，可考虑术前新辅助治疗；CA19-9≥1000 U/ml为重要高危线索。",
    },
    "CPC2020_BILIARY_DRAINAGE": {
        "source": "中国胰腺癌综合诊治指南（2020版）",
        "section": "根治术前减黄治疗",
        "rule": "术前减黄需MDT判断；高龄或体能差且黄疸时间长、明显肝功能异常、发热或胆管炎，以及拟行新辅助治疗者，推荐先行胆道引流。",
    },
    "PCN2022_MRI_MRCP": {
        "source": "中国胰腺囊性肿瘤诊断指南（2022年）",
        "section": "推荐意见2、14、15",
        "rule": "MRI是PCN首选诊断方法；IPMN或MCN随访首选MRI联合MRCP，MRI禁忌时可用EUS或CT。",
    },
    "PCN2022_EUS_HIGH_RISK": {
        "source": "中国胰腺囊性肿瘤诊断指南（2022年）",
        "section": "推荐意见3至5",
        "rule": "病灶≥3 cm、壁结节>5 mm、囊壁增厚或强化、主胰管>5 mm、胰管截断伴远端萎缩、淋巴结肿大、CA19-9升高或增长≥5 mm/2年时，建议EUS进一步评估；性质不明且结果会改变治疗时可行EUS-FNA/FNB。",
    },
    "PCN2022_SURVEILLANCE": {
        "source": "中国胰腺囊性肿瘤诊断指南（2022年）",
        "section": "推荐意见12至17",
        "rule": "无症状IPMN或MCN且具备手术条件者应随访；无高危征象者按病灶大小制定随访，高危但未手术者建议每6个月MRI；SCN按症状随访。",
    },
    "PCN2022_SURGICAL_RISK": {
        "source": "中国胰腺囊性肿瘤诊断指南（2022年）",
        "section": "随访策略与恶变高危因素",
        "rule": "黄疸、细胞学阳性、主胰管≥10 mm等属于重要高危或绝对手术指征线索，需MDT外科评估。",
    },
    "CACA2025_PNET_STAGING": {
        "source": "中国抗癌协会神经内分泌肿瘤诊治指南（2025年版）正式全文",
        "section": "4.1.4及4.2.1 pNET/pNEN",
        "rule": "pNET需结合EUS、增强CT/MRI评估定位、血管、淋巴结和转移，并综合功能状态、病理分级、分期和遗传背景制定方案。",
    },
    "CACA2025_PNET_SMALL": {
        "source": "中国抗癌协会神经内分泌肿瘤诊治指南（2025年版）正式全文",
        "section": "4.2.1.1 局限期非功能性pNET",
        "rule": "无症状、无淋巴结转移或局部侵犯的<2 cm G1非功能性pNET可影像随访；G2相对积极手术，G3或持续生长者应手术。",
    },
    "CACA2025_PNET_LARGE": {
        "source": "中国抗癌协会神经内分泌肿瘤诊治指南（2025年版）正式全文",
        "section": "4.2.1.1至4.2.1.2",
        "rule": "≥2 cm非功能性pNET优选规则性胰腺切除并淋巴结清扫；局部进展或转移性pNET需扩充分期、病理分级并由MDT个体化决策。",
    },
    "CACA2025_PDAC_IMAGING": {
        "source": "中国肿瘤整合诊治指南（CACA）胰腺癌 V2.0_2025",
        "section": "第三章第三节影像学检查，第1297页",
        "rule": "薄层增强CT是胰腺癌最常用的影像检查；增强MRI在鉴别困难、肝脏小转移及CT等密度病灶中具有补充价值，MRCP用于胆胰管评价。",
    },
    "CACA2025_PDAC_RESECTABILITY": {
        "source": "中国肿瘤整合诊治指南（CACA）胰腺癌 V2.0_2025",
        "section": "第四章第一节可切除性的解剖学评估，第1302页",
        "rule": "治疗前应由MDT依据肿瘤与重要血管关系及远处转移，分为可切除、交界可切除、局部进展或合并远处转移；疑有远处转移而高质量CT/MRI不能确诊时，可行PET，必要时腹腔镜探查。",
    },
    "CACA2025_PDAC_PATHOLOGY": {
        "source": "中国肿瘤整合诊治指南（CACA）胰腺癌 V2.0_2025",
        "section": "第四章第七至八节，第1317至1319页",
        "rule": "转移性或局部进展期胰腺癌在系统或转化治疗前需病理确诊，优先选择可安全取材且有代表性的病灶；局部进展期推荐EUS穿刺。",
    },
    "CACA2025_PDAC_RESECTABLE": {
        "source": "中国肿瘤整合诊治指南（CACA）胰腺癌 V2.0_2025",
        "section": "第四章第九节，第1321页",
        "rule": "可切除胰腺癌应先评估高危因素、体能、营养和黄疸；无高危因素且无手术禁忌证者推荐根治性切除，不主张所有可切除病例常规新辅助治疗。",
    },
    "CACA2025_PDAC_HIGH_RISK": {
        "source": "中国肿瘤整合诊治指南（CACA）胰腺癌 V2.0_2025",
        "section": "第四章第九节新辅助治疗，第1321页",
        "rule": "可切除但伴CA19-9非常高、肿瘤较大、区域淋巴结较大、体重明显减轻或极度疼痛等高危因素者推荐新辅助治疗；CEA阳性、CA125阳性且CA19-9≥1000 U/ml为重要高危组合。",
    },
    "CACA2025_PDAC_BORDERLINE": {
        "source": "中国肿瘤整合诊治指南（CACA）胰腺癌 V2.0_2025",
        "section": "第四章第十节，第1323页",
        "rule": "体能状态较好的交界可切除胰腺癌推荐先行新辅助治疗；治疗后无进展者即使影像未降期也应由MDT评估手术探查，首选腹腔镜探查并先排除远处转移。",
    },
    "CACA2025_PDAC_LOCALLY_ADVANCED": {
        "source": "中国肿瘤整合诊治指南（CACA）胰腺癌 V2.0_2025",
        "section": "第四章第八节，第1319页",
        "rule": "局部进展期初始不推荐手术，应病理确诊后接受非手术或转化治疗；治疗后CA19-9明显下降、临床改善且影像PR或SD者可由MDT考虑手术探查。",
    },
    "CACA2025_PDAC_METASTATIC": {
        "source": "中国肿瘤整合诊治指南（CACA）胰腺癌 V2.0_2025",
        "section": "第四章第七节，第1317至1319页",
        "rule": "合并远处转移属于全身晚期肿瘤，以系统治疗为主，不推荐减瘤手术；单器官且转移灶不超过3个者仅在系统治疗明显退缩并预计R0切除时考虑手术临床研究。",
    },
    "CACA2025_PDAC_RESTAGING": {
        "source": "中国肿瘤整合诊治指南（CACA）胰腺癌 V2.0_2025",
        "section": "第四章第一节新辅助/转化治疗后可切除性评估，第1303页",
        "rule": "新辅助或转化治疗后应结合CT/MRI、CA19-9变化、临床状态及MDT重新评估；影像无明显进展且生物学和临床状态改善者不应仅因未降期而否定探查。",
    },
    "CACA2025_PDAC_BILIARY_DRAINAGE": {
        "source": "中国肿瘤整合诊治指南（CACA）胰腺癌 V2.0_2025",
        "section": "第四章第二节术前减黄，第1304至1305页",
        "rule": "术前减黄不常规实施；高龄或体能差且梗阻时间较长、明显肝功能异常、发热或胆管炎，以及拟行新辅助治疗者推荐先行减黄。",
    },
    "CP2018_DIAGNOSIS": {
        "source": "慢性胰腺炎诊治指南（2018，广州）",
        "section": "诊断标准及分型分期",
        "rule": "CT、MRI/MRCP及EUS用于评价钙化、结石、胰管改变和实质病变；肿块型慢性胰腺炎与胰腺癌难鉴别时可行EUS-FNA，不能仅凭队列标签确定性质。",
    },
    "CP2018_INTERVENTION": {
        "source": "慢性胰腺炎诊治指南（2018，广州）",
        "section": "治疗及预后",
        "rule": "胰管结石或狭窄导致的梗阻性疼痛首选内镜减压或取石，6至8周疗效不佳再考虑手术；有症状、并发症或持续增大的假性囊肿应治疗，通常优先内镜。",
    },
    "CP2018_SURGERY": {
        "source": "慢性胰腺炎诊治指南（2018，广州）",
        "section": "外科手术治疗",
        "rule": "顽固疼痛、内镜治疗失败、无法内镜处理的梗阻或假性囊肿等并发症，以及怀疑恶变，是外科评估指征；术式须按病变部位、胰管和并发症个体化选择。",
    },
    "SPN2015_TREATMENT": {
        "source": "胰腺囊性疾病诊治指南（2015）",
        "section": "4.1.4 实性假乳头状肿瘤的治疗",
        "rule": "所有确诊SPN均推荐手术；小、包膜完整且边界清楚者可局部剜除，明显侵犯者扩大切除；不常规清扫胰周淋巴结，体尾部病例可根据条件保脾。",
    },
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def compact_quote(text: str, start: int, end: int, width: int = 110) -> str:
    left = max(0, start - 35)
    right = min(len(text), end + width)
    return clean(text[left:right])[:180]


def build_sources(case: dict[str, Any]) -> list[dict[str, str]]:
    a = case["agent_input"]
    out: list[dict[str, str]] = []
    for item in a.get("imaging_before_decision", []):
        out.append(
            {
                "type": "imaging",
                "time": clean(item.get("exam_time")),
                "method": clean(item.get("exam_method")),
                "id": clean(item.get("source_record_key")),
                "text": clean(item.get("report")),
            }
        )
    for item in a.get("clinical_state_documents", []):
        out.append(
            {
                "type": "document",
                "time": clean(item.get("time")),
                "method": clean(item.get("title")),
                "id": clean(item.get("source_record_id")),
                "text": clean(item.get("text")),
            }
        )
    current = a.get("sanitized_state_from_decision_document") or {}
    if current.get("text"):
        out.append(
            {
                "type": "sanitized_decision_document",
                "time": clean(current.get("time")),
                "method": "去计划句后的决策时点状态",
                "id": clean(current.get("source_record_id")),
                "text": clean(current.get("text")),
            }
        )
    for item in a.get("pathology_before_decision", []):
        out.append(
            {
                "type": "pathology",
                "time": clean(item.get("report_date") or item.get("specimen_date")),
                "method": clean(item.get("specimen_type") or "病理"),
                "id": clean(item.get("source_record_key")),
                "text": clean(item.get("pathology_diagnosis") or item.get("clinical_diagnosis")),
            }
        )
    return out


def find_evidence(sources: list[dict[str, str]], pattern: str, limit: int = 3) -> list[dict[str, str]]:
    rx = re.compile(pattern, re.I)
    found = []
    for source in sources:
        for match in rx.finditer(source["text"]):
            found.append(
                {
                    "source_type": source["type"],
                    "source_id": source["id"],
                    "time": source["time"],
                    "method_or_title": source["method"],
                    "quote": compact_quote(source["text"], match.start(), match.end()),
                }
            )
            break
        if len(found) >= limit:
            break
    return found


def positive_biliary_obstruction_evidence(sources: list[dict[str, str]], limit: int = 3) -> list[dict[str, str]]:
    """Extract patient-specific positive jaundice/cholangitis evidence, excluding negation and templates."""
    target = re.compile(r"梗阻性黄疸|胆管炎|(?:皮肤(?:黏膜)?(?:及|和)?|巩膜)[^，,。；;]{0,8}黄染", re.I)
    negated = re.compile(
        r"(?:无|未见|未发现|无明确)[^，,。；;]{0,10}(?:黄疸|黄染|胆管炎)|"
        r"(?:不伴|否认)[^，,。；;]{0,45}(?:黄疸|黄染|胆管炎)",
        re.I,
    )
    template = re.compile(r"一般(?:有|多为)|可出现|可伴|辅助检查|鉴别诊断|应与.{0,12}鉴别", re.I)
    found = []
    for source in sources:
        for clause in re.split(r"[。；;\n]", source["text"]):
            match = target.search(clause)
            if not match or negated.search(clause) or template.search(clause):
                continue
            found.append(
                {
                    "source_type": source["type"],
                    "source_id": source["id"],
                    "time": source["time"],
                    "method_or_title": source["method"],
                    "quote": clean(clause)[:180],
                }
            )
            break
        if len(found) >= limit:
            break
    return found


def metastasis_evidence(sources: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    organ = r"(?:远处|肝(?:脏)?|肺|胸膜|腹膜|网膜|骨|肾上腺|非区域淋巴结)"
    mention = re.compile(organ + r"[^。；;\n]{0,35}(?:转移|播散|种植)", re.I)
    suspicious = re.compile(r"待排|不除外|不能除外|可疑|可能|倾向|考虑|建议.*(?:复查|MRI|MR|PET|活检)", re.I)
    negative = re.compile(r"未见|无明确|无[^。；]{0,6}(?:转移|播散|种植)|排除", re.I)
    certain, suspect, neg = [], [], []
    for source in sources:
        text = source["text"]
        for m in mention.finditer(text):
            context = compact_quote(text, m.start(), m.end(), 45)
            polarity_context = text[m.start(): min(len(text), m.end() + 24)]
            ev = {
                "source_type": source["type"], "source_id": source["id"], "time": source["time"],
                "method_or_title": source["method"], "quote": context,
            }
            if negative.search(polarity_context):
                neg.append(ev)
            elif suspicious.search(polarity_context):
                suspect.append(ev)
            else:
                certain.append(ev)
    if certain:
        return "明确", certain[:3]
    if suspect:
        return "可疑", suspect[:3]
    if neg:
        return "报告明确未见", neg[:3]
    return "未说明", []


def ca199_status(case: dict[str, Any]) -> tuple[str, float | None, list[dict[str, str]]]:
    values = []
    evidence = []
    for item in case["agent_input"].get("important_laboratory_before_decision", []):
        name = clean(item.get("item_name"))
        if not re.search(r"CA\s*19[-－]?9|CA199|糖类抗原\s*19[-－]?9", name, re.I):
            continue
        raw = clean(item.get("result"))
        match = re.search(r"-?\d+(?:\.\d+)?", raw.replace(",", ""))
        value = float(match.group()) if match else None
        if value is not None:
            values.append(value)
        evidence.append(
            {
                "source_type": "laboratory", "source_id": clean(item.get("source_record_key")),
                "time": clean(item.get("available_time")), "method_or_title": name,
                "quote": f"{name}: {raw} {clean(item.get('unit'))}",
            }
        )
    if not values:
        return "未见", None, evidence[:3]
    latest = values[-1]
    return ("≥1000 U/ml" if latest >= 1000 else "已见但<1000 U/ml"), latest, evidence[-3:]


def max_pancreas_lesion_mm(imaging: list[dict[str, str]]) -> tuple[float | None, list[dict[str, str]]]:
    values: list[tuple[float, dict[str, str], str]] = []
    for source in imaging:
        for sentence in re.split(r"[。；;\n]", source["text"]):
            if not re.search(r"胰|囊|IPMN|MCN|SCN|肿瘤|占位", sentence, re.I):
                continue
            for match in re.finditer(r"(\d+(?:\.\d+)?)\s*[×xX＊*]\s*(\d+(?:\.\d+)?)\s*(mm|cm)", sentence, re.I):
                factor = 10 if match.group(3).lower() == "cm" else 1
                value = max(float(match.group(1)), float(match.group(2))) * factor
                values.append((value, source, clean(sentence)))
            for match in re.finditer(r"(?:直径|最大径|长径)\s*(?:约|为|：|:)?\s*(\d+(?:\.\d+)?)\s*(mm|cm)", sentence, re.I):
                factor = 10 if match.group(2).lower() == "cm" else 1
                values.append((float(match.group(1)) * factor, source, clean(sentence)))
    if not values:
        return None, []
    value, source, sentence = max(values, key=lambda x: x[0])
    return value, [{"source_type": source["type"], "source_id": source["id"], "time": source["time"], "method_or_title": source["method"], "quote": sentence[:180]}]


def classify_case(case: dict[str, Any], index: dict[str, str]) -> dict[str, Any]:
    sources = build_sources(case)
    disease = index["病种"]
    text = " ".join(s["text"] for s in sources)
    imaging = [s for s in sources if s["type"] == "imaging"]
    pathology_sources = [s for s in sources if s["type"] == "pathology"]
    staging_sources = imaging + pathology_sources
    has_enhanced_ct = any(re.search(r"CT", s["method"], re.I) and re.search(r"增强|多期|动脉期|静脉期", s["method"] + " " + s["text"], re.I) for s in imaging)
    has_mri = any(re.search(r"MR|MRI|磁共振", s["method"] + " " + s["text"], re.I) for s in imaging)
    only_mrcp = bool(imaging) and all(re.search(r"MRCP|水成像", s["method"], re.I) for s in imaging)
    path_ev = find_evidence(pathology_sources, r"腺癌|导管腺癌|癌细胞|恶性肿瘤", 3)
    pathology_confirmed = any(s["type"] == "pathology" and re.search(r"腺癌|癌细胞|恶性", s["text"]) for s in sources)
    # 分期与可切除性只接受影像/病理原始来源，避免把入院记录中的鉴别诊断、
    # 通用知情同意或手术风险说明误当作患者事实。
    met_status, met_ev = metastasis_evidence(staging_sources)
    br_ev = find_evidence(imaging, r"交界可切除|临界可切除|borderline", 3)
    la_ev = find_evidence(imaging, r"局部进展|不可切除|无法切除|不能切除", 3)
    resectable_ev = find_evidence(imaging, r"(?<!交界)(?<!临界)可切除|评估为可切除", 3)
    vessel_ev = find_evidence(imaging, r"(?:肠系膜上动脉|肠系膜上静脉|门静脉|腹腔干|肝总动脉)[^。；]{0,30}(?:接触|包绕|侵犯|受侵|狭窄|闭塞|血栓|变形|180)", 3)
    advanced_vessel_ev = find_evidence(
        imaging,
        r"(?:肠系膜上动脉|腹腔干)[^，,、；。]{0,18}(?:＞|>|超过)\s*180|"
        r"(?:肠系膜上动脉|腹腔干)[^，,、；。]{0,18}(?:包绕|闭塞|无法重建)",
        3,
    )
    borderline_vessel_ev = find_evidence(
        imaging,
        r"肠系膜上动脉[^，,、；。]{0,18}(?:≤|＜|<)\s*180|"
        r"肝总动脉[^，,、；。]{0,18}(?:接触|侵犯|包绕)|"
        r"(?:肠系膜上静脉|门静脉)[^，,、；。]{0,18}(?:＞|>|超过)\s*180|"
        r"(?:肠系膜上静脉|门静脉)[^，,、；。]{0,18}(?:轮廓不规则|血栓|闭塞)",
        3,
    )
    head_ev = find_evidence(staging_sources, r"胰头|钩突", 2)
    head_imaging_ev = find_evidence(imaging, r"胰头|钩突", 2)
    tail_ev = find_evidence(staging_sources, r"胰体尾|胰尾|胰体", 2)
    jaundice_ev = positive_biliary_obstruction_evidence(sources, 3)
    ca_status, ca_value, ca_ev = ca199_status(case)
    lesion_mm, lesion_size_ev = max_pancreas_lesion_mm(imaging)
    pcn_high_ev = find_evidence(
        imaging,
        r"壁结节|囊壁[^。；]{0,12}(?:增厚|强化)|主胰管[^。；]{0,15}(?:扩张|增宽)|"
        r"胰管截断[^。；]{0,20}萎缩|淋巴结[^。；]{0,12}(?:肿大|转移)|实性成分|增长",
        4,
    )
    pcn_absolute_ev = find_evidence(
        imaging + pathology_sources,
        r"主胰管[^。；]{0,12}(?:≥|＞|>)\s*10\s*mm|细胞学[^。；]{0,12}(?:阳性|癌)|"
        r"壁结节[^。；]{0,12}(?:≥|＞|>)\s*5\s*mm",
        3,
    )
    grade_ev = find_evidence(pathology_sources, r"\bG[123]\b|Ki\s*[-⁃]?\s*67[^。；]{0,16}%", 3)
    function_ev = find_evidence(sources, r"胰岛素瘤|胃泌素瘤|胰高血糖素瘤|VIP瘤|功能性.{0,8}(?:神经内分泌|pNET)|反复低血糖", 3)
    cp_tumor_suspect_ev = find_evidence(
        imaging,
        r"胰腺癌(?:可能|待排|考虑)?|癌变可能|神经内分泌肿瘤可能|恶性肿瘤待排|胰腺[^。；]{0,12}占位",
        3,
    )
    cp_obstruction_ev = find_evidence(
        imaging,
        r"胰管(?:结石|狭窄|梗阻)|主胰管[^。；]{0,16}(?:扩张|结石|狭窄|梗阻)",
        3,
    )
    pseudocyst_ev = find_evidence(imaging, r"假性囊肿", 3)
    spn_ev = find_evidence(imaging + pathology_sources, r"实性[-—]?假乳头状|\bSPN\b|\bSPT\b", 3)
    # CACA 2025 的高危条件是“区域淋巴结较大”，不能把结构化报告中的任意
    # “可疑淋巴结”直接等同为高危。只有明确肿大/较大或给出尺寸时才触发；
    # 其余仅保留为待 MDT 复核的候选线索。
    pdac_large_node_ev = find_evidence(
        imaging,
        r"区域淋巴结[^。；;\n]{0,30}(?:肿大|较大|短径\s*(?:约|为|：|:)?\s*\d+(?:\.\d+)?\s*(?:mm|cm)|"
        r"\d+(?:\.\d+)?\s*[×xX＊*]\s*\d+(?:\.\d+)?\s*(?:mm|cm))",
        2,
    )
    pdac_node_candidate_ev = find_evidence(
        imaging, r"可疑淋巴结\s*[:：]\s*(?!未见|无)[^。；;\n]{1,35}", 2
    )
    pdac_high_risk_ev = (
        pdac_large_node_ev
        + find_evidence(
            sources,
            r"体重(?:明显)?(?:减轻|下降|降低)\s*\d+(?:\.\d+)?\s*(?:kg|KG|公斤)|剧烈(?:的)?疼痛|极度疼痛|疼痛难忍",
            2,
        )
    )[:3]

    facts = {
        "cohort_disease_stratum_not_used_as_diagnosis": disease,
        "predecision_pathology_confirmed_malignancy": "Y" if pathology_confirmed else "U",
        "distant_metastasis_statement": met_status,
        "explicit_resectability_statement": "交界可切除" if br_ev else ("局部进展/不可切除" if la_ev else ("可切除" if resectable_ev else "未明确")),
        "vessel_relation_mentioned": "Y" if vessel_ev else "U",
        "vascular_resectability_candidate": "局部进展倾向_需MDT确认" if advanced_vessel_ev else ("交界可切除倾向_需MDT确认" if borderline_vessel_ev else ("有血管关系但不能自动分级" if vessel_ev else "未形成候选")),
        "lesion_location": "胰头/钩突" if head_ev and not tail_ev else ("胰体尾" if tail_ev and not head_ev else "未能唯一确定"),
        "enhanced_ct_before_decision": "Y" if has_enhanced_ct else "U",
        "mri_before_decision": "Y" if has_mri else "U",
        "only_mrcp_as_indexed_imaging": "Y" if only_mrcp else "N",
        "ca199_status": ca_status,
        "ca199_value": ca_value,
        "obstructive_jaundice_or_cholangitis_signal": "Y" if jaundice_ev else "U",
        "largest_pancreas_lesion_mm": lesion_mm,
        "pathologic_grade_statement": "Y" if grade_ev else "U",
        "functional_pnet_statement": "Y" if function_ev else "U",
    }
    evidence = {
        "pathology_or_malignancy": path_ev,
        "distant_metastasis": met_ev,
        "resectability": (br_ev or la_ev or resectable_ev),
        "vessel_relation": vessel_ev,
        "advanced_vessel_candidate": advanced_vessel_ev,
        "borderline_vessel_candidate": borderline_vessel_ev,
        "location": (head_ev or tail_ev),
        "ca199": ca_ev,
        "biliary_obstruction": jaundice_ev,
        "lesion_size": lesion_size_ev,
        "pcn_high_risk_features": pcn_high_ev,
        "pcn_absolute_surgical_risk_features": pcn_absolute_ev,
        "pnet_grade": grade_ev,
        "pnet_function": function_ev,
        "chronic_pancreatitis_tumor_suspicion": cp_tumor_suspect_ev,
        "chronic_pancreatitis_obstruction": cp_obstruction_ev,
        "pseudocyst": pseudocyst_ev,
        "spn_support": spn_ev,
        "pdac_high_risk_features": pdac_high_risk_ev,
        "pdac_suspicious_node_candidate": pdac_node_candidate_ev,
    }

    missing = []
    prerequisites = []
    alternatives = []
    avoid = []
    basis = ["EVIDENCE_FACT_BOUNDARY"]
    status = "条件性答案"
    confidence = "中"
    ready = False
    process_usable = False

    if disease in {"IPMN", "MCN", "SCN"}:
        basis += ["PCN2022_MRI_MRCP", "PCN2022_SURVEILLANCE"]
        high_risk = bool(pcn_high_ev) or (lesion_mm is not None and lesion_mm >= 30) or (ca_value is not None and ca_value > 37)
        # PCN高危征象中的梗阻性黄疸要求与胰头病灶相符，避免把其他病因或模板描述误归因于囊性病变。
        pcn_attributable_jaundice = bool(jaundice_ev) and bool(head_imaging_ev)
        absolute_risk = bool(pcn_absolute_ev) or pcn_attributable_jaundice
        if disease == "SCN":
            if high_risk or absolute_risk:
                primary_category = "囊性肿瘤再评估"
                primary_action = "先复核SCN诊断及症状归因，采用MRI/MRCP并在高危或诊断不确定时行EUS，由MDT决定观察或手术"
                alternatives = ["若确认无症状、典型SCN且无其他高危线索，可按症状随访"]
                missing += ["需要确认当前症状是否确由SCN引起", "需要确认SCN诊断是否足够可靠"]
                basis += ["PCN2022_EUS_HIGH_RISK"]
            else:
                primary_category = "观察随访"
                primary_action = "诊断明确且无症状的SCN按症状随访，避免仅因病灶存在而直接手术"
                alternatives = ["诊断不确定或症状出现时补充MRI/MRCP和EUS评估"]
                avoid = ["将SCN低恶变风险病例常规按恶性肿瘤处理"]
            process_usable = True
        elif absolute_risk:
            primary_category = "MDT外科评估"
            primary_action = "存在重要恶变或手术指征线索，先行EUS精细评估并进入MDT外科评估，不直接按普通随访处理"
            alternatives = ["如性质仍不明确且结果将改变治疗，可考虑EUS-FNA/FNB"]
            avoid = ["忽略黄疸、阳性细胞学、主胰管显著扩张或强化壁结节等高危线索"]
            basis += ["PCN2022_EUS_HIGH_RISK", "PCN2022_SURGICAL_RISK"]
            process_usable = True
        elif high_risk:
            primary_category = "EUS进一步评估"
            primary_action = "针对高危征象补充EUS评估；依据EUS、MRI/MRCP和MDT结果决定手术或密切随访"
            alternatives = ["如EUS结果不会改变治疗或已有明确手术适应证，不常规追加穿刺"]
            avoid = ["仅凭单一囊肿大小或单一影像征象自动判定恶性"]
            basis += ["PCN2022_EUS_HIGH_RISK"]
            process_usable = True
        else:
            primary_category = "MRI/MRCP监测随访"
            primary_action = f"无明确高危征象的{disease}采用MRI联合MRCP监测，具体间隔按病灶大小和患者手术耐受性制定"
            alternatives = ["MRI禁忌时可采用EUS或CT"]
            avoid = ["无高危征象时直接把病灶视为恶性并手术"]
            process_usable = True
        status = "可形成流程规范答案"
        confidence = "中"
        if lesion_mm is None:
            missing += ["缺少可可靠引用的病灶最大径，无法确定随访强度"]
        if disease in {"IPMN", "MCN"} and not pcn_high_ev:
            missing += ["需确认壁结节、主胰管径、囊壁强化和生长速度是否完整记录"]
    elif disease == "PanNET/PNET":
        basis += ["CACA2025_PNET_STAGING"]
        if met_status in {"明确", "可疑"}:
            primary_category = "扩充分期与病理分级"
            primary_action = "补充增强CT/MRI及必要的SSTR PET/CT等分期，并取得病理分级和Ki-67；由NET MDT决定系统治疗、原发灶或转移灶手术策略"
            alternatives = ["肝转移可结合肝脏特异性增强MRI；必要时多点活检评估异质性"]
            avoid = ["未明确分级、功能状态和转移范围即按普通胰腺癌术式处理"]
            basis += ["CACA2025_PNET_LARGE"]
        elif lesion_mm is not None and lesion_mm >= 20:
            primary_category = "pNET外科MDT评估"
            primary_action = "完成病理分级、功能状态和全身分期后，局限期≥2 cm非功能性pNET原则上进入规则性胰腺切除及淋巴结评估路径"
            alternatives = ["具体术式依据部位、深度、胰管及血管关系决定"]
            avoid = ["缺少分级和分期时仅依据病灶大小直接确定术式"]
            basis += ["CACA2025_PNET_LARGE"]
        elif lesion_mm is not None and lesion_mm < 20:
            primary_category = "pNET分级后观察或手术"
            primary_action = "先明确功能状态、G分级/Ki-67、淋巴结和局部侵犯；若为无症状、无转移的<2 cm G1非功能性pNET可严密影像随访，G2/G3或生长者倾向手术"
            alternatives = ["EUS用于定位及判断胰管、血管和周围淋巴结"]
            avoid = ["未分级即把所有<2 cm pNET统一观察或统一手术"]
            basis += ["CACA2025_PNET_SMALL"]
        else:
            primary_category = "pNET诊断分层"
            primary_action = "先通过EUS、增强CT/MRI和病理明确病灶大小、功能状态、G分级/Ki-67及分期，再决定观察、局部治疗或手术"
            alternatives = ["必要时补充分子影像以完善分期"]
            avoid = ["缺少病灶大小和病理分级时直接确定术式"]
        status = "可形成流程规范答案"
        confidence = "中"
        process_usable = True
        if lesion_mm is None:
            missing += ["缺少可可靠引用的肿瘤最大径"]
        if not grade_ev:
            missing += ["缺少病理G分级和Ki-67"]
        if not function_ev:
            missing += ["缺少功能性/非功能性判定及相应激素评估"]
        if met_status == "未说明":
            missing += ["当前输入未见可引用的淋巴结和远处转移完整分期结论"]
    elif disease == "慢性胰腺炎":
        basis += ["CP2018_DIAGNOSIS"]
        if cp_tumor_suspect_ev:
            primary_category = "肿块性质鉴别"
            primary_action = "当前影像存在肿瘤或癌变疑点，应先以胰腺增强CT/MRI联合EUS-FNA/FNB鉴别肿块型慢性胰腺炎与胰腺肿瘤，再按病理和分期决定治疗"
            alternatives = ["若病灶具有明确可切除恶性肿瘤特征，由胰腺MDT决定直接手术或先取材；不得使用队列病种标签替代诊断"]
            avoid = ["在未解决肿瘤疑点时仅按慢性胰腺炎行内镜减压或长期观察", "仅凭队列最终分层倒推决策时点诊断"]
            missing += ["缺少能够解决肿块型慢性胰腺炎与胰腺肿瘤鉴别的病理或MDT结论"]
        elif pseudocyst_ev:
            primary_category = "假性囊肿分层处理"
            primary_action = "先判断假性囊肿是否有症状、感染、出血、破裂或持续增大；存在上述指征时通常优先内镜引流，内镜不适用或失败时再进入外科评估"
            alternatives = ["无症状且无并发症者可结合胰管交通情况和连续影像随访"]
            avoid = ["仅凭假性囊肿存在或大小直接确定胰腺切除术"]
            missing += ["需确认假性囊肿的症状归因、并发症、与主胰管交通及动态变化"]
            basis += ["CP2018_INTERVENTION", "CP2018_SURGERY"]
        elif cp_obstruction_ev:
            primary_category = "胰管梗阻分层处理"
            primary_action = "对胰管结石、狭窄或梗阻导致的症状，先进行病因、胰管形态和症状归因评估；符合梗阻性疼痛者优先内镜减压或取石，疗效不佳再评估手术"
            alternatives = ["大于5 mm的主胰管阳性结石可评估ESWL后ERCP取石"]
            avoid = ["未经过症状归因和内镜可行性评估即直接胰腺切除"]
            missing += ["需确认当前症状是否由胰管梗阻引起及既往内镜治疗效果"]
            basis += ["CP2018_INTERVENTION", "CP2018_SURGERY"]
        else:
            primary_category = "慢性胰腺炎诊疗分层"
            primary_action = "先确认慢性胰腺炎诊断、病因、疼痛类型、胰腺内外分泌功能和并发症，再按症状给予戒酒戒烟、营养和胰酶等治疗"
            alternatives = ["出现胰管梗阻、假性囊肿或胆道梗阻时进入相应内镜或外科路径"]
            avoid = ["缺少症状和功能分层时直接确定侵入性治疗"]
            missing += ["缺少病因、疼痛、胰腺内外分泌功能及并发症的完整分层"]
        status = "可形成流程规范答案"
        confidence = "中"
        process_usable = True
    elif disease == "SPN":
        basis += ["SPN2015_TREATMENT"]
        if spn_ev:
            primary_category = "SPN外科MDT评估"
            primary_action = "影像已明确提出SPN可能，应先完成局部侵犯和远处病灶评估并由MDT确认诊断；确诊后原则上手术，具体采用剜除或按部位切除取决于大小、包膜、边界及周围侵犯"
            alternatives = ["体尾部病灶在肿瘤学安全可保证时可评估保脾", "无影像淋巴结侵犯时不常规扩大淋巴结清扫"]
            avoid = ["仅依据队列标签预先固定Whipple或胰体尾切除术", "把SPN直接套入PDAC新辅助治疗规则"]
            missing += ["缺少SPN病理确认或MDT影像诊断结论", "需明确包膜、局部侵犯、远处病灶及与主胰管和血管的关系"]
        else:
            primary_category = "SPN与其他胰腺肿瘤鉴别"
            primary_action = "当前决策时点材料未明确提出SPN，应先由胰腺影像、病理和外科MDT鉴别PDAC、pNET及SPN；诊断明确后再进入相应手术或系统治疗路径"
            alternatives = ["若取材结果将改变治疗策略，可考虑EUS-FNB"]
            avoid = ["因最终队列归为SPN而在决策时点直接给出SPN手术答案"]
            missing += ["决策时点缺少支持SPN诊断的明确影像或病理证据"]
        status = "可形成流程规范答案"
        confidence = "中"
        process_usable = True
    elif disease != "PDAC":
        primary_category = "专病指南评估"
        primary_action = f"补充{disease}专病指南并由相应MDT确定下一步；当前仅保留影像与病理事实，不生成治疗金标准"
        alternatives = ["若诊断性质仍不明确，可依据病灶性质补充高质量影像或病理确认"]
        avoid = ["不得使用PDAC手术规则替代该病种专病指征", "不得从影像单一征象自动推导恶性、分期或可切除性"]
        missing = [f"缺少{disease}专病诊疗指南或共识", "缺少适用于该病种的手术/观察阈值和随访规则"]
        basis += ["CPC2020_IMG_STAGING"]
        status = "来源范围不足"
        confidence = "低"
    else:
        basis += ["CACA2025_PDAC_RESECTABILITY"]
        if not has_enhanced_ct and not has_mri:
            primary_category = "补充分期"
            primary_action = "先补充高质量胰腺增强CT和/或增强MRI，再由MDT评估可切除性与远处转移"
            alternatives = ["疑似胰腺恶性病变可结合EUS；具体检查顺序由MDT决定"]
            avoid = ["在分期影像不足时直接确定根治术式", "自动推导TNM或可切除性"]
            missing += ["缺少可用于局部血管关系和远处转移评估的高质量增强影像"]
            basis += ["CACA2025_PDAC_IMAGING"]
        elif met_status == "明确":
            basis += ["CACA2025_PDAC_METASTATIC", "CACA2025_PDAC_PATHOLOGY"]
            if pathology_confirmed:
                primary_category = "系统治疗/姑息治疗"
                primary_action = "按转移性胰腺癌进入MDT系统治疗与症状处理，不直接行根治性胰腺切除"
                ready = True
                confidence = "高"
            else:
                primary_category = "病理确认"
                primary_action = "优先取得病理证据，可选择可疑转移灶取材；确认后进入转移性胰腺癌系统治疗"
                missing += ["缺少治疗前病理学确认"]
            alternatives = ["存在梗阻时先行内引流或其他姑息介入", "单器官且转移灶不超过3个的特殊寡转移，仅在系统治疗明显退缩并预计R0切除时进入临床研究性手术评估"]
            avoid = ["未控制或未确认远处转移时直接实施根治性切除"]
        elif met_status == "可疑":
            primary_category = "补充分期/转移灶确认"
            primary_action = "先确认可疑远处病灶：PET-CT或PET-MRI、可疑病灶活检，必要时腹腔镜探查；确认M0后再决定根治路径"
            alternatives = ["针对肝内可疑灶补充肝脏MRI", "由MDT选择最安全且诊断收益最高的确认方式"]
            avoid = ["未完成可疑远处病灶评估即直接进入根治性切除"]
            missing += ["远处病灶性质尚未明确"]
            basis += ["CACA2025_PDAC_RESECTABILITY", "CACA2025_PDAC_IMAGING"]
        elif la_ev or advanced_vessel_ev:
            basis += ["CACA2025_PDAC_LOCALLY_ADVANCED", "CACA2025_PDAC_PATHOLOGY", "CACA2025_PDAC_RESTAGING"]
            primary_category = "病理确认后转化治疗" if not pathology_confirmed else "转化治疗"
            primary_action = "先由MDT确认局部进展期；取得病理证据后进行转化治疗，治疗后无进展且体能良好时再行以腹腔镜为优先的手术探查" if not pathology_confirmed else "先由MDT确认局部进展期后进行转化治疗；治疗后无进展且体能良好时再行以腹腔镜为优先的手术探查"
            alternatives = ["存在胆道或消化道梗阻时先处理梗阻"]
            avoid = ["局部进展期直接进行根治性切除"]
            if not pathology_confirmed:
                missing += ["缺少转化治疗前病理学确认"]
            ready = pathology_confirmed
            if advanced_vessel_ev and not la_ev:
                missing += ["影像血管关系提示局部进展倾向，但自动分级仅为候选，需MDT确认"]
        elif br_ev or borderline_vessel_ev:
            basis += ["CACA2025_PDAC_BORDERLINE", "CACA2025_PDAC_PATHOLOGY", "CACA2025_PDAC_RESTAGING"]
            primary_category = "新辅助治疗前确认"
            primary_action = "由MDT完成交界可切除性判定；若确认为交界可切除，取得病理证据后首选新辅助治疗"
            alternatives = ["如影像仅描述血管接触而未明确可切除性，应由影像科和胰腺外科联合复核，不能由规则自动分级"]
            avoid = ["仅依据单一血管接触描述直接判定可切除或直接手术"]
            if not pathology_confirmed:
                missing += ["若进入新辅助治疗，缺少治疗前病理学确认"]
            if not br_ev:
                missing += ["影像血管关系提示交界可切除倾向，但自动分级仅为候选，需MDT确认"]
            confidence = "中"
        else:
            basis += ["CACA2025_PDAC_RESECTABLE", "CACA2025_PDAC_IMAGING"]
            if (ca_value is not None and ca_value >= 1000) or pdac_high_risk_ev:
                primary_category = "新辅助治疗评估"
                primary_action = "当前存在可切除胰腺癌高危线索，应先由MDT综合体能、营养、肿瘤标志物、肿瘤和淋巴结负荷评估；取得病理证据后优先考虑新辅助治疗，而非直接手术"
                alternatives = ["若高危线索复核后不成立且MDT确认可切除、无转移，可进入根治性切除路径"]
                avoid = ["仅凭单一高危线索或受胆道梗阻影响的CA19-9直接决定治疗"]
                if ca_value is not None and ca_value >= 1000:
                    missing += ["需要在解除胆道梗阻或感染影响后复核CA19-9"]
                missing += ["需要明确影像可切除性"]
                basis += ["CACA2025_PDAC_HIGH_RISK"]
            else:
                if head_ev and not tail_ev:
                    primary_category = "条件性根治性手术"
                    primary_action = "经MDT确认可切除且无远处转移后，可行根治性胰十二指肠切除术"
                elif tail_ev and not head_ev:
                    primary_category = "条件性根治性手术"
                    primary_action = "经MDT确认可切除且无远处转移后，可行根治性胰体尾联合脾脏切除术"
                else:
                    primary_category = "MDT可切除性评估"
                    primary_action = "先由MDT明确肿瘤部位和可切除性；若可切除且无远处转移，再进入相应根治性切除路径"
                    missing += ["病灶部位不能从当前资料唯一确定"]
                alternatives = ["若MDT判定存在交界可切除或高危因素，转入病理确认后新辅助治疗路径"]
                avoid = ["在未明确可切除性时把条件性手术答案解释为已经满足手术指征"]
                if resectable_ev and met_status == "报告明确未见":
                    ready = True
                    confidence = "高"
                else:
                    missing += ["当前资料未见明确的MDT可切除性结论"]
                if pdac_node_candidate_ev and not pdac_large_node_ev:
                    missing += ["影像存在可疑淋巴结线索，但未见足以确认其符合‘区域淋巴结较大’高危条件的尺寸或明确表述，需MDT复核"]

        if jaundice_ev:
            prerequisites.append("如存在胆管炎、明显肝功能异常、长期黄疸、体能差或拟行新辅助治疗，应先由MDT评估胆道引流")
            basis += ["CACA2025_PDAC_BILIARY_DRAINAGE"]

    if not pathology_confirmed and disease == "PDAC" and primary_category not in {"条件性根治性手术", "MDT可切除性评估", "补充分期", "补充分期/转移灶确认"}:
        if "缺少治疗前病理学确认" not in missing and "若进入新辅助治疗，缺少治疗前病理学确认" not in missing:
            missing.append("非直接手术治疗路径下缺少治疗前病理学确认")
    if disease == "PDAC" and not (has_enhanced_ct or has_mri):
        confidence = "低"
    if not met_ev and disease == "PDAC":
        missing.append("当前输入未见可引用的远处转移明确阴性或阳性结论")
    if disease == "PDAC":
        status = "可形成流程规范答案" if primary_category != "MDT可切除性评估" else "条件性答案"
        process_usable = primary_category != "MDT可切除性评估"

    basis = list(dict.fromkeys(basis))
    missing = list(dict.fromkeys(missing))
    return {
        "schema_version": "preop_normative_expert_answer_v2",
        "case_id": case["case_id"],
        "patient_id": case["patient"]["patient_id"],
        "patient_uid": case["patient"]["patient_uid"],
        "decision_time": case["decision_time"],
        "cohort_disease_stratum": disease,
        "answer_independence": {
            "uses_agent_input_only": True,
            "observed_clinician_action_used": False,
            "future_outcome_used": False,
            "note": "病种分层仅用于选择规则包，不作为决策时点已确诊事实。",
        },
        "guideline_version_policy": {
            "normative_reference": "current_standard_2025",
            "historical_use_note": "本答案用于当前规范专家参考；如用于评价历史医生行为，应另按决策日期匹配当时已发布指南，不得以2025版倒推过错。",
        },
        "predecision_facts": facts,
        "fact_evidence": evidence,
        "normative_answer": {
            "status": status,
            "primary_action_category": primary_category,
            "primary_action": primary_action,
            "acceptable_alternatives": alternatives,
            "actions_to_avoid": avoid,
            "prerequisites": prerequisites,
            "ready_as_normative_gold": ready,
            "usable_as_process_supervision_label": process_usable,
            "confidence": confidence,
        },
        "guideline_basis": [{"rule_id": rule_id, **GUIDELINE_RULES[rule_id]} for rule_id in basis],
        "missing_information": missing,
    }


def main() -> None:
    cases = read_jsonl(INPUT)
    with INDEX.open("r", encoding="utf-8-sig", newline="") as f:
        index_rows = list(csv.DictReader(f))
    by_case = {row["case_id"]: row for row in index_rows}
    answers = [classify_case(case, by_case[case["case_id"]]) for case in cases]

    OUT_JSONL.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in answers) + "\n", encoding="utf-8")
    used_rule_ids = list(dict.fromkeys(
        rule["rule_id"] for answer in answers for rule in answer["guideline_basis"]
    ))
    used_rules = {rule_id: GUIDELINE_RULES[rule_id] for rule_id in used_rule_ids}
    OUT_RULES.write_text(json.dumps(used_rules, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    flat_rows = []
    missing_rows = []
    for item in answers:
        n = item["normative_answer"]
        f = item["predecision_facts"]
        flat_rows.append(
            {
                "序号": len(flat_rows) + 1,
                "case_id": item["case_id"],
                "患者编号": item["patient_id"],
                "patient_uid": item["patient_uid"],
                "决策时间": item["decision_time"],
                "队列病种分层": item["cohort_disease_stratum"],
                "规范参考版本": "当前规范_CACA胰腺癌V2.0_2025及对应专病指南",
                "答案状态": n["status"],
                "规范首选动作分类": n["primary_action_category"],
                "规范首选动作": n["primary_action"],
                "可作为规范金标准": "Y" if n["ready_as_normative_gold"] else "N",
                "可作为流程监督标签": "Y" if n["usable_as_process_supervision_label"] else "N",
                "置信度": n["confidence"],
                "术前病理确认": f["predecision_pathology_confirmed_malignancy"],
                "远处转移陈述": f["distant_metastasis_statement"],
                "明确可切除性陈述": f["explicit_resectability_statement"],
                "增强CT": f["enhanced_ct_before_decision"],
                "MRI": f["mri_before_decision"],
                "CA19-9状态": f["ca199_status"],
                "待补信息": "；".join(item["missing_information"]),
                "依据规则": "；".join(x["rule_id"] for x in item["guideline_basis"]),
            }
        )
        for missing in item["missing_information"]:
            missing_rows.append(
                {
                    "case_id": item["case_id"], "患者编号": item["patient_id"],
                    "病种": item["cohort_disease_stratum"], "答案状态": n["status"],
                    "缺失内容": missing,
                }
            )

    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(flat_rows[0]))
        writer.writeheader(); writer.writerows(flat_rows)
    with OUT_MISSING.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["case_id", "患者编号", "病种", "答案状态", "缺失内容"])
        writer.writeheader(); writer.writerows(missing_rows)

    action_counts = Counter(x["normative_answer"]["primary_action_category"] for x in answers)
    status_counts = Counter(x["normative_answer"]["status"] for x in answers)
    confidence_counts = Counter(x["normative_answer"]["confidence"] for x in answers)
    disease_counts = Counter(x["cohort_disease_stratum"] for x in answers)
    missing_counts = Counter(row["缺失内容"] for row in missing_rows)
    gold_ready = sum(x["normative_answer"]["ready_as_normative_gold"] for x in answers)
    process_ready = sum(x["normative_answer"]["usable_as_process_supervision_label"] for x in answers)
    max_action_share = max(action_counts.values()) / len(answers)
    audit = {
        "generated_at": "2026-10-08",
        "case_count": len(answers),
        "unique_case_count": len({x["case_id"] for x in answers}),
        "sources": [
            r"F:\BaiduNetdiskDownload\胰腺书籍20260819\规则.docx",
            "https://cacaguidelines.cacakp.com/pdflist/detail?id=424（中国肿瘤整合诊治指南CACA胰腺癌V2.0_2025，官网52页）",
            r"F:\中国胰腺囊性肿瘤诊断指南(2022年).pdf",
            r"F:\中国抗癌协会神经内分泌肿瘤诊治指南（2025版）》.pdf（6页更新精要）",
            "https://www.china-oncology.com/zh/article/doi/10.19401/j.cnki.1007-3639.2025.01.010/（58页正式全文）",
            "https://seleguide.yiigle.com/uploads/guide_html/慢性胰腺炎诊治指南(2018，广州).html",
            str(BASE / "normative_guideline_work" / "胰腺囊性疾病诊治指南2015.pdf"),
        ],
        "disease_distribution": dict(disease_counts),
        "answer_status_distribution": dict(status_counts),
        "primary_action_distribution": dict(action_counts),
        "confidence_distribution": dict(confidence_counts),
        "ready_as_normative_gold_count": gold_ready,
        "usable_as_process_supervision_label_count": process_ready,
        "needs_expert_or_source_supplement_count": len(answers) - process_ready,
        "top_missing_information": dict(missing_counts.most_common()),
        "independence_checks": {
            "observed_action_fields_absent": all("actual_action" not in json.dumps(x, ensure_ascii=False) for x in answers),
            "future_outcome_fields_absent": all("subsequent_feedback" not in json.dumps(x, ensure_ascii=False) for x in answers),
            "all_have_guideline_basis": all(x["guideline_basis"] for x in answers),
            "all_have_primary_action": all(x["normative_answer"]["primary_action"] for x in answers),
        },
        "collapse_check": {
            "largest_action_share": round(max_action_share, 4),
            "over_70_percent": max_action_share > 0.70,
        },
        "scope_note": "PDAC当前规范答案使用CACA胰腺癌V2.0_2025；IPMN/MCN/SCN使用2022年中国PCN指南；PanNET使用中国抗癌协会2025正式全文；慢性胰腺炎使用2018广州指南；SPN使用2015年胰腺囊性疾病指南。历史行为评价仍须按决策日期匹配当时指南，不以2025版倒推。",
    }
    OUT_AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
