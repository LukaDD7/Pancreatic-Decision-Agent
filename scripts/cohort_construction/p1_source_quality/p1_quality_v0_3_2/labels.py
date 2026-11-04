"""Conservative, quote-backed retrieval labels; no clinical truth inference.

Fix known scope defects by splitting punctuation, locating anatomy objects,
preserving historical statements, and retaining multiple local descriptions.
No label produced here is a patient inclusion requirement on its own.
"""
import hashlib
import html
import re
import unicodedata


def normalize(value):
    text = unicodedata.normalize('NFKC', html.unescape(str(value or '')))
    return (text.replace('\\n', '\n').replace('\\r', '\n').replace('\r', '\n')
            .replace('小于等于', '<=').replace('大于等于', '>=').replace('不超过', '<=')
            .replace('小于', '<').replace('大于', '>'))


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def time_role(text):
    # Role is local to a statement, never inherited from the entire paragraph.
    cues = list(re.finditer(r'本次|目前|此次|现较|现为|现见|既往|此前|上次|原先|较前|原为', text))
    if cues:
        return 'CURRENT_EXPLICIT' if cues[-1].group() in {'本次','目前','此次','现较','现为','现见'} else 'HISTORY_EXPLICIT'
    return 'UNSPECIFIED'


def relationship(text, *, summary=False, template=False):
    """Do not let a negative substring hide a distinct positive predicate."""
    if not text.strip(' 。.;；:：,，、'):
        return 'CONTACT_OR_INVASION' if summary else 'MISSING'
    if re.search(r'无法|难以|不明确|不确定|不能(?:明确|确定|排除)|不除外|待排|可疑|疑似|关系不清|分界不清|界面不清|[?？]', text):
        return 'EXPLICIT_INDETERMINATE'
    neg_pattern = r'(?:无|未见)(?:明显)?(?:接触|受侵|侵犯|包绕|累及|受累)|未受(?:侵|累)(?:犯|及)?|(?:分界|界限|界面)(?:尚)?清(?:楚|晰)|脂肪间隙(?:尚)?(?:清晰|存在)'
    neg = bool(re.search(neg_pattern, text))
    rest = re.sub(neg_pattern, ' ', text)
    positive = bool(re.search(r'接触|受侵|侵犯|包绕|包裹|累及|受压|压迫|粘连|黏连|附着|贴附|紧贴', rest))
    if neg and positive:
        return 'MULTIPLE_DESCRIPTIONS'
    if neg:
        return 'NO_CONTACT_OR_INVASION'
    if (summary or template) and text.strip(' 。.;；:：,，、') == '无':
        return 'NO_CONTACT_OR_INVASION'
    if re.search(r'接触(?:面|角度)\s*[:：]?\s*无(?:[。.;；]|$)', text):
        return 'NO_CONTACT_OR_INVASION'
    angle = re.search(r'(?<!\d)([<>≤≥]=?)?\s*(\d{1,3})\s*(?:°|度)', text)
    if angle and int(angle[2]) > 360:
        return 'UNRECOGNIZED_VALUE'
    if angle and not angle[1] and int(angle[2]) == 0:
        return 'NO_CONTACT_OR_INVASION'
    if re.search(r'接触(?:面|角度)\s*[:：]?\s*$', text) and not angle:
        return 'EMPTY_ANGLE_FIELD'
    if positive or angle:
        return 'CONTACT_OR_INVASION'
    if re.search(r'通畅|显影|显示清晰|未见狭窄|未见闭塞|无(?:明显)?狭窄|无(?:明显)?闭塞', text):
        return 'PATENCY_ONLY'
    return 'UNRECOGNIZED_VALUE'


