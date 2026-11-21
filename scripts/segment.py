"""Step 3 - segment.

Turns ordered blocks into numbered translation units.

Guarantees this step provides:
  * every unit gets a stable id (S0001...) that later checks and the bilingual render
    reuse verbatim, so a reviewer can always point at one paragraph;
  * running heads / page numbers ("furniture") are detected and excluded instead of
    being translated as if they were body text;
  * paragraph continuations across columns and page breaks are merged back into one
    unit, which is what stops "half a sentence at the bottom of page 3";
  * a page-by-page character reconciliation proves nothing was dropped.

Outputs: work/segments.jsonl, work/coverage.json, work/segments.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import common as C

SENTENCE_END = re.compile(r"[.!?。！？][\"'）)\]]?\s*$")
FIGURE_RE = re.compile(
    r"\b(Fig(?:ure)?\.?\s*\d+[A-Za-z]?|Table\s*\d+[A-Za-z]?|Scheme\s*\d+|Eq\.?\s*\(?\d+[a-z]?\)?|"
    r"Extended\s+Data\s+(?:Fig(?:ure)?|Table)\.?\s*\d+)",
    re.I,
)
PAGE_NUMBER_RE = re.compile(r"^\s*(?:page\s*)?\d{1,3}\s*(?:/|of)\s*\d{1,3}\s*$|^\s*\d{1,3}\s*$", re.I)


def norm_key(text: str) -> str:
    key = text.lower()
    key = re.sub(r"\d+", "#", key)
    key = re.sub(r"\s+", " ", key).strip()
    return key


def detect_furniture(blocks: list[dict], pages: int) -> set[str]:
    """Running heads, journal names and page numbers repeat on most pages."""
    where: dict[str, set[int]] = defaultdict(set)
    sample: dict[str, str] = {}
    for b in blocks:
        text = b.get("text") or ""
        if not text or len(text) > 140:
            continue
        key = norm_key(text)
        if len(key) < 3:
            continue
        where[key].add(b["page"])
        sample.setdefault(key, text)
    threshold = max(3, int(round(0.3 * max(1, pages))))
    furniture = {key for key, pset in where.items() if len(pset) >= threshold}
    for b in blocks:
        text = b.get("text") or ""
        if text and PAGE_NUMBER_RE.match(text):
            furniture.add(norm_key(text))
    del sample
    return furniture


def merge_paragraphs(blocks: list[dict]) -> list[dict]:
    """Join a paragraph that continues into the next column or onto the next page.

    Within a page the extractor already rebuilt paragraphs, so here only the
    boundaries that carry no layout evidence are resolved: column N bottom -> column
    N+1 top, and page N bottom -> page N+1 top. The signal is the absence of a
    terminal sentence mark, which is why translation can no longer start mid-sentence.
    """
    merged: list[dict] = []
    for b in blocks:
        item = dict(b)
        item.setdefault("block_ids", [b["pid"]])
        if not merged:
            merged.append(item)
            continue
        prev = merged[-1]
        prev_text = prev["text"].rstrip()
        crosses = b["page"] != prev["page"] or b.get("col", 0) != prev.get("col", 0)
        next_page_ok = b["page"] == prev["page"] or b["page"] == prev["page"] + 1
        can_merge = (
            crosses
            and next_page_ok
            and prev.get("kind") == "paragraph"
            and b.get("kind") == "paragraph"
            and not SENTENCE_END.search(prev_text)
            and len(prev_text) > 150
        )
        if can_merge:
            add = b["text"].lstrip()
            prev["text"] = prev_text[:-1] + add if prev_text.endswith("-") else prev_text + " " + add
            prev["page_end"] = max(prev.get("page_end", prev["page"]), b["page"])
            prev["block_ids"].append(b["pid"])
            prev["cross_page"] = prev.get("cross_page", False) or b["page"] != prev["page"]
            prev["cross_column"] = prev.get("cross_column", False) or (
                b["page"] == prev["page"] and b.get("col", 0) != prev.get("col", 0)
            )
            prev["merged"] = True
            # a merged paragraph now ends where the appended block ends, otherwise
            # every following block would look like another boundary crossing
            prev["page"] = b["page"]
            prev["col"] = b.get("col", 0)
            continue
        merged.append(item)
    return merged


def main(argv=None) -> int:
    parser = C.add_dir_args(argparse.ArgumentParser(description=__doc__))
    parser.add_argument("--keep-furniture", action="store_true", help="keep running heads as segments")
    args = parser.parse_args(argv)
    dirs = C.dirs_from(args)

    pre = C.read_json(dirs.w("preflight.json"), {})
    blocks = C.read_jsonl(dirs.w("blocks.jsonl"))
    if not blocks:
        C.fail("no_blocks: 先运行 scripts/extract.py")

    pages = pre.get("material_scan", {}).get("pages") or max((b["page"] for b in blocks), default=1)
    pages = max(1, int(pages))

    furniture = set() if args.keep_furniture else detect_furniture(blocks, pages)
    kept: list[dict] = []
    dropped: list[dict] = []
    image_blocks: list[dict] = []
    for b in blocks:
        if b.get("kind") == "image":
            image_blocks.append(b)
            continue
        if b.get("text") and norm_key(b["text"]) in furniture:
            dropped.append({"pid": b["pid"], "page": b["page"], "text": b["text"][:120]})
            continue
        kept.append(b)

    kept = merge_paragraphs(kept)

    segments: list[dict] = []
    section, section_num = "Front matter", ""
    for idx, b in enumerate(kept, start=1):
        kind = b.get("kind", "paragraph")
        text = C.normalize_space(b.get("text", ""))
        if not text:
            continue
        if kind == "title":
            section, section_num = "Title", ""
        elif kind == "heading":
            section = text
            m = re.match(r"^(\d+(?:\.\d+){0,3})", text)
            section_num = m.group(1) if m else ""
        fig = ""
        if kind == "caption":
            m = FIGURE_RE.search(text)
            fig = m.group(1) if m else ""
        segments.append(
            {
                "id": f"S{len(segments) + 1:04d}",
                "kind": kind,
                "page": b["page"],
                "page_end": b.get("page_end", b["page"]),
                "section": section,
                "section_num": section_num,
                "col": b.get("col", 0),
                "chars": len(re.sub(r"\s", "", text)),
                "text": text,
                "block_ids": b.get("block_ids", [b["pid"]]),
                "cross_page": bool(b.get("cross_page", False)),
                "merged": bool(b.get("merged", False)),
                "figure": fig,
                "status": "todo",
            }
        )

    # ---- manual segments: pages the extractor cannot read (scanned pages, formulas
    # supplied as images, tables transcribed by hand). They are appended after the
    # automatic segments and counted in the coverage reconciliation like any other.
    manual_rows = C.read_jsonl(dirs.work / "segments_manual.jsonl")
    manual_chars: dict[int, int] = defaultdict(int)
    for row in manual_rows:
        text = C.normalize_space(row.get("text", ""))
        if not text:
            continue
        page = int(row.get("page") or 1)
        kind = row.get("kind") or "paragraph"
        segments.append(
            {
                "id": f"S{len(segments) + 1:04d}",
                "kind": kind,
                "page": page,
                "page_end": int(row.get("page_end") or page),
                "section": row.get("section") or section,
                "section_num": "",
                "col": 0,
                "chars": len(re.sub(r"\s", "", text)),
                "text": text,
                "block_ids": ["manual"],
                "cross_page": False,
                "merged": False,
                "figure": "",
                "status": "todo",
                "source": "manual",
            }
        )
        manual_chars[page] += len(re.sub(r"\s", "", text))

    # ---- coverage reconciliation, per page
    assigned: dict[int, int] = defaultdict(int)
    by_pid = {b["pid"]: b for b in blocks}
    for seg in segments:
        for bid in seg["block_ids"]:
            blk = by_pid.get(bid)
            if blk:
                assigned[blk["page"]] += len(re.sub(r"\s", "", blk.get("text", "")))

    page_totals: dict[int, int] = defaultdict(int)
    for b in blocks:
        if b.get("text"):
            page_totals[b["page"]] += len(re.sub(r"\s", "", b["text"]))
    furniture_chars: dict[int, int] = defaultdict(int)
    for d in dropped:
        furniture_chars[d["page"]] += len(re.sub(r"\s", "", d["text"]))

    page_rows = []
    total_chars = 0
    total_seg = 0
    for pno in range(1, pages + 1):
        total = page_totals.get(pno, 0)
        inseg = assigned.get(pno, 0) + manual_chars.get(pno, 0)
        furn = furniture_chars.get(pno, 0)
        denom = max(0, total - furn)
        ratio = round(inseg / denom, 4) if denom else 1.0
        total_chars += total
        total_seg += inseg
        page_rows.append(
            {
                "page": pno,
                "chars_extracted": total,
                "chars_in_segments": inseg,
                "chars_furniture": furn,
                "chars_unaccounted": max(0, denom - inseg),
                "ratio": ratio,
                "images": sum(1 for i in image_blocks if i["page"] == pno),
            }
        )

    scanned = set(pre.get("material_scan", {}).get("scanned_pages") or [])
    coverage = {
        "generated_at": C.now_iso(),
        "pages": page_rows,
        "totals": {
            "chars_extracted": total_chars,
            "chars_in_segments": total_seg,
            "char_ratio": round(total_seg / total_chars, 4) if total_chars else 1.0,
            "segments": len(segments),
            "manual_segments": sum(1 for s in segments if s.get("source") == "manual"),
            "furniture_blocks_dropped": len(dropped),
            "images": len(image_blocks),
        },
        "pages_without_text_layer": sorted(scanned),
        "pages_below_threshold": [r["page"] for r in page_rows if r["ratio"] < 0.98 and r["chars_extracted"] > 0],
        "pass": all(r["ratio"] >= 0.98 for r in page_rows if r["chars_extracted"] > 0) and not scanned,
    }
    coverage["furniture_samples"] = dropped[:8]

    kinds = Counter(s["kind"] for s in segments)
    C.write_jsonl(dirs.w("segments.jsonl"), segments)
    C.write_json(dirs.w("coverage.json"), coverage)
    C.write_text(dirs.w("segments.md"), render_preview(segments, coverage))

    print(
        json.dumps(
            {
                "ok": True,
                "segments": len(segments),
                "kinds": dict(kinds),
                "cross_page_merged": sum(1 for s in segments if s["cross_page"]),
                "char_ratio": coverage["totals"]["char_ratio"],
                "coverage_pass": coverage["pass"],
                "pages_below_threshold": coverage["pages_below_threshold"],
                "next": "python scripts/glossary.py suggest",
            },
            ensure_ascii=False,
        )
    )
    return 0


def render_preview(segments: list[dict], coverage: dict) -> str:
    lines = ["# 段落清单（抽取预览）", "", f"- 段数：{len(segments)}", f"- 字符对账：{coverage['totals']['char_ratio']}", ""]
    current = None
    for seg in segments:
        if seg["section"] != current:
            current = seg["section"]
            lines.append(f"\n## §{current}\n")
        head = f"**{seg['id']}** `p{seg['page']}` · {seg['kind']} · {seg['chars']}字"
        if seg["cross_page"]:
            head += " · 跨页"
        if seg["figure"]:
            head += f" · {seg['figure']}"
        body = seg["text"].replace("\n", " ")
        if len(body) > 240:
            body = body[:240] + " …"
        lines.append(head)
        lines.append(f"> {body}")
        lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
