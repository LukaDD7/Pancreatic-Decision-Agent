import re
from datetime import date

IP = re.compile(r"IPMN|IPMC|IPMT|导管内.{0,12}乳头状.{0,8}[黏粘]液|导管内.{0,8}[黏粘]液.{0,8}乳头", re.I)
BD = re.compile(r"分支(?:胰管|导管)?型|分支胰管来源|BD[- ]?IPMN|IPMN[（(]?分支", re.I)
MD = re.compile(r"主(?:胰管|导管)型|MD[- ]?IPMN|IPMN[（(]?主胰管", re.I)
MT = re.compile(r"混合(?:胰管|导管)?型|M[IT][- ]?IPMN|IPMN[（(]?混合", re.I)
OTHER_CYST = re.compile(r"潴留(?:性)?囊肿|假性囊肿|浆液性|SCN|[黏粘]液性囊(?:腺)?瘤|MCN|实性假乳头|SPN|SPT|囊腺瘤|神经内分泌|淋巴上皮囊肿|单纯囊肿|先天性囊肿")
POSTOP = re.compile(r"(?:胰|胰腺|胰十二指肠|胰体尾|Whipple|胰头|胰尾|胰中段|全胰)[^。；;\n]{0,8}(?:切除)?术后|残(?:余)?胰|胰肠吻合|胰胃吻合", re.I)
MPD_NORMAL = re.compile(r"主胰管[^。；;\n]{0,10}(?:未见|无|不|未)[^。；;\n]{0,4}(?:扩张|增宽)|主胰管[^。；;\n]{0,6}(?:显示)?(?:正常|通畅|纤细)|胰管[^。；;\n]{0,4}(?:未见|无)[^。；;\n]{0,4}扩张")
MPD_DILATED = re.compile(r"主胰管[^。；;\n]{0,12}(?<!未见)(?<!无)(?<!未)(?:明显|显著|全程|弥漫)?扩张|主胰管[^。；;\n]{0,6}增宽")
MPD_NUM = re.compile(r"(?<!分支)(?:主胰管|胰管)[^。；;，,\n\d]{0,22}?(\d+(?:\.\d+)?)\s*(mm|cm|毫米|厘米)", re.I)
CYST_SIZE = re.compile(r"(?:囊性|囊肿|囊状|囊实|IPMN|大者|较大)[^。；;\n]{0,50}?(\d+(?:\.\d+)?\s*(?:mm|cm|毫米|厘米)?(?:\s*[×xX＊*]\s*\d+(?:\.\d+)?\s*(?:mm|cm|毫米|厘米)?){0,2})\s*(mm|cm|毫米|厘米)", re.I)
MALIG_IMG = re.compile(r"恶变|胰腺癌|胰(?:头|体|尾|颈)?(?:部)?癌|恶性(?:肿瘤)?可能|浸润")
PANC_CA_DEFINITE = re.compile(r"胰(?:腺|头|颈|体|尾|体尾|钩突)?(?:部)?(?:导管)?(?:腺)?癌(?!.{0,3}(?:可能|待排|不除外|除外|？|\?))")
PANC_METHOD = re.compile(r"胰|MRCP|水成像|上腹|全腹|腹部|中腹|肝胆|超声内镜|EUS|内镜超声|PET", re.I)
NON_ABD = re.compile(r"^(?!.*(?:腹|胰|肝|胆|MRCP|水成像)).*(?:胸|肺|头颅|颅脑|颈|乳腺|心|膝|髋|腰椎|颈椎|胸椎|骨|鼻|眼|甲状腺|上消化道|造影)", re.I)
PATH_PANC = re.compile(r"胰")
RESECTION_SPEC = re.compile(r"胰十二指肠|胰头.{0,4}切除|胰体尾|胰尾|胰腺.{0,6}切除|全胰|胰腺节段|胰中段|中段胰|保留十二指肠|胰腺肿物|胰腺肿瘤|胰腺病损|胰体|胰头", re.I)
BIOPSY = re.compile(r"穿刺|FNA|细针|活检|刷检|细胞学|小标本", re.I)
HGD_POS = re.compile(r"高级别(?:上皮内瘤变|异型增生|别)|重度异型增生|原位癌|IPMC|浸润(?:性)?癌|癌变|相关(?:性)?浸润|伴浸润|导管内乳头状[黏粘]液性癌")
PDAC_POS = re.compile(r"导管腺癌|腺癌|胰腺癌|癌(?!变)")
NEG_SPAN = re.compile(r"(?:未见|无|不伴|未发现|未|没有|阴性)[^，,。；;]{0,8}(?:高级别|重度异型|浸润|癌|恶性)|(?:高级别|浸润|癌)[^，,。；;]{0,4}(?:\(-\)|（-）|阴性)")
CONCOMITANT = re.compile(r"伴发|独立(?:起源|发生)|另见|另一|分别|与IPMN无关|非IPMN来源|远离")
SURG_TITLE = re.compile(r"手术记录|切除术")
SURG_BODY = re.compile(r"(?:实施手术名称|手术名称|术式)[:：][^\n]{0,40}(?:胰十二指肠|胰体尾|全胰|胰腺.{0,6}切除|胰中段|保留十二指肠)")
SURG_DATE = re.compile(r"手术(?:日期|时间)[:：]?\s*((?:19|20)\d{2}[-/年.]\d{1,2}[-/月.]\d{1,2})")
DEATH_TITLE = re.compile(r"死亡")