def vessel_statements(text, rules, source):
    aliases = {}
    for vessel, names in rules['vessels'].items():
        for name in names:
            aliases[name] = vessel
    patterns = [re.escape(a) if not a.isascii() else r'(?<![A-Za-z0-9_])'+a+r'(?![A-Za-z0-9_])'
                for a in sorted(aliases, key=len, reverse=True)]
    vessel_pattern = re.compile('|'.join(patterns), re.I)
    boundaries = re.compile(rules['other_object_boundary_pattern'])
    result = []
    previous = None
    # Commas isolate unrelated organ statements. Explicit segment continuations
    # may inherit a vessel; ordinary organ descriptions never inherit it.
    for unit in re.finditer(r'[^。；;\n，,]+', text):
        content = unit.group()
        between = text[previous['end']:unit.start()] if previous else ''
        if re.search(r'[。；;\n]', between):
            previous = None
        mentions = list(vessel_pattern.finditer(content))
        if not mentions and previous and re.match(r'\s*(?:近段|远段|局部|其中)', content) and not boundaries.search(content):
            state = relationship(content)
            result.append({'vessel': previous['vessel'], 'state': state, 'quote': content,
                           'start': unit.start(), 'end': unit.end(), 'source_field': source,
                           'time_role': time_role(content), 'angles': re.findall(r'[<>]=?\s*\d+(?:[-~至]\d+)?\s*(?:°|度)|\d+\s*(?:°|度)', content),
                           'scope': 'EXPLICIT_SEGMENT_CONTINUATION'})
            previous['end'] = unit.end()
            continue
        if not mentions:
            previous = None
            continue
        heading_prefix = text[max(0, unit.start()-500):unit.start()] + content[:mentions[0].start()]
        headings = list(re.finditer(r'动脉评估|静脉评估|其它评估|其他评估|与血管接触|可疑淋巴结|远处转移', heading_prefix))
        heading = headings[-1].group() if headings else ''
        # Lymph-node encasement is not automatically primary-tumour encasement.
        if re.search(r'(?:可疑)?淋巴结\s*[:：]', content) or heading == '可疑淋巴结':
            previous = None
            continue
        for i, mention in enumerate(mentions):
            stop = mentions[i+1].start() if i+1 < len(mentions) else len(content)
            boundary = boundaries.search(content, mention.end(), stop)
            if boundary:
                stop = boundary.start()
            tail = content[mention.end():stop].strip(' 、和及与:：')
            prefix = content[:mention.start()]
            local_prefix_match = re.search(r'(?:(?:未见|无)(?:明显)?|可疑|疑似|不除外|不能(?:明确|确定|排除)|无法(?:明确|判断)|难以(?:判断|确定))(?:是否)?(?:与)?\s*$', prefix)
            local_prefix = local_prefix_match.group() if local_prefix_match else ''
            # Shared predicates only across a pure list of named vessels.
            if i+1 < len(mentions) and not tail:
                middle = content[mention.end():mentions[-1].start()]
                middle = vessel_pattern.sub('', middle)
                if re.fullmatch(r'[\s、和及与]*', middle):
                    tail = content[mentions[-1].end():]
            if i and not local_prefix:
                before_list = content[:mentions[0].start()]
                middle = vessel_pattern.sub('', content[mentions[0].start():mention.start()])
                if re.fullmatch(r'[\s、和及与]*', middle):
                    m = re.search(r'(?:(?:未见|无)(?:明显)?|可疑|疑似|不除外|不能(?:明确|确定|排除))(?:与)?\s*$', before_list)
                    local_prefix = m.group() if m else ''
            # Do not use an explicit jejunal-branch predicate for the parent SMV.
            parent_tail, child_count = re.subn(r'(?:无(?:明显)?|未见(?:明显)?)?(?:累及|侵犯|受侵|接触)(?:第[一二三1-3])?空肠(?:引流)?支', '', tail)
            # A branch-only phrase cannot turn into an empty summary slot
            # that then falsely supplies CONTACT to the parent vessel.
            branch_only = child_count and not parent_tail.strip(' 、和及与:：')
            state = relationship(local_prefix + parent_tail, summary=heading == '与血管接触' and not branch_only,
                                 template=heading in {'动脉评估', '静脉评估'})
            result.append({'vessel': aliases.get(mention.group(), aliases.get(mention.group().upper())),
                           'state': state, 'quote': content[:stop], 'start': unit.start(), 'end': unit.start()+stop,
                           'source_field': source, 'time_role': time_role(content),
                           'angles': re.findall(r'[<>]=?\s*\d+(?:[-~至]\d+)?\s*(?:°|度)|\d+\s*(?:°|度)', parent_tail),
                           'scope': 'LOCAL_OBJECT'})
        previous = {'vessel': result[-1]['vessel'], 'end': unit.end()} if result else None
    return result


