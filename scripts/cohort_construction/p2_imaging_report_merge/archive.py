"""Read the hospital imaging-report archive and recover each cell's clinical role.

The archive is an untrusted external file: it is parsed, never executed, and
never extracted to disk. Nothing here writes.

Why this module exists
----------------------
The export's column names are inverted relative to their content:

``报告结论`` ("conclusion") holds the long descriptive findings, and ``报告表现``
("manifestation") holds the short impression. Measured over all 217 CSVs of the
2026-11-09 archive (8,092,246 rows): the ``报告结论`` cell is more descriptive
than the ``报告表现`` cell in 93.0% of rows, and its median length is 97
characters against 23. A reader who trusts the column names writes a report's
findings into the impression field and calls it a conclusion.

Two export layouts coexist inside the same archive:

* **plain** — the two cells are already separated.
* **labeled** — both cells are concatenated into ``报告结论`` behind
  ``原表【报告表现】`` / ``原表【报告结论】`` markers and ``报告表现`` is left empty.

The labeled layout names its own roles, so it is used as *evidence* for the
convention rather than assumed. ``CellConvention`` verifies the two layouts
against each other and refuses to proceed if they disagree.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from collections import defaultdict

# The export's own labels. Kept as constants because they are data, not prose.
COLUMN_IMPRESSION = '报告表现'
COLUMN_CONCLUSION = '报告结论'
MARKER = re.compile(r'^原表【(?P<label>[^】]+)】\n?', re.M)
EXAM_FIELDS = ('检查号', '姓名', '患者编号', '影像号', 'AccNO', '检查方法')
TIME_FIELDS = ('检查日期', '检查时间', '报告日期', '报告时间', '审核日期', '审核时间')
# The export's CSVs do not all carry the same optional columns; the columns read
# here must be present in every file, and nothing is inferred from the rest.
REQUIRED_COLUMNS = ('检查号', COLUMN_IMPRESSION, COLUMN_CONCLUSION) + TIME_FIELDS
# Descriptive prose: what was seen. Use to check which column holds findings.
_DESCRIPTIVE = re.compile(
    r'可见|显示|大小|密度|边缘|边界|形态|直径|信号|强化|增强后|未见明显|增厚|积液|呈')
_IMPRESSIONAL = re.compile(r'建议|考虑|符合|提示|诊断|待排|除外|复查|随诊|可能')


class ArchiveError(RuntimeError):
    """The archive's shape contradicts the assumption the caller relies on."""


def split_labeled(text):
    """Return ``{label: body}`` for the 原表【…】 layout, or ``None``.

    Only a layout that starts at offset 0 and consists solely of labeled
    sections is accepted. Anything else returns ``None`` so the caller treats
    the cell as opaque rather than guessing where a section ends.
    """
    if not isinstance(text, str) or '原表【' not in text:
        return None
    parts = MARKER.split(text)
    if parts[0].strip():
        return None  # prose before the first label: not a pure labeled layout
    sections = {}
    for index in range(1, len(parts), 2):
        sections[parts[index]] = parts[index + 1]
    return sections or None


def recover_cells(row):
    """Map one export row to ``(impression, findings, layout)``.

    The labeled layout is authoritative about its own roles. The plain layout
    relies on the verified column convention. Both are returned with the layout
    name so a receipt can say which evidence each cell rests on.
    """
    conclusion = row.get(COLUMN_CONCLUSION) or ''
    impression = row.get(COLUMN_IMPRESSION) or ''
    for cell in (conclusion, impression):
        sections = split_labeled(cell)
        if sections is not None:
            if COLUMN_IMPRESSION not in sections or COLUMN_CONCLUSION not in sections:
                raise ArchiveError(
                    'LABELED_CELL_INCOMPLETE:' + ','.join(sorted(sections)))
            return (sections[COLUMN_IMPRESSION], sections[COLUMN_CONCLUSION],
                    'labeled_cells')
    return impression, conclusion, 'named_columns'


def descriptive_score(text):
    return len(_DESCRIPTIVE.findall(text)) - len(_IMPRESSIONAL.findall(text))