def parse_date(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"((?:19|20)\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})", text)
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def add_years(day, years):
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, month=2, day=28)


def modality(method):
    if re.search(r"超声内镜|EUS|内镜超声", method, re.I):
        return "EUS"
    if re.search(r"MRCP|水成像", method, re.I):
        return "MRCP"
    if re.search(r"MR|磁共振", method, re.I):
        return "MRI"
    if re.search(r"PET", method, re.I):
        return "PET"
    if re.search(r"CT", method, re.I):
        return "CT"
    if re.search(r"超声|B超|彩超", method):
        return "US"
    return "OTHER"


def items(text):
    text = re.sub(r"(?:^|[\s。；;])(?=\d{1,2}[、．.]\s*\D)", "\n", text)
    return [item.strip() for item in re.split(r"[\n。；;]", text) if item.strip()]


def classify_item(item):
    match = IP.search(item)
    if not match:
        return "NO_IPMN", ""
    local = item[max(0, match.start() - 14):match.end() + 14]
    if re.search(r"(?:未见|无|排除|除外)[^，,]{0,6}(?:IPMN|导管内乳头)", local, re.I) and not re.search(r"不(?:能)?除外", local):
        return "NEGATED", ""
    if re.search(r"外院|前片(?:示|提示)|原报告|曾(?:诊断|考虑)|病史[:：]|临床(?:诊断|提示)[:：]", item):
        return "HISTORY", "quoted"
    if re.search(r"IPMN.{0,8}术后|(?:切除|手术)术?后.{0,10}(?:病理)?.{0,4}IPMN", item, re.I) and not re.search(r"残(?:余)?胰.{0,12}IPMN", item, re.I):
        return "HISTORY", "postop"
    if re.search(r"(?:IPMN|导管内乳头\S{0,10})(?:\s*)(?:待排|待除外|不除外|不能除外|不能排除|[?？])|(?:待排|不除外|不能除外|不能排除|除外|鉴别)\S{0,4}(?:IPMN|导管内乳头)", item, re.I):
        return "DIFFERENTIAL", "tbe"
    if OTHER_CYST.search(item) and re.search(r"或|与.{0,10}鉴别|鉴别", item):
        return "DIFFERENTIAL", "alt_or"
    if re.search(r"(?:IPMN|IPMT|IPMC)\s*或|或\s*(?:IPMN|IPMT|IPMC)", item, re.I):
        return "DIFFERENTIAL", "alt_or"
    if re.search(r"可能大|可能性大|考虑|首先|符合|多考虑|倾向", local):
        return "LEADING", "favoured"
    if re.search(r"可能|疑", local):
        return "LEADING", "possible"
    return "LEADING", "explicit"