def measurement_statements(text, source):
    """Measurements stay separate by object/time; never count them as conflicts."""
    out = []
    pattern = re.compile(r'\d+(?:\.\d+)?\s*(?:mm|cm|毫米|厘米)?\s*[xX×*]\s*\d+(?:\.\d+)?\s*(?:mm|cm|毫米|厘米)(?:\s*[xX×*]\s*\d+(?:\.\d+)?\s*(?:mm|cm|毫米|厘米))?|(?:最大径|直径|长径|大小)(?:约|为|达|[:：]|\s)*\d+(?:\.\d+)?\s*(?:mm|cm|毫米|厘米)', re.I)
    for match in pattern.finditer(text):
        prefix = text[:match.start()]
        sentence_start = max(prefix.rfind('。'), prefix.rfind(';'), prefix.rfind('；'), prefix.rfind('\n'))+1
        context = text[sentence_start:match.end()]
        nouns = list(re.finditer(r'胆总管|胆管|胰管|血管|淋巴结|肝(?:脏|内)|肾脏|脾脏|标本|病理|胰(?:腺|头|体尾|体|尾|颈)|肿块|肿瘤|占位|病灶', context))
        noun = nouns[-1].group() if nouns else ''
        if re.search(r'标本|病理', context):
            obj = 'PATHOLOGY_OR_SPECIMEN'
        elif noun in {'胆总管', '胆管', '胰管', '血管', '淋巴结', '肝脏', '肝内', '肾脏', '脾脏'}:
            obj = 'OTHER_OBJECT'
        elif noun:
            obj = 'PANCREATIC_LESION_CANDIDATE'
        else:
            obj = 'UNRESOLVED_OBJECT'
        out.append({'quote': match.group(), 'start': match.start(), 'end': match.end(),
                    'source_field': source, 'object': obj, 'time_role': time_role(context[-40:])})
    return out


def metastatic_state(text):
    # "未见变化" describes stability, not absence. Remove only this phrase
    # from the classification view; preserve the original quote unchanged.
    view = re.sub(r'未见(?:明显)?(?:变化|改变|增大|缩小)|无(?:明显)?(?:变化|改变)', '', text)
    if re.search(r'待排|不除外|不能排除|可疑|疑似|无法|难以', view):
        return 'EXPLICIT_INDETERMINATE'
    if re.search(r'(?:无|未见|未发现)(?:明确|明显)?(?:远处)?(?:转移|种植|病变)|(?:转移|种植)\s*[:：]?\s*(?:无|未见)|[:：]\s*无\s*$', view):
        return 'NEGATIVE'
    if re.search(r'转移|种植|播散', view):
        return 'METASTATIC_STATEMENT'
    return 'MENTION_ONLY'


