# -*- coding: utf-8 -*-
"""Build a review-ready Word catalog for the full question bank."""
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


PROJECT = Path(__file__).resolve().parent.parent
BANK_PATH = PROJECT / "data" / "questions.json"
FALLBACK_PATH = PROJECT / "data" / "fallback_quizzes.json"
OUTPUT_PATH = PROJECT / "output" / "AI_八股训练器题库目录_2026-10-19.docx"

BLACK = RGBColor(0, 0, 0)
DARK_GRAY = RGBColor(75, 75, 75)
MID_GRAY = RGBColor(105, 105, 105)
HEADER_BLUE = "1F4E79"
ROW_BLUE = "EAF2F8"
BORDER_GRAY = "D9D9D9"
EAST_ASIA_FONT = "Microsoft YaHei"
LATIN_FONT = "Aptos"

QUALITY_LABEL = {"strong": "S", "weak": "W", "empty": "E"}


def set_run_font(run, size, bold=False, color=BLACK, italic=False):
    run.font.name = LATIN_FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = color
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.rFonts
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), LATIN_FONT)
    rfonts.set(qn("w:hAnsi"), LATIN_FONT)
    rfonts.set(qn("w:eastAsia"), EAST_ASIA_FONT)


def set_style_font(style, size, bold=False, color=BLACK):
    style.font.name = LATIN_FONT
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.color.rgb = color
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.rFonts
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), LATIN_FONT)
    rfonts.set(qn("w:hAnsi"), LATIN_FONT)
    rfonts.set(qn("w:eastAsia"), EAST_ASIA_FONT)


def configure_section(section, columns=1, margin=0.62):
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(0.62)
    section.bottom_margin = Inches(0.62)
    section.left_margin = Inches(margin)
    section.right_margin = Inches(margin)
    section.header_distance = Inches(0.3)
    section.footer_distance = Inches(0.3)
    cols = section._sectPr.xpath("./w:cols")
    if cols:
        col = cols[0]
        col.set(qn("w:num"), str(columns))
        col.set(qn("w:space"), "320" if columns > 1 else "0")


def add_page_number(paragraph):
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run("第 ")
    set_run_font(run, 8.5, color=MID_GRAY)
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t")
    text.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.extend([begin, instr, separate, text, end])
    tail = paragraph.add_run(" 页")
    set_run_font(tail, 8.5, color=MID_GRAY)


def set_cell_shading(cell, fill):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_cell_margins(cell, top=70, start=95, bottom=70, end=95):
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    mar = tc_pr.first_child_found_in("w:tcMar")
    if mar is None:
        mar = OxmlElement("w:tcMar")
        tc_pr.append(mar)
    for edge, value in (("top", top), ("start", start),
                        ("bottom", bottom), ("end", end)):
        node = mar.find(qn(f"w:{edge}"))
        if node is None:
            node = OxmlElement(f"w:{edge}")
            mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_table_borders(table):
    tbl_pr = table._tbl.tblPr
    borders = tbl_pr.first_child_found_in("w:tblBorders")
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        tbl_pr.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        node = borders.find(qn(f"w:{edge}"))
        if node is None:
            node = OxmlElement(f"w:{edge}")
            borders.append(node)
        node.set(qn("w:val"), "single")
        node.set(qn("w:sz"), "4")
        node.set(qn("w:space"), "0")
        node.set(qn("w:color"), BORDER_GRAY)


def prevent_row_split(row):
    tr_pr = row._tr.get_or_add_trPr()
    cant_split = OxmlElement("w:cantSplit")
    tr_pr.append(cant_split)


def add_summary_table(doc, rows, widths):
    table = doc.add_table(rows=1, cols=len(widths))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    set_table_borders(table)
    headers = ["项目", "数量"] if len(widths) == 2 else [
        "来源仓库", "题目数", "强", "弱", "空"
    ]
    for i, text in enumerate(headers):
        cell = table.rows[0].cells[i]
        cell.width = widths[i]
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        set_cell_shading(cell, HEADER_BLUE)
        set_cell_margins(cell)
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER if i else WD_ALIGN_PARAGRAPH.LEFT
        run = p.add_run(text)
        set_run_font(run, 8.8, bold=True, color=RGBColor(255, 255, 255))
    header_pr = table.rows[0]._tr.get_or_add_trPr()
    repeat_header = OxmlElement("w:tblHeader")
    header_pr.append(repeat_header)
    for index, values in enumerate(rows, 1):
        row = table.add_row()
        prevent_row_split(row)
        for i, value in enumerate(values):
            cell = row.cells[i]
            cell.width = widths[i]
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            set_cell_margins(cell)
            if index % 2 == 0:
                set_cell_shading(cell, ROW_BLUE)
            p = cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER if i else WD_ALIGN_PARAGRAPH.LEFT
            run = p.add_run(str(value))
            set_run_font(run, 8.6, color=BLACK)
    return table