def pancreatic_exam(method, text):
    current_modality = modality(method)
    if current_modality == "OTHER" and not PANC_METHOD.search(method):
        return False
    if NON_ABD.search(method) and "胰" not in text:
        return False
    return "胰" in text or bool(IP.search(text)) or bool(re.search(r"胰|MRCP|水成像", method, re.I))


def mpd_measurement(text):
    values = []
    for item in items(text):
        if "分支胰管" in item and "主胰管" not in item:
            continue
        for match in MPD_NUM.finditer(item):
            value = float(match[1]) * (10 if match[2].lower() in ("cm", "厘米") else 1)
            if 0.5 <= value <= 30:
                values.append(value)
    maximum = max(values) if values else None
    if maximum is not None and maximum >= 5:
        return "dilated", maximum
    if MPD_DILATED.search(text) and not MPD_NORMAL.search(text):
        return "dilated", maximum
    if maximum is not None or MPD_NORMAL.search(text):
        return "normal", maximum
    return "unknown", None


def cyst_size_mm(text):
    values = []
    for item in items(text):
        if "胰" not in item and not IP.search(item):
            continue
        for match in CYST_SIZE.finditer(item):
            numbers = [float(value) for value in re.findall(r"\d+(?:\.\d+)?", match[1])]
            value = max(numbers) * (10 if match[2].lower() in ("cm", "厘米") else 1)
            if 1 <= value <= 200:
                values.append(value)
    return max(values) if values else None


def nodule_status(text):
    statuses = []
    for item in items(text):
        if not re.search(r"壁结节|附壁结节|实性结节|强化结节|实性成分", item):
            continue
        if re.search(r"未见|无|未显示|未探及|不伴", item) and not re.search(r"不除外|不能除外|待排|可疑|可能", item):
            statuses.append("explicit_absent")
        elif re.search(r"不除外|不能除外|待排|可疑|可能|难以|不清", item):
            statuses.append("uncertain")
        else:
            statuses.append("explicit_present_candidate")
    if "explicit_present_candidate" in statuses:
        return "explicit_present_candidate"
    if "uncertain" in statuses:
        return "uncertain"
    if "explicit_absent" in statuses:
        return "explicit_absent"
    return "not_mentioned"


def exam_view(record):
    text = record.get("report_deidentified") or record.get("original_report_text") or ""
    if not text:
        text = "\n".join(str(record.get(key) or "") for key in ("description_deidentified", "diagnosis_deidentified", "报告表现", "报告结论"))
    method = str(record.get("exam_method") or record.get("检查方法") or record.get("检查方法__列32") or "")
    day = parse_date(record.get("exam_date") or record.get("exam_datetime") or record.get("检查日期") or record.get("archive_event_time"))
    current_modality = modality(method)
    pancreas = pancreatic_exam(method, text)
    report_items = items(text)
    leading = any(classify_item(item)[0] == "LEADING" for item in report_items)
    malignant_wording = any(IP.search(item) and MALIG_IMG.search(item) and not re.search(r"待排|？|\?", item) for item in report_items)
    definite_pancreatic_cancer = any(PANC_CA_DEFINITE.search(item) and not re.search(r"IPMN|囊|术后|化疗|放疗", item) for item in report_items)
    return {
        "date": day.isoformat() if day else "",
        "available_date": parse_date(record.get("archive_available_time")).isoformat() if parse_date(record.get("archive_available_time")) else "",
        "modality": current_modality,
        "pancreas": pancreas,
        "postop": bool(POSTOP.search(text)),
        "cyst_present": bool(IP.search(text) or re.search(r"胰[^。；;\n]{0,20}囊", text)),
        "leading": leading,
        "malignant_wording_candidate": malignant_wording,
        "definite_pancreatic_cancer_candidate": definite_pancreatic_cancer,
        "cyst_size_mm": cyst_size_mm(text) if pancreas else None,
        "mpd_status": mpd_measurement(text)[0] if pancreas else "unknown",
        "mpd_mm": mpd_measurement(text)[1] if pancreas else None,
        "nodule_status": nodule_status(text),
    }


