import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from scripts.cohort_construction.paths import data_root

ROOT = data_root()
PROJECT = ROOT / "pipeline_outputs_stage8_v1" / "pancreas_decision_window_enriched_v1"
OUT = PROJECT / "restricted"
DOCUMENTS = ROOT / "患者入院出院文书.csv"
STRONG_POOL = OUT / "strong_pool_pre_t0.parquet"
SHORTLIST = (
    ROOT
    / "pipeline_outputs_stage8_v1"
    / "pancreas_decision_failure_cases_v3"
    / "restricted"
    / "shortlist.xlsx"
)

PAIR = re.compile(
    r"拟实施手术名称\s*[:：]\s*(?P<planned>.*?)\s*实施手术名称\s*[:：]\s*(?P<actual>.*?)"
    r"(?=\s*(?:手术人员|麻醉方式|手术经过|术者|主刀)\s*[:：])",
    re.S,
)
SURGERY_DATE = re.compile(r"手术时间\s*[:：]?\s*(20\d{2})[-/]([01]?\d)[-/]([0-3]?\d)")
RADICAL = re.compile(
    r"胰十二指肠切除|Whipple|胰体尾(?:脾脏)?切除|远端胰腺切除|胰腺次全切除|"
    r"胰腺根治性次全切除|根治性胰腺次全切除|全胰切除|胰腺根治性大部切除|"
    r"根治性大部切除|胰体癌根治|胰尾癌根治|胰腺癌根治"
)
DISTANT_TREATMENT = re.compile(
    r"肝(?:左|右)?(?:外|内)?(?:半|部分|尾状)?(?:叶|段)?(?:肿物|结节|肿瘤|转移灶)?(?:切除|剜除)|"
    r"腹膜(?:结节|转移灶)?切除|网膜(?:结节|转移灶)?切除|射频消融"
)
EXPLORE_BIOPSY = re.compile(r"探查|活检|结节切除")
PALLIATIVE = re.compile(r"胃空肠吻合|胆肠吻合|肠肠吻合|造口|引流|神经离断|无水酒精注射")


def compact(value, limit=500):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def parse_json_list(value):
    if pd.isna(value) or not str(value).strip():
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def classify_actual(procedure):
    procedure = str(procedure or "")
    if RADICAL.search(procedure):
        if DISTANT_TREATMENT.search(procedure):
            return "根治性切除并同期处理远处病灶"
        return "根治性切除"
    if EXPLORE_BIOPSY.search(procedure):
        if PALLIATIVE.search(procedure):
            return "探查/活检并姑息处理"
        return "探查/活检"
    if PALLIATIVE.search(procedure):
        return "姑息处理"
    return "术式未解析"


def extract_intraoperative_finding(text):
    diagnosis_match = re.search(
        r"术中诊断\s*[:：]\s*(.*?)(?=\s*(?:拟实施手术名称|实施手术名称|手术名称)\s*[:：])",
        text,
        re.S,
    )
    exploration_match = re.search(
        r"探查\s*[:：]\s*(.{0,900}?)(?=\s*(?:打开|游离|分离|遂|决定|取|术中冰冻|切开))",
        text,
        re.S,
    )
    pieces = []
    if diagnosis_match:
        pieces.append(compact(diagnosis_match.group(1), 500))
    if exploration_match:
        pieces.append(compact(exploration_match.group(1), 900))
    evidence = " | ".join(dict.fromkeys(piece for piece in pieces if piece))
    distant_pattern = re.compile(
        r"肿瘤有(?:[^。；\n]{0,20})?转移|肿瘤广泛转移|腹腔继发恶性肿瘤|"
        r"肿瘤(?:腹腔|网膜|腹膜)[^。；\n]{0,15}广泛转移|"
        r"可扪及转移结节|(?:肝(?:脏)?|腹膜|网膜|腹壁|盆腔|肠系膜|肺|胸膜|骨)"
        r"[^。；\n]{0,25}(?:多发转移|广泛转移)"
    )
    no_distant_pattern = re.compile(
        r"肿瘤无(?:[^。；\n]{0,40})?转移|未见(?:[^。；\n]{0,20})?转移|未扪及异常结节"
    )
    local_unresectable_pattern = re.compile(
        r"无法切除|不可切除|广泛侵犯|门静脉瘤栓|胰源性门脉高压|"
        r"侵犯(?:腹腔干|肝总动脉|肠系膜上动脉|门静脉|下腔静脉)"
    )
    if distant_pattern.search(evidence):
        category = "术中发现远处转移/高度可疑远处病灶"
    elif local_unresectable_pattern.search(evidence):
        category = "术中发现局部/血管侵犯因素"
    elif no_distant_pattern.search(evidence):
        category = "术中未见明确远处转移"
    else:
        category = "术中所见未充分结构化"
    return category, evidence