class CellConvention:
    """Evidence that ``报告结论`` holds findings and ``报告表现`` an impression.

    Two independent checks, both over the rows actually found for the target
    exams. If either fails the merge must stop: a wrong convention silently
    swaps two clinical fields on every patient in the batch.
    """

    def __init__(self):
        self.labeled_exams = 0
        self.plain_exams = 0
        self.both_exams = 0
        self.agreements = 0
        self.disagreements = []
        self.descriptive_votes = {'conclusion': 0, 'impression': 0, 'tie': 0}

    def add_exam(self, exam_no, rows):
        layouts = {recover_cells(row)[2] for row in rows}
        if 'labeled_cells' in layouts:
            self.labeled_exams += 1
        if 'named_columns' in layouts:
            self.plain_exams += 1
        if layouts == {'labeled_cells', 'named_columns'}:
            # Same exam present in both layouts: the plain row must say exactly
            # what the labeled row says. This is the within-dataset proof.
            self.both_exams += 1
            labeled = next(recover_cells(row) for row in rows
                           if recover_cells(row)[2] == 'labeled_cells')
            plain = next(recover_cells(row) for row in rows
                         if recover_cells(row)[2] == 'named_columns')
            if (labeled[0].strip(), labeled[1].strip()) == (plain[0].strip(), plain[1].strip()):
                self.agreements += 1
            else:
                self.disagreements.append(exam_no)
        for row in rows:
            impression, findings, _ = recover_cells(row)
            delta = descriptive_score(findings) - descriptive_score(impression)
            self.descriptive_votes[
                'conclusion' if delta > 0 else
                'impression' if delta < 0 else 'tie'] += 1

    def check(self):
        if self.both_exams and self.disagreements:
            raise ArchiveError('LABELED_AND_NAMED_LAYOUT_DISAGREE:' +
                               ','.join(sorted(self.disagreements)[:10]))
        if self.descriptive_votes['conclusion'] <= self.descriptive_votes['impression']:
            raise ArchiveError('COLUMN_CONVENTION_NOT_SUPPORTED_BY_CONTENT')
        return self.receipt()

    def receipt(self):
        return dict(
            finding='报告结论_holds_findings，报告表现_holds_impression',
            basis=dict(
                labeled_layout_exams=self.labeled_exams,
                named_column_layout_exams=self.plain_exams,
                exams_present_in_both_layouts=self.both_exams,
                both_layout_cell_agreements=self.agreements,
                both_layout_disagreements=self.disagreements,
                descriptive_votes_per_row=self.descriptive_votes,
            ),
            note=('导出列名与内容相反：报告结论 存长描述（所见），报告表现 存短结论。'
                  '本数据集按内容角色取列，不按列名推断；带 原表【…】 标记的行以标记为准。'))


def read_rows(archive_path, exam_numbers, *, encoding='gb18030'):
    """Single pass over the archive; keep only the requested exam numbers.

    Optional columns vary between files — one file in this archive lacks
    ``危急值结果内容`` — so the rule is that every file carries every column this
    merge reads (``REQUIRED_COLUMNS``), and the differing sets are recorded rather
    than assumed equal. A file missing a *read* column is still a hard failure:
    silently absent cells would read as empty clinical text.
    """
    wanted = {str(value).strip() for value in exam_numbers if str(value).strip()}
    rows = defaultdict(list)
    scanned = files = 0
    layouts = set()
    with zipfile.ZipFile(archive_path) as archive:
        names = sorted(info.filename for info in archive.infolist()
                       if info.filename.lower().endswith('.csv'))
        if not names:
            raise ArchiveError('ARCHIVE_HAS_NO_CSV')
        for name in names:
            files += 1
            with archive.open(name) as handle:
                stream = io.TextIOWrapper(handle, encoding=encoding,
                                          errors='replace', newline='')
                reader = csv.DictReader(stream)
                header = tuple(reader.fieldnames or ())
                missing = [column for column in REQUIRED_COLUMNS
                           if column not in header]
                if missing:
                    raise ArchiveError('ARCHIVE_CSV_MISSING_REQUIRED_COLUMN:'
                                       + name + ':' + ','.join(missing))
                layouts.add(header)
                for row in reader:
                    scanned += 1
                    key = (row.get('检查号') or '').strip()
                    if key in wanted:
                        rows[key].append(dict(row, _archive_file=name))
    return dict(rows=dict(rows), scanned_rows=scanned, csv_files=files,
                column_layouts=len(layouts),
                columns=sorted({column for header in layouts for column in header}))