def coverage(text, rules):
    # Empty form headings are not substantive evidence. This affects only the
    # retrieval view; original text and all quote coordinates stay untouched.
    text = re.sub(r'(?:手术指征|可切除性|远处转移|肝脏病变|腹膜转移|肿块评估|动脉评估|静脉评估)\s*[:：][ \t]*(?=[。；;\n]|$)', ' ', text)
    target = bool(re.search(rules['target_pattern'], text, re.I))
    # Structured reports sometimes say "肿块评估：低密度...位于胰头".
    # The section itself supplies the omitted lesion noun, within its bounds.
    tumour_section = re.search(r'肿块评估\s*[:：](.*?)(?=胰胆管评估\s*[:：]|动脉评估\s*[:：]|静脉评估\s*[:：]|其[它他]评估\s*[:：]|$)', text, re.S)
    if tumour_section and re.search(rules['pancreatic_anatomy_pattern'], tumour_section[1]) and re.search(r'位于|密度|信号|强化|弥散|肿块|病灶|无法测量', tumour_section[1]):
        target = True
    anatomy = bool(re.search(rules['pancreatic_anatomy_pattern'], text))
    a = anatomy and bool(re.search(r'肿块|肿瘤|占位|病灶|强化|萎缩|未见.{0,8}(?:占位|异常|病变)|血管|动脉|静脉|胰(?:腺|体尾|头|体|尾|十二指肠)[^。；;\n]{0,15}(?:切除|根治)', text))
    b = bool(re.search(rules['biological_pattern'], text, re.I))
    # Narrative distant-domain statements also represent biological evidence,
    # including explicit negatives; they need not use a template heading.
    b = b or bool(re.search(r'(?:远处|肝脏|肝内|腹膜|网膜)[^。；;\n]{0,20}(?:转移|种植|播散)', text))
    c = bool(re.search(rules['conditional_pattern'], text, re.I))
    plan = bool(re.search(rules['substantive_plan_pattern'], text, re.I))
    return {'target_context': target, 'A': bool(a), 'B': b, 'C': c,
            'ASSESSMENT_OR_ACQUISITION_CONTEXT': plan,
            'substantive_research_related': target and any([a, b, c, plan])}


def parse_image(record, rules):
    statements, measurements, mets = [], [], []
    fields = {k: normalize(record.get(k)) for k in ['original_findings', 'original_impression']}
    for name, text in fields.items():
        statements.extend(vessel_statements(text, rules, name))
        measurements.extend(measurement_statements(text, name))
        for unit in re.finditer(r'[^。；;\n，,]+', text):
            if re.search(r'远处转移|肝脏病变|腹膜|网膜|转移|种植|播散', unit.group()):
                mets.append({'quote': unit.group(), 'start': unit.start(), 'end': unit.end(),
                             'source_field': name, 'state': metastatic_state(unit.group()),
                             'time_role': time_role(unit.group())})
    text = fields['original_findings']+'\n'+fields['original_impression']
    represented = {s['vessel'] for s in statements if s['state'] in {'CONTACT_OR_INVASION', 'NO_CONTACT_OR_INVASION', 'EXPLICIT_INDETERMINATE', 'MULTIPLE_DESCRIPTIONS'}}
    differences = []
    for vessel in rules['vessels']:
        current = [s for s in statements if s['vessel'] == vessel and s['time_role'] != 'HISTORY_EXPLICIT']
        states = {s['state'] for s in current}
        if {'CONTACT_OR_INVASION', 'NO_CONTACT_OR_INVASION'} <= states or 'MULTIPLE_DESCRIPTIONS' in states:
            differences.append({'vessel': vessel, 'flag': 'DIFFERING_LOCAL_DESCRIPTIONS_REVIEW', 'truth_adjudicated': False})
        if len({angle for s in current for angle in s['angles']}) > 1:
            differences.append({'vessel': vessel, 'flag': 'DIFFERING_ANGLE_WORDING_CONTEXT_REQUIRED', 'truth_adjudicated': False})
    return {'has_text': bool(text.strip()), 'coverage': coverage(text, rules), 'vessel_statements': statements,
            'represented_vessels': sorted(v for v in represented if v),
            'not_represented_vessels': sorted(set(rules['vessels'])-represented),
            'measurements': measurements, 'metastatic_statements': mets,
            'difference_flags': differences,
            'postoperative_clue': bool(re.search(r'胰[^。；;\n]{0,25}(?:切除|根治)术后|胰腺癌术后|Whipple.{0,6}术后', text, re.I)),
            'normalized_text_sha256': digest(text)}