def style_workbook(writer):
    for worksheet in writer.book.worksheets:
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for cell in worksheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E78")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for row in worksheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        for index, column in enumerate(worksheet.columns, 1):
            values = [str(cell.value or "") for cell in list(column)[:200]]
            width = min(max(max((len(value) for value in values), default=8) + 2, 10), 52)
            worksheet.column_dimensions[get_column_letter(index)].width = width


def build_signal_base():
    strong = pd.read_parquet(STRONG_POOL)
    rows = {}
    for _, row in strong.iterrows():
        patient_id = str(row["患者ID"])
        rows[patient_id] = {
            "患者ID": patient_id,
            "首次信号日期_检查日期暂代": pd.Timestamp(row["首次信号日期_检查日期暂代"]),
            "信号等级": "强可疑",
            "部位": row["部位"],
            "信号原文": row["原文"],
            "信号证据引用": row["信号证据引用"],
            "分母证据": row["分母证据"],
            "病种": row["病种"],
            "是否追加评估": row["是否追加评估"],
            "追加评估证据": row["追加评估证据"],
            "最终远处转移状态": row["最终远处转移状态_预标注"],
            "M状态证据": row["M状态证据"],
            "开发终点标签": row["开发终点标签"],
            "开发终点证据": row["开发终点证据"],
            "来源": "全量强信号预筛",
            "旧shortlist_case_id": "",
            "旧shortlist原标签": "",
        }

    shortlist = pd.read_excel(SHORTLIST, sheet_name="五类特殊病例")
    for _, row in shortlist.iterrows():
        patient_id = str(row["patient_id"])
        events = parse_json_list(row["preop_strong_metastasis_json"]) + parse_json_list(
            row["preop_uncertain_metastasis_json"]
        )
        valid = []
        event_date = pd.to_datetime(row["event_date"], errors="coerce")
        for event in events:
            date = pd.to_datetime(event.get("date"), errors="coerce")
            if pd.isna(date) or pd.isna(event_date) or not (date < event_date <= date + pd.Timedelta(days=90)):
                continue
            valid.append((date, event))
        if not valid:
            continue
        date, event = sorted(valid, key=lambda item: item[0])[0]
        signal_level = "明确" if any(
            token in str(event.get("text") or "") for token in ("远处转移：", "多发转移", "转移灶", "转移瘤")
        ) and not re.search(r"待排|可能|不排除|不除外|可疑", str(event.get("text") or "")) else "强可疑"
        citation = f"{event.get('source_record_key','')}@{date.date()}:{compact(event.get('text',''))}"
        if patient_id in rows:
            rows[patient_id]["来源"] = "全量强信号预筛+旧shortlist"
            rows[patient_id]["旧shortlist_case_id"] = str(row.get("case_id") or "")
            rows[patient_id]["旧shortlist原标签"] = str(row.get("primary_reason") or "")
            continue
        rows[patient_id] = {
            "患者ID": patient_id,
            "首次信号日期_检查日期暂代": date,
            "信号等级": signal_level,
            "部位": "待人工确认",
            "信号原文": compact(event.get("text", "")),
            "信号证据引用": citation,
            "分母证据": "旧shortlist已有胰腺外科候选路径",
            "病种": str(row.get("disease_labels") or "其他/待定"),
            "是否追加评估": "待核",
            "追加评估证据": "",
            "最终远处转移状态": "待核",
            "M状态证据": "",
            "开发终点标签": "待核",
            "开发终点证据": "",
            "来源": "旧shortlist补充",
            "旧shortlist_case_id": str(row.get("case_id") or ""),
            "旧shortlist原标签": str(row.get("primary_reason") or ""),
        }
    return rows