def pathology_view(record):
    diagnosis = str(record.get("病理诊断") or "")
    diagnosis = re.split(r"免疫组化|分子检测|特殊染色", diagnosis)[0]
    diagnosis += " " + " ".join(str(record.get(key) or "") for key in ("补充意见1", "补充意见2"))
    specimen_name = str(record.get("标本名称") or "")
    specimen_type = str(record.get("标本类型") or "")
    gross = str(record.get("肉眼所见") or "")
    context = " ".join((specimen_name, specimen_type, gross[:200], diagnosis[:400]))
    pancreas = bool(PATH_PANC.search(context))
    biopsy = bool(BIOPSY.search(specimen_name + specimen_type))
    resection = bool(pancreas and RESECTION_SPEC.search(specimen_name + gross[:120] + diagnosis[:120]) and not biopsy and specimen_type != "小标本") or bool(re.search(r"胰十二指肠|胰体尾", specimen_name))
    clean = NEG_SPAN.sub("", diagnosis)
    ipmn = bool(IP.search(diagnosis))
    malignancy = ""
    if pancreas:
        if ipmn and HGD_POS.search(clean):
            if re.search(r"浸润|IPMC|癌", clean) and re.search(r"浸润(?:性)?癌|相关.{0,4}浸润|伴浸润|浸润性", clean):
                malignancy = "IPMN_IC"
            else:
                malignancy = "IPMN_HGD"
            if PDAC_POS.search(clean) and CONCOMITANT.search(clean):
                malignancy = "IPMN_HGD_IC_PLUS_CPDAC"
        elif PDAC_POS.search(clean) and re.search(r"胰", clean + specimen_name) and not re.search(r"神经内分泌|腺泡|实性假乳头|壶腹|胆管癌|十二指肠(?:乳头)?(?:腺)?癌|胃癌|转移", clean):
            malignancy = "CPDAC_CANDIDATE" if ipmn and CONCOMITANT.search(clean) else "PANC_CA_WITH_IPMN_UNATTRIBUTED" if ipmn else "PANC_CA_NO_IPMN_WORDING"
    specimen_date = parse_date(record.get("specimen_date_parsed") or record.get("取材日期"))
    received_date = parse_date(record.get("received_date_parsed") or record.get("收到日期"))
    report_date = parse_date(record.get("report_date_parsed") or record.get("报告日期"))
    event_date = specimen_date or received_date or report_date
    return {
        "date": event_date.isoformat() if event_date else "",
        "date_basis": "specimen" if specimen_date else "received_proxy" if received_date else "report_proxy" if report_date else "",
        "report_date": report_date.isoformat() if report_date else "",
        "pancreas": pancreas,
        "resection": resection,
        "total_resection_candidate": bool(resection and re.search(r"全胰", context)),
        "biopsy": biopsy,
        "malignancy": malignancy,
        "ipmn_lgd_candidate": bool(pancreas and ipmn and not malignancy and re.search(r"低级别|轻度异型|低度异型", clean)),
        "non_target_pancreas_pathology_candidate": bool(pancreas and not malignancy),
    }


def surgery_date(record):
    title = str(record.get("文书名称") or "")
    text = str(record.get("document_text") or record.get("文书内容") or "")
    if not (SURG_TITLE.search(title) and SURG_BODY.search(text)):
        return None
    match = SURG_DATE.search(text)
    return parse_date(match[1]) if match else None


def lab_kind(name):
    compact = re.sub(r"[\s_（）()\-–—]", "", str(name or "")).upper()
    if "CA199" in compact or "CA19９" in compact or "糖类抗原199" in compact or "糖链抗原199" in compact:
        return "ca199"
    if "糖化血红蛋白" in compact or "HBA1C" in compact:
        return "hba1c"
    if "总胆红素" in compact:
        return "total_bilirubin"
    if "直接胆红素" in compact:
        return "direct_bilirubin"
    if "间接胆红素" in compact:
        return "indirect_bilirubin"
    return ""
