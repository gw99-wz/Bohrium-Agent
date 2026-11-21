"""Step 2 - extract.

Reads the source document and rebuilds paragraph-level text objects that keep the
layout facts the rest of the pipeline needs: reading order on two-column pages,
column index, font size, and which lines actually belong to one paragraph.

Why line grouping exists: many PDFs export one *line* per text block, so taking
PyMuPDF blocks as paragraphs shreds an abstract into 10 fragments and lets a
sentence leak across two "paragraphs". Grouping by vertical rhythm
(gap / median line gap), short final lines and known section labels rebuilds the
paragraph before anything is translated.

Outputs: work/blocks.jsonl, work/extraction_report.json
Optional: --render-pages 4,5 -> work/pages/p004.png for the OCR / vision fallback.

Backends: PyMuPDF -> pdftotext -layout -> explicit error. DOCX via stdlib zipfile.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import zipfile
from pathlib import Path

import common as C

MATH_CHARS = set("∑∫√±×÷≈≤≥∂∇∞∝⟶→←↔⟨⟩⊗⊕†‡§¶")
MAX_GROUP_CHARS = 1800
CAPTION_RE = re.compile(
    r"^\s*(?:(?:Fig(?:ure)?|FIG(?:URE)?)\.?\s*\d+[A-Za-z]?|Table\s*\d+[A-Za-z]?|"
    r"Scheme\s*\d+|Extended\s+Data\s+(?:Fig(?:ure)?|Table)\.?\s*\d+|"
    r"Supplementary\s+(?:Fig(?:ure)?|Table)\.?\s*\d+|图\s*\d+|表\s*\d+)",
    re.I,
)
REF_HEADING_RE = re.compile(
    r"^\s*(?:\d+\.?\s*)?(?:References|Bibliography|REFERENCES|REFERENCES AND NOTES|"
    r"Notes and References|参考文献)\s*$",
    re.I,
)
NUM_HEADING_RE = re.compile(r"^\s*(?:\d+(?:\.\d+){0,3}\.?)\s+[A-Z\u4e00-\u9fff]")
MATH_FONT_RE = re.compile(r"(math|symbol|cmr|cmmi|cmsy|stix|mtmi|mt-extra|euclid)", re.I)
KNOWN_LABELS = {
    "abstract", "摘要", "introduction", "引言", "绪论", "results", "结果", "discussion", "讨论",
    "methods", "方法", "conclusions", "conclusion", "结论", "acknowledgments", "acknowledgements",
    "致谢", "references", "bibliography", "参考文献", "supplementary information",
    "supplementary material", "experimental", "experimental section", "appendix", "附录",
    "keywords", "keyword", "pacs numbers",
}
TERMINAL = re.compile(r"[.!?。！？]\s*$")
SOFT_TERMINAL = re.compile(r"[:：;；]\s*$")
FRONT_MATTER = re.compile(
    r"^\s*(?:PACS(?:\s+numbers)?\s*[:：]|Keywords?\s*[:：]|DOI\s*[:：]|"
    r"Copyright|©|Received\s|Published\s|Accepted\s|\*Corresponding|E-?mail\s*[:：]|"
    r"Cite this|Edited by|Reviewed by)",
    re.I,
)


# ------------------------------------------------------------------ PDF backend


def load_pdf_lib():
    for name in ("pymupdf", "fitz"):
        try:
            return __import__(name), name
        except Exception:
            continue
    return None, None


def join_lines(lines: list[str]) -> str:
    """Join text lines, undoing end-of-line hyphenation."""
    out = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not out:
            out = line
            continue
        if out.endswith("-") and not out.endswith("--") and line[:1].islower():
            out = out[:-1] + line
        elif out.endswith(("/", "\u2013", "\u2014")) and line[:1].isalnum():
            out = out[:-1] + " " + line
        else:
            out += " " + line
    return out


def line_from_pdf(block, page_no: int) -> dict | None:
    """One visual line: either text or an image placeholder."""
    bbox = [round(float(v), 2) for v in block.get("bbox", [0, 0, 0, 0])]
    if block.get("type") != 0:
        return {"page": page_no, "type": "image", "text": "", "bbox": bbox, "size": 0.0, "bold": False, "math": False}
    parts_all: list[str] = []
    sizes: list[float] = []
    bold = False
    math = False
    for line in block.get("lines", []):
        per_line: list[str] = []
        for span in line.get("spans", []):
            text = span.get("text", "")
            per_line.append(text)
            if text.strip():
                sizes.append(float(span.get("size", 0) or 0))
            font = span.get("font", "") or ""
            if int(span.get("flags", 0) or 0) & 16 or re.search(r"bold|black|heavy|semibold", font, re.I):
                bold = True
            if MATH_FONT_RE.search(font):
                math = True
        joined = "".join(per_line).strip()
        if joined:
            parts_all.append(joined)
    text = join_lines(parts_all)
    if not text:
        return None
    if sum(1 for ch in text if ch in MATH_CHARS) >= 3:
        math = True
    return {
        "page": page_no,
        "type": "text",
        "text": text,
        "bbox": bbox,
        "size": round(max(sizes), 2) if sizes else 0.0,
        "bold": bold,
        "math": math,
    }


def reading_order(items: list[dict], page_width: float) -> list[dict]:
    """Two-column aware reading order; images keep their vertical slot."""
    text_items = [b for b in items if b.get("text")]
    images = [b for b in items if not b.get("text")]
    if not text_items:
        return items
    xs0 = min(b["bbox"][0] for b in text_items)
    xs1 = max(b["bbox"][2] for b in text_items)
    span = max(1.0, xs1 - xs0)
    wide_t = span * 0.62

    def width(b):
        return b["bbox"][2] - b["bbox"][0]

    def center(b):
        return (b["bbox"][0] + b["bbox"][2]) / 2.0

    wide = [b for b in text_items if width(b) >= wide_t]
    narrow = [b for b in text_items if width(b) < wide_t]
    mid = (xs0 + xs1) / 2.0
    two_col = False
    if len(narrow) >= 4:
        left = [b for b in narrow if center(b) < mid]
        right = [b for b in narrow if center(b) >= mid]
        if len(left) >= 2 and len(right) >= 2:
            left_max = max(b["bbox"][2] for b in left)
            right_min = min(b["bbox"][0] for b in right)
            # the two groups must not overlap horizontally: that is what distinguishes
            # two real columns from one column of short lines
            if left_max <= right_min + span * 0.02:
                two_col = True
    if not two_col and len(narrow) >= 8:
        left_n = sum(1 for b in narrow if center(b) < mid)
        two_col = left_n >= 4 and len(narrow) - left_n >= 4

    def col_of(b):
        return 0 if (not two_col or center(b) < mid) else 1

    def sort_key(b):
        return (col_of(b), round(b["bbox"][1], 1), round(b["bbox"][0], 1))

    ordered: list[dict] = []
    cursor = -1e9
    for wb in sorted(wide, key=lambda b: b["bbox"][1]):
        band = [b for b in narrow if cursor <= b["bbox"][1] < wb["bbox"][1]]
        band.sort(key=sort_key)
        ordered.extend(band)
        ordered.append(wb)
        cursor = max(cursor, wb["bbox"][3])
    tail = [b for b in narrow if b["bbox"][1] >= cursor]
    tail.sort(key=sort_key)
    ordered.extend(tail)

    for b in ordered:
        b["col"] = col_of(b)
        b["two_col"] = two_col

    merged: list[dict] = []
    pending = sorted(images, key=lambda b: b["bbox"][1])
    for b in ordered:
        while pending and pending[0]["bbox"][1] <= b["bbox"][1]:
            img = pending.pop(0)
            img["col"] = 0
            img["two_col"] = two_col
            merged.append(img)
        merged.append(b)
    for img in pending:
        img["col"] = 0
        img["two_col"] = two_col
        merged.append(img)
    return merged


# ------------------------------------------------------- paragraph reconstruction


def body_size(lines: list[dict]) -> float:
    weighted: list[float] = []
    for line in lines:
        size = line.get("size") or 0
        if not line.get("text") or size <= 0:
            continue
        repeat = max(1, min(60, len(line["text"]) // 15))
        weighted.extend([size] * repeat)
    return round(statistics.median(weighted), 2) if weighted else 10.0


def heading_hint(line: dict, body: float) -> bool:
    text = line.get("text", "").strip()
    if not text or len(text) > 160:
        return False
    low = text.lower().rstrip(":.：")
    if low in KNOWN_LABELS:
        return True
    size = line.get("size") or body
    if size >= body * 1.12 and not TERMINAL.search(text):
        return True
    if line.get("bold") and len(text) <= 110 and NUM_HEADING_RE.match(text):
        return True
    if text.isupper() and 3 <= len(text) <= 90 and len(text.split()) <= 9:
        return True
    return False


def group_paragraphs(lines: list[dict], body: float) -> list[dict]:
    """Rebuild paragraphs from visual lines.

    A break is inserted when any of the following holds:
      * vertical gap > 1.35x the median line gap of that page/column;
      * the previous line ends a sentence *and* is a short (non-justified) line;
      * the previous line is a heading label;
      * the group would exceed MAX_GROUP_CHARS (split at a sentence boundary).
    """
    groups: list[dict] = []
    images: list[dict] = []
    by_page: dict[int, list[dict]] = {}
    for line in lines:
        if line.get("type") == "image":
            images.append(line)
            continue
        if line.get("text"):
            by_page.setdefault(line["page"], []).append(line)

    for page_no, page_lines in by_page.items():
        # median line gap and column width per (page, col)
        gaps: dict[int, list[float]] = {}
        widths: dict[int, list[float]] = {}
        for idx, line in enumerate(page_lines):
            col = line.get("col", 0)
            widths.setdefault(col, []).append(line["bbox"][2] - line["bbox"][0])
            if idx + 1 < len(page_lines):
                nxt = page_lines[idx + 1]
                if nxt.get("col", 0) == col:
                    gap = nxt["bbox"][1] - line["bbox"][3]
                    if 0 < gap < 200:
                        gaps.setdefault(col, []).append(gap)
        median_gap = {col: (statistics.median(v) if v else 14.0) for col, v in gaps.items()}
        col_width = {col: (max(v) if v else 430.0) for col, v in widths.items()}

        current: dict | None = None
        open_chars = 0
        for line in page_lines:
            col = line.get("col", 0)
            if current is not None and current["col"] == col:
                last = current["last_bbox"]
                gap = line["bbox"][1] - last[3]
                gap_ratio = gap / max(1.0, median_gap.get(col, 14.0))
                prev_short = (last[2] - last[0]) < 0.9 * col_width.get(col, 430.0)
                break_here = False
                if gap_ratio > 1.35:
                    break_here = True
                elif current.get("heading_hint"):
                    break_here = True
                elif FRONT_MATTER.match(current["text"]) or FRONT_MATTER.match(line["text"]):
                    break_here = True
                elif TERMINAL.search(current["text"]) and prev_short:
                    break_here = True
                elif SOFT_TERMINAL.search(current["text"]) and prev_short:
                    break_here = True
                elif open_chars > MAX_GROUP_CHARS and TERMINAL.search(current["text"]):
                    break_here = True
                if not break_here:
                    current["text"] = (
                        current["text"][:-1] + line["text"]
                        if current["text"].endswith("-") and line["text"][:1].islower()
                        else current["text"] + " " + line["text"]
                    )
                    current["bbox"] = [
                        min(current["bbox"][0], line["bbox"][0]),
                        current["bbox"][1],
                        max(current["bbox"][2], line["bbox"][2]),
                        max(current["bbox"][3], line["bbox"][3]),
                    ]
                    current["last_bbox"] = list(line["bbox"])
                    current["lines"] += 1
                    current["size"] = max(current["size"], line.get("size") or 0)
                    current["bold"] = current["bold"] or line.get("bold", False)
                    current["math"] = current["math"] or line.get("math", False)
                    open_chars += len(line["text"])
                    continue
            current = {
                "page": line["page"],
                "type": "text",
                "col": col,
                "text": line["text"],
                "bbox": list(line["bbox"]),
                "last_bbox": list(line["bbox"]),
                "size": line.get("size") or 0.0,
                "bold": line.get("bold", False),
                "math": line.get("math", False),
                "lines": 1,
                "heading_hint": heading_hint(line, body),
            }
            open_chars = len(line["text"])
            groups.append(current)

    for img in images:
        groups.append(
            {
                "page": img["page"],
                "type": "image",
                "col": img.get("col", 0),
                "text": "",
                "bbox": img["bbox"],
                "size": 0.0,
                "bold": False,
                "math": False,
                "lines": 0,
            }
        )
    groups.sort(key=lambda g: (g["page"], g["col"], g["bbox"][1]))
    return groups


# ------------------------------------------------------------- classification


def classify(groups: list[dict], body: float) -> dict:
    counters = {"heading": 0, "caption": 0, "reference": 0, "formula": 0, "footnote": 0, "title": 0, "table_like": 0, "abstract": 0}
    in_references = False
    max_size = max((g.get("size") or 0 for g in groups if g.get("text")), default=0)
    first_text_idx = next((i for i, g in enumerate(groups) if g.get("text")), -1)

    for idx, g in enumerate(groups):
        g["kind"] = "paragraph"
        if g.get("type") == "image":
            g["kind"] = "image"
            continue
        text = g.get("text", "")
        low = text.lower().strip().rstrip(":.：")
        size = g.get("size") or body

        if idx == first_text_idx and title_candidate(g, body, max_size) and counters["title"] == 0:
            g["kind"] = "title"
            counters["title"] += 1
            continue
        if REF_HEADING_RE.match(text) or low in {"references", "bibliography", "参考文献"}:
            g["kind"] = "heading"
            in_references = True
            counters["heading"] += 1
            continue
        if in_references:
            g["kind"] = "reference"
            counters["reference"] += 1
            continue
        if g.get("heading_hint") and len(text) <= 160:
            g["kind"] = "heading"
            if low in {"abstract", "摘要"}:
                counters["abstract"] += 1
            counters["heading"] += 1
            continue
        if CAPTION_RE.match(text) and len(text) < 1600:
            g["kind"] = "caption"
            counters["caption"] += 1
            continue
        if g.get("math") and ("=" in text or sum(ch in MATH_CHARS for ch in text) >= 3) and len(text) < 600:
            g["kind"] = "formula"
            counters["formula"] += 1
            continue
        digits = sum(ch.isdigit() for ch in text)
        if len(text) > 20 and digits / len(text) > 0.4:
            g["kind"] = "table_like"
            counters["table_like"] += 1
            continue
        if g["page"] > 1 and size and size <= body * 0.9 and len(text) < 500 and g["bbox"][1] > 600:
            g["kind"] = "footnote"
            counters["footnote"] += 1
            continue
    return counters


def title_candidate(g: dict, body: float, max_size: float) -> bool:
    text = g.get("text", "")
    if g.get("page") != 1 or not text or len(text) > 320:
        return False
    size = g.get("size") or body
    if max_size > body * 1.15 and size >= max_size * 0.97 and g.get("lines", 1) <= 4:
        return True
    return False

# --------------------------------------------------------------- other backends


def extract_docx(path: Path) -> tuple[list[dict], dict]:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", "replace")
    from xml.etree import ElementTree as ET

    root = ET.fromstring(xml)
    groups: list[dict] = []
    for para in root.iter(f"{ns}p"):
        text = "".join(node.text or "" for node in para.iter(f"{ns}t")).strip()
        if not text:
            continue
        style = ""
        for node in para.iter(f"{ns}pStyle"):
            style = node.get(f"{ns}val", "") or ""
            break
        bold = any(True for _ in para.iter(f"{ns}b"))
        is_heading = bool(re.search(r"heading|标题", style, re.I))
        groups.append(
            {
                "page": 1,
                "type": "text",
                "pid": f"g{len(groups):03d}",
                "col": 0,
                "text": text,
                "bbox": [0, 0, 0, 0],
                "size": 16.0 if is_heading else 10.0,
                "bold": bold,
                "math": False,
                "lines": 1,
                "heading_hint": is_heading,
            }
        )
    return groups, {"backend": "zipfile", "pages": None}


def extract_text(path: Path, suffix: str) -> tuple[list[dict], dict]:
    raw = C.read_text(path)
    chunks: list[tuple[str, str]] = []
    if suffix in {".md", ".markdown"}:
        chunk: list[str] = []
        in_code = False
        for line in raw.splitlines():
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                continue
            if re.match(r"^\s{0,3}#{1,6}\s+", line):
                if chunk:
                    chunks.append(("text", "\n".join(chunk)))
                    chunk = []
                chunks.append(("heading", re.sub(r"^\s{0,3}#{1,6}\s+", "", line)))
                continue
            if not line.strip():
                if chunk:
                    chunks.append(("text", "\n".join(chunk)))
                    chunk = []
                continue
            chunk.append(line)
        if chunk:
            chunks.append(("text", "\n".join(chunk)))
    else:
        chunks = [("text", p) for p in re.split(r"\n\s*\n", raw) if p.strip()]

    groups: list[dict] = []
    for kind, text in chunks:
        text = C.normalize_space(text.replace("\n", " "))
        if len(text) < 3:
            continue
        groups.append(
            {
                "page": 1,
                "type": "text",
                "pid": f"g{len(groups):03d}",
                "col": 0,
                "text": text,
                "bbox": [0, 0, 0, 0],
                "size": 16.0 if kind == "heading" else 10.0,
                "bold": kind == "heading",
                "math": False,
                "lines": 1,
                "heading_hint": kind == "heading",
            }
        )
    return groups, {"backend": "text", "pages": 1}


# ------------------------------------------------------------------------ main


def extract_pdf(path: Path, render_pages: set[int] | None = None, pages_dir: Path | None = None) -> tuple[list[dict], dict]:
    lib, backend = load_pdf_lib()
    report = {"backend": backend, "pages": 0, "two_column_pages": []}
    lines: list[dict] = []

    if lib is not None:
        doc = lib.open(str(path))
        report["pages"] = doc.page_count
        for pno in range(doc.page_count):
            page = doc.load_page(pno)
            raw = page.get_text("dict")
            page_items = [line_from_pdf(b, pno + 1) for b in raw.get("blocks", [])]
            page_items = [b for b in page_items if b is not None]
            ordered = reading_order(page_items, page.rect.width)
            if any(b.get("two_col") for b in ordered):
                report["two_column_pages"].append(pno + 1)
            for idx, item in enumerate(ordered):
                item["order"] = idx
                item["pid"] = f"p{pno + 1}-l{idx:03d}"
                lines.append(item)
            if render_pages and (pno + 1) in render_pages and pages_dir is not None:
                pages_dir.mkdir(parents=True, exist_ok=True)
                page.get_pixmap(dpi=170).save(str(pages_dir / f"p{pno + 1:03d}.png"))
            page = None
        doc.close()
        return lines, report

    # ---- fallback: pdftotext -layout (no font information, paragraphs from blank lines)
    try:
        proc = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"], capture_output=True, check=True)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            json.dumps(
                {
                    "ok": False,
                    "error": "no_pdf_extractor",
                    "detail": f"PyMuPDF 不可用且 pdftotext 调用失败: {type(exc).__name__}: {exc}",
                    "hint": "安装 PyMuPDF (pip install pymupdf) 或 poppler-utils；或按 SKILL.md 的降级流程由 Agent 转写文本",
                },
                ensure_ascii=False,
            )
        )
    text = proc.stdout.decode("utf-8", "replace")
    pages = [p for p in text.split("\f") if p.strip()]
    report.update({"backend": "pdftotext-layout", "pages": len(pages)})
    for pno, page_text in enumerate(pages, start=1):
        for idx, para in enumerate(re.split(r"\n\s*\n", page_text)):
            cleaned = join_lines(para.splitlines())
            if len(cleaned) < 3:
                continue
            lines.append(
                {
                    "page": pno,
                    "type": "text",
                    "pid": f"p{pno}-l000",
                    "order": idx,
                    "col": 0,
                    "text": cleaned,
                    "bbox": [0, 0, 0, 0],
                    "size": 0.0,
                    "bold": False,
                    "math": False,
                    "pre_grouped": True,
                }
            )
    return lines, report


def main(argv=None) -> int:
    parser = C.add_dir_args(argparse.ArgumentParser(description=__doc__))
    parser.add_argument("--render-pages", default="", help="comma separated 1-based page numbers to render as PNG")
    parser.add_argument("--material", default="", help="override the source file path")
    args = parser.parse_args(argv)
    dirs = C.dirs_from(args)

    pre = C.read_json(dirs.w("preflight.json"), {})
    material = args.material or pre.get("material")
    if not material or not Path(material).exists():
        C.fail(f"material_not_found: {material!r}；先运行 scripts/preflight.py")

    path = Path(material)
    suffix = path.suffix.lower()
    render_pages = {int(x) for x in re.findall(r"\d+", args.render_pages)} if args.render_pages else None

    if suffix == ".pdf":
        lines, report = extract_pdf(path, render_pages, dirs.work / "pages")
    elif suffix == ".docx":
        lines, report = extract_docx(path)
    elif suffix in {".md", ".markdown", ".txt", ".tex", ".html", ".htm"}:
        lines, report = extract_text(path, suffix)
    else:
        C.fail(f"unsupported_material: {suffix}")

    if not any(line.get("text") for line in lines):
        report.update(
            {
                "status": "no_text",
                "hint": "未抽取到文本，可能是纯扫描件。用 --render-pages 导出 PNG，由具备视觉能力的 Agent 转写为 work/segments_manual.jsonl",
            }
        )
        C.write_json(dirs.w("extraction_report.json"), report)
        C.fail("no_text_extracted")

    body = body_size([l for l in lines if not l.get("pre_grouped")]) or body_size(lines)
    already_paragraphs = suffix in {".md", ".markdown", ".txt", ".tex", ".html", ".htm", ".docx"} or any(
        l.get("pre_grouped") for l in lines
    )
    if already_paragraphs:
        groups = [
            {
                "page": l["page"], "type": "text", "col": 0, "text": l["text"], "bbox": l["bbox"],
                "size": l.get("size", 0.0), "bold": l.get("bold", False), "math": l.get("math", False),
                "lines": l.get("lines", 1),
                "heading_hint": l.get("heading_hint") or heading_hint(l, body), "pid": l["pid"],
            }
            for l in lines
            if l.get("text")
        ]
    else:
        groups = group_paragraphs(lines, body)

    for idx, g in enumerate(groups):
        g.setdefault("pid", f"p{g['page']}-g{idx:03d}")

    counters = classify(groups, body)

    page_chars: dict[int, int] = {}
    page_images: dict[int, int] = {}
    for g in groups:
        if g.get("type") == "image":
            page_images[g["page"]] = page_images.get(g["page"], 0) + 1
            continue
        page_chars[g["page"]] = page_chars.get(g["page"], 0) + len(re.sub(r"\s", "", g["text"]))

    report.update(
        {
            "material": str(path),
            "material_name": path.name,
            "lines": len([l for l in lines if l.get("type") == "text"]),
            "blocks": len(groups),
            "body_font_size": body,
            "kinds": counters,
            "page_chars": {str(k): v for k, v in sorted(page_chars.items())},
            "page_images": {str(k): v for k, v in sorted(page_images.items())},
            "status": "ok",
        }
    )
    C.write_jsonl(dirs.w("blocks.jsonl"), groups)
    C.write_json(dirs.w("extraction_report.json"), report)
    print(
        json.dumps(
            {
                "ok": True,
                "backend": report["backend"],
                "pages": report.get("pages"),
                "paragraphs": len(groups),
                "lines": report["lines"],
                "kinds": counters,
                "body_font_size": body,
                "two_column_pages": len(report.get("two_column_pages") or []),
                "next": "python scripts/segment.py",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