def main():
    signal_rows = build_signal_base()
    patient_ids = set(signal_rows)
    paired_operations = defaultdict(list)
    csv.field_size_limit(2**31 - 1)
    with DOCUMENTS.open("r", encoding="gb18030", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        for row_number, row in enumerate(reader, start=2):
            patient_id = str(row.get("PATIENT_ID") or "")
            if patient_id not in patient_ids:
                continue
            create_time = pd.to_datetime(row.get("CREATE_DATE_TIME"), errors="coerce")
            if pd.isna(create_time):
                continue
            signal_date = signal_rows[patient_id]["首次信号日期_检查日期暂代"]
            if not (signal_date <= create_time <= signal_date + pd.Timedelta(days=100)):
                continue
            text = str(row.get("文书内容") or "")
            match = PAIR.search(text)
            if not match:
                continue
            planned = compact(match.group("planned"), 240)
            actual = compact(match.group("actual"), 320)
            date_match = SURGERY_DATE.search(text)
            if date_match:
                operation_date = pd.Timestamp(
                    int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
                )
            else:
                operation_date = create_time.normalize()
            days = (operation_date.normalize() - signal_date.normalize()).days
            if not (1 <= days <= 90):
                continue
            citation_base = f"患者入院出院文书.csv:{row_number}@{create_time.date()}"
            intraop_category, intraop_evidence = extract_intraoperative_finding(text)
            paired_operations[patient_id].append(
                {
                    "operation_date": operation_date,
                    "days": days,
                    "planned": planned,
                    "actual": actual,
                    "plan_citation": f"{citation_base}:拟实施手术名称：{planned}",
                    "actual_citation": f"{citation_base}:实施手术名称：{actual}",
                    "intraop_category": intraop_category,
                    "intraop_evidence": f"{citation_base}:{intraop_evidence}" if intraop_evidence else "",
                }
            )

    excluded = Counter()
    closed = []
    for patient_id, signal in signal_rows.items():
        operations = paired_operations.get(patient_id, [])
        if not operations:
            excluded["未找到同文书成对的拟实施/实施手术名称"] += 1
            continue
        operation = sorted(operations, key=lambda item: (item["days"], item["operation_date"]))[0]
        if not RADICAL.search(operation["planned"]):
            excluded["拟实施术式不是明确根治性胰腺切除"] += 1
            continue
        actual_class = classify_actual(operation["actual"])
        if actual_class == "术式未解析":
            excluded["实际术式无法分类"] += 1
            continue
        if actual_class == "根治性切除并同期处理远处病灶":
            decision = "根治性切除并同期处理远处病灶"
        elif actual_class == "根治性切除":
            decision = "继续根治切除"
        else:
            decision = "正确降级为探查/活检/姑息"
        closed.append(
            {
                **signal,
                "计划术式": operation["planned"],
                "计划证据引用": operation["plan_citation"],
                "实际手术日期": operation["operation_date"].date(),
                "距首次信号天数": operation["days"],
                "实际术式": operation["actual"],
                "切除性质": actual_class,
                "实际术式证据引用": operation["actual_citation"],
                "术中发现分类": operation["intraop_category"],
                "术中发现证据引用": operation["intraop_evidence"],
                "手术决策结果": decision,
                "决策链状态": "闭合",
                "T0状态": "缺报告签发/审核时间；当前以检查时间暂代",
            }
        )

    all_closed = pd.DataFrame(closed)
    all_closed["_decision_order"] = all_closed["手术决策结果"].map(
        {
            "正确降级为探查/活检/姑息": 0,
            "根治性切除并同期处理远处病灶": 1,
            "继续根治切除": 2,
        }
    )
    all_closed["_actual_order"] = all_closed["切除性质"].map(
        {
            "探查/活检": 0,
            "探查/活检并姑息处理": 1,
            "姑息处理": 2,
            "根治性切除并同期处理远处病灶": 3,
            "根治性切除": 4,
        }
    )
    all_closed = all_closed.sort_values(
        ["_decision_order", "_actual_order", "首次信号日期_检查日期暂代", "患者ID"]
    )

    downgraded = all_closed[all_closed["手术决策结果"].eq("正确降级为探查/活检/姑息")]
    simultaneous = all_closed[all_closed["切除性质"].eq("根治性切除并同期处理远处病灶")]
    radical = all_closed[
        all_closed["切除性质"].eq("根治性切除")
        & ~all_closed["患者ID"].isin(simultaneous["患者ID"])
    ]
    # Enriched, not prevalence-estimating: retain all rare simultaneous cases,
    # then balance correct downgrades and continued resections up to 60 cases.
    cohort = pd.concat(
        [simultaneous.head(10), downgraded.head(25), radical.head(25)], ignore_index=True
    ).drop_duplicates("患者ID")
    if len(cohort) < 60:
        remaining = all_closed[~all_closed["患者ID"].isin(cohort["患者ID"])].head(60 - len(cohort))
        cohort = pd.concat([cohort, remaining], ignore_index=True)

    drop_cols = ["_decision_order", "_actual_order"]
    cohort = cohort.drop(columns=drop_cols, errors="ignore")
    all_closed = all_closed.drop(columns=drop_cols, errors="ignore")
    review = cohort.head(25).copy()
    for column in ("计划术式正确", "实际术式正确", "信号引用真实", "人工结论", "备注"):
        review[column] = ""

    distribution = cohort["手术决策结果"].value_counts().to_dict()
    actual_distribution = cohort["切除性质"].value_counts().to_dict()
    dominant_share = max(distribution.values(), default=0) / max(len(cohort), 1)
    manifest = {
        "status": "PRE_T0_REVIEW_READY",
        "generated_at": "2026-09-27",
        "source_signal_patients": len(signal_rows),
        "all_closed_chain_n": len(all_closed),
        "selected_cohort_n": len(cohort),
        "decision_distribution": distribution,
        "actual_procedure_distribution": actual_distribution,
        "dominant_decision_share": dominant_share,
        "break_chain": dominant_share > 0.70,
        "exclusion_counts": dict(excluded),
        "selection_note": "富集开发集，不用于发生率估计；计划和实际术式必须来自同一份手术记录的成对字段。",
        "t0_blocker": "缺少影像报告签发/审核时间，首次信号暂以检查时间排序。",
    }

    cohort.to_parquet(OUT / "closed_decision_chain_cohort_60_pre_t0.parquet", index=False)
    all_closed.to_parquet(OUT / "all_closed_decision_chains_pre_t0.parquet", index=False)
    (OUT / "closed_decision_chain_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with pd.ExcelWriter(OUT / "closed_decision_chain_cohort_60_pre_t0.xlsx", engine="openpyxl") as writer:
        cohort.to_excel(writer, index=False, sheet_name="闭合链60例")
        all_closed.to_excel(writer, index=False, sheet_name="全部闭合链")
        review.to_excel(writer, index=False, sheet_name="人工抽检25例")
        pd.DataFrame(
            [{"排除原因": key, "数量": value} for key, value in excluded.most_common()]
        ).to_excel(writer, index=False, sheet_name="排除原因")
        pd.DataFrame(
            [
                {"字段": "决策链闭合", "定义": "术前强信号、明确根治计划、实际术式和决策结果均有引用"},
                {"字段": "继续根治切除", "定义": "实际实施手术名称含明确胰腺根治性切除术式"},
                {"字段": "正确降级", "定义": "拟行根治性切除，实际改为探查、活检或姑息处理"},
                {"字段": "T0限制", "定义": "当前只有检查时间，缺报告签发/审核时间，故为pre-T0版本"},
            ]
        ).to_excel(writer, index=False, sheet_name="字段说明")
        pd.DataFrame(
            [{"指标": key, "值": json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value} for key, value in manifest.items()]
        ).to_excel(writer, index=False, sheet_name="运行清单")
        style_workbook(writer)

    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