def add_section_heading(doc, text):
    p = doc.add_paragraph(style="Heading 2")
    p.paragraph_format.space_before = Pt(7)
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.keep_with_next = True
    run = p.add_run(text)
    set_run_font(run, 12.5, bold=True, color=BLACK)
    return p


def add_question_entry(doc, q):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(1.6)
    p.paragraph_format.line_spacing = 1.0
    p.paragraph_format.widow_control = True

    id_run = p.add_run(q["id"])
    set_run_font(id_run, 7.7, bold=True, color=DARK_GRAY)
    sep = p.add_run("  ")
    set_run_font(sep, 8.7)
    text_run = p.add_run(q["question"])
    set_run_font(text_run, 8.7, color=BLACK)
    marker = QUALITY_LABEL.get(q.get("excerpt_quality"), "?")
    mark_run = p.add_run(f"  {marker}")
    set_run_font(mark_run, 7.4, bold=True, color=MID_GRAY)


def add_fallback_entry(doc, index, item):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.keep_with_next = True
    run = p.add_run(f"F{index:02d}  {item['knowledge_point']}")
    set_run_font(run, 12, bold=True, color=BLACK)
    tag = p.add_run(f"    {item.get('difficulty', '')}  |  {item['id']}")
    set_run_font(tag, 8.4, color=MID_GRAY)

    q = doc.add_paragraph()
    q.paragraph_format.space_after = Pt(4)
    q.paragraph_format.keep_with_next = True
    run = q.add_run(item["question"])
    set_run_font(run, 10.2)

    for key in ("A", "B", "C", "D"):
        opt = doc.add_paragraph()
        opt.paragraph_format.left_indent = Inches(0.15)
        opt.paragraph_format.space_after = Pt(1.5)
        opt.paragraph_format.keep_with_next = key != "D"
        run = opt.add_run(f"{key}. {item['options'][key]}")
        set_run_font(run, 9.6)

    ans = doc.add_paragraph()
    ans.paragraph_format.space_before = Pt(3)
    ans.paragraph_format.space_after = Pt(2)
    ans.paragraph_format.keep_with_next = True
    run = ans.add_run(f"答案 {item['answer']}")
    set_run_font(run, 9.4, bold=True)

    analysis = doc.add_paragraph()
    analysis.paragraph_format.space_after = Pt(3)
    run = analysis.add_run(item["analysis"])
    set_run_font(run, 9.8)

    sources = item.get("source_paths") or []
    if sources:
        src = doc.add_paragraph()
        src.paragraph_format.space_after = Pt(2)
        run = src.add_run("来源  " + "；".join(sources[:2]))
        set_run_font(run, 8.2, color=MID_GRAY)


def build_document():
    questions = json.loads(BANK_PATH.read_text(encoding="utf-8"))
    fallbacks = json.loads(FALLBACK_PATH.read_text(encoding="utf-8"))

    quality_counts = Counter(q.get("excerpt_quality", "unknown") for q in questions)
    repo_groups = defaultdict(list)
    repo_quality = defaultdict(Counter)
    for q in questions:
        tags = q.get("tags") or []
        repo = tags[-1] if len(tags) > 1 else "unknown"
        repo_groups[repo].append(q)
        repo_quality[repo][q.get("excerpt_quality", "unknown")] += 1
    repo_order = sorted(repo_groups, key=lambda r: (-len(repo_groups[r]), r))

    doc = Document()
    doc.core_properties.title = "AI 八股训练器题库目录"
    doc.core_properties.subject = "当前题库全部题目与离线验证题"
    doc.core_properties.author = "bagu-trainer"

    set_style_font(doc.styles["Normal"], 10.2)
    doc.styles["Normal"].paragraph_format.space_after = Pt(4)
    doc.styles["Normal"].paragraph_format.line_spacing = 1.12
    for style_name, size in (("Title", 27), ("Heading 1", 17), ("Heading 2", 12.5)):
        style = doc.styles[style_name]
        set_style_font(style, size, bold=(style_name != "Title"))
        style.font.color.rgb = BLACK
    title_style_borders = doc.styles["Title"].element.get_or_add_pPr().find(
        qn("w:pBdr"))
    if title_style_borders is not None:
        doc.styles["Title"].element.get_or_add_pPr().remove(title_style_borders)
    doc.styles["Heading 1"].paragraph_format.space_before = Pt(0)
    doc.styles["Heading 1"].paragraph_format.space_after = Pt(10)
    doc.styles["Heading 2"].paragraph_format.space_before = Pt(7)
    doc.styles["Heading 2"].paragraph_format.space_after = Pt(3)

    first = doc.sections[0]
    configure_section(first, columns=1, margin=0.8)
    add_page_number(first.footer.paragraphs[0])

    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_before = Pt(74)
    title = doc.add_paragraph(style="Title")
    title.paragraph_format.space_after = Pt(12)
    title.paragraph_format.keep_with_next = True
    run = title.add_run("AI 八股训练器题库目录")
    set_run_font(run, 27, bold=False, color=BLACK)
    title_pr = title._p.get_or_add_pPr()
    borders = title_pr.find(qn("w:pBdr"))
    if borders is not None:
        title_pr.remove(borders)

    subtitle = doc.add_paragraph()
    subtitle.paragraph_format.space_after = Pt(28)
    run = subtitle.add_run(
        f"主库 {len(questions):,} 题与离线验证题 {len(fallbacks)} 题"
    )
    set_run_font(run, 14.5, color=DARK_GRAY)

    intro = doc.add_paragraph()
    intro.paragraph_format.space_after = Pt(12)
    run = intro.add_run(
        "本目录用于审阅当前题库内容。主库按来源仓库分组，逐条列出题号、"
        "题干和来源质量；主库没有固定选项和答案，实际选择题会结合工作场景生成。"
    )
    set_run_font(run, 11.2)
    intro2 = doc.add_paragraph()
    intro2.paragraph_format.space_after = Pt(26)
    run = intro2.add_run(
        "离线验证题单独列出完整选项、答案、解析和来源，便于直接核对题目质量。"
    )
    set_run_font(run, 11.2)

    meta = doc.add_paragraph()
    run = meta.add_run("生成日期  2026年10月19日")
    set_run_font(run, 9.5, color=MID_GRAY)

    doc.add_page_break()
    h = doc.add_paragraph(style="Heading 1")
    run = h.add_run("题库概览")
    set_run_font(run, 17, bold=True, color=BLACK)
    overview = doc.add_paragraph()
    run = overview.add_run(
        "主库当前收录 5,443 道去重后的知识问答题。来源质量分为强、弱、空三档，"
        "只有强来源会进入情景题生成流程。"
    )
    set_run_font(run, 10.5)

    add_summary_table(doc, [
        ("主库题目", f"{len(questions):,}"),
        ("强来源", f"{quality_counts.get('strong', 0):,}"),
        ("弱来源", f"{quality_counts.get('weak', 0):,}"),
        ("空来源", f"{quality_counts.get('empty', 0):,}"),
        ("来源仓库", f"{len(repo_groups)}"),
        ("离线验证题", f"{len(fallbacks)}"),
    ], [Inches(3.0), Inches(1.25)])

    note = doc.add_paragraph()
    note.paragraph_format.space_before = Pt(10)
    note.paragraph_format.space_after = Pt(11)
    run = note.add_run(
        "质量标记  S 表示来源正文可用，W 表示来源正文偏弱，E 表示未找到可用来源。"
        "该标记描述来源正文，不代表题目事实已经人工验证。"
    )
    set_run_font(run, 9.5, color=DARK_GRAY)

    doc.add_page_break()
    h = doc.add_paragraph(style="Heading 1")
    run = h.add_run("来源仓库分布")
    set_run_font(run, 17, bold=True, color=BLACK)
    intro = doc.add_paragraph()
    intro.paragraph_format.space_after = Pt(9)
    run = intro.add_run("题目按最后一条来源标签归组，重复来源不会重复计数。")
    set_run_font(run, 9.8, color=DARK_GRAY)
    source_rows = [
        (repo, f"{len(repo_groups[repo]):,}",
         f"{repo_quality[repo].get('strong', 0):,}",
         f"{repo_quality[repo].get('weak', 0):,}",
         f"{repo_quality[repo].get('empty', 0):,}")
        for repo in repo_order
    ]
    add_summary_table(doc, source_rows, [
        Inches(2.9), Inches(0.9), Inches(0.72), Inches(0.72), Inches(0.72)
    ])

    body = doc.add_section(WD_SECTION.NEW_PAGE)
    configure_section(body, columns=2, margin=0.55)
    h = doc.add_paragraph(style="Heading 1")
    h.paragraph_format.space_after = Pt(5)
    run = h.add_run("主库全部题目")
    set_run_font(run, 16, bold=True, color=BLACK)
    for repo in repo_order:
        add_section_heading(doc, f"{repo}  {len(repo_groups[repo]):,} 题")
        for q in sorted(repo_groups[repo], key=lambda x: x["id"]):
            add_question_entry(doc, q)

    fallback_section = doc.add_section(WD_SECTION.NEW_PAGE)
    configure_section(fallback_section, columns=1, margin=0.8)
    h = doc.add_paragraph(style="Heading 1")
    run = h.add_run("离线验证题")
    set_run_font(run, 16, bold=True, color=BLACK)
    intro = doc.add_paragraph()
    intro.paragraph_format.space_after = Pt(8)
    run = intro.add_run(
        "以下题目不依赖模型生成，可直接用于离线练习和内容质量抽查。"
    )
    set_run_font(run, 10.2)
    for i, item in enumerate(fallbacks, 1):
        add_fallback_entry(doc, i, item)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUTPUT_PATH)
    return {
        "output": str(OUTPUT_PATH),
        "main_questions": len(questions),
        "fallback_questions": len(fallbacks),
        "repositories": len(repo_groups),
    }


if __name__ == "__main__":
    print(json.dumps(build_document(), ensure_ascii=False, indent=2))
