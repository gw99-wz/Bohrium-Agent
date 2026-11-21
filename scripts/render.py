"""Step 7 - render the reviewer-facing artifacts.

Produces, under the output directory:
  bilingual.md        paragraph-aligned source/translation, one block per segment id
  translation.zh.md   clean Chinese reading copy
  result.json         status + the case's own output_contract fields + evidence
  verify_report.json  copy of the machine checks
  pending_review.md   the list a human must confirm
  glossary.tsv        the locked terminology table actually used
  run_manifest.json   inputs, hashes, versions, timings - what makes the run replayable

The contract mapping is deliberately defensive: field names come from the case file,
which is not known in advance. A field this Skill cannot fill is written as null with
a note, never invented.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import common as C
import glossary as G

STATUS_TEXT = {
    "success": "成功：全部段落已翻译并通过校验",
    "partial": "部分完成：存在未翻译或校验未通过的段落，见 pending_review.md",
    "unable": "无法判断/无法完成：没有可用译文，原因见 uncertainty",
}


def contract_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def contract_value(field: str, ctx: dict):
    key = contract_key(field)
    if not key:
        return None, "空字段名"
    rules = [
        (("translation", "zh", "chinese", "译文", "translat", "chinesetext", "targettext"), "translation_text"),
        (("bilingual", "align", "对照", "parallel", "paragraphpair"), "bilingual_text"),
        (("glossary", "term", "术语", "terminology"), "glossary"),
        (("evidence", "source", "locator", "page", "证据", "出处", "location", "citation"), "evidence"),
        (("status", "状态", "state"), "status"),
        (("uncertain", "不确定", "limitation", "limit", "risk"), "uncertainty"),
        (("segment", "段落", "paragraphs"), "segments"),
        (("check", "verify", "qa", "校验", "quality"), "rates"),
        (("summary", "abstract", "摘要", "overview", "conclusion"), "summary"),
        (("file", "artifact", "path", "files"), "artifacts"),
        (("title", "标题"), "title"),
        (("language", "语言"), "language_pair"),
        (("caseid", "case"), "case_id"),
    ]
    for needles, slot in rules:
        if any(n in key for n in needles):
            return ctx.get(slot), None
    return None, f"output_contract 字段 “{field}” 未被本 Skill 定义，已置 null（见 uncertainty）"


def build_payload(dirs: C.Dirs) -> dict:
    segments = C.read_jsonl(dirs.work / "segments.jsonl")
    translations = C.read_jsonl(dirs.work / "translations.jsonl")
    report = C.read_json(dirs.work / "verify_report.json", {}) or {}
    pre = C.read_json(dirs.work / "preflight.json", {}) or {}
    coverage = C.read_json(dirs.work / "coverage.json", {}) or {}
    lock = C.read_json(dirs.work / "glossary.lock.json", {}) or {}
    case_json = C.read_json(dirs.work / "case_snapshot.json", {}) or {}

    zh_by_id = {t["id"]: t for t in translations}
    return {
        "segments": segments,
        "translations": translations,
        "zh_by_id": zh_by_id,
        "report": report,
        "pre": pre,
        "coverage": coverage,
        "lock": lock,
        "case": case_json,
    }


def bilingual_md(payload: dict, case_id: str) -> str:
    segments = payload["segments"]
    zh_by_id = payload["zh_by_id"]
    report = payload["report"]
    rates = report.get("rates", {})
    lines = [
        "# 双语对照（段落级）",
        "",
        f"- 案例：`{case_id}`",
        f"- 源文件：{Path(payload['pre'].get('material') or '').name}",
        f"- 段数：{len(segments)}　已译：{report.get('translated', len(zh_by_id))}",
        f"- 术语一致率：{rates.get('glossary_consistency')}　数字准确率：{rates.get('number_accuracy')}　"
        f"引用准确率：{rates.get('citation_accuracy')}",
        f"- 校验结论：{'通过' if report.get('pass') else '需修复（见 pending_review.md）'}",
        "",
        "> 段号由 `scripts/segment.py` 分配，与 `segments.jsonl` 一一对应；`pN` 为原文页码。",
        "",
    ]
    current_section = None
    for seg in segments:
        if seg["section"] != current_section:
            current_section = seg["section"]
            lines.append(f"\n## §{current_section}\n")
        tr = zh_by_id.get(seg["id"], {})
        zh = (tr.get("zh") or "").strip() or "【未译】"
        tag = f"{seg['id']} · p{seg['page']}"
        if seg["page_end"] != seg["page"]:
            tag += f"–{seg['page_end']}"
        tag += f" · {seg['kind']}"
        if seg.get("figure"):
            tag += f" · {seg['figure']}"
        if tr.get("flags") and "uncertain" in tr["flags"]:
            tag += " · 待确认"
        lines.append(f"**{tag}**")
        lines.append("")
        lines.append(f"> {seg['text']}")
        lines.append("")
        lines.append(zh)
        if tr.get("basis"):
            lines.append("")
            lines.append(f"*译法依据：{tr['basis']}*")
        lines.append("")
    return "\n".join(lines)


def clean_md(payload: dict, case_id: str) -> str:
    segments = payload["segments"]
    zh_by_id = payload["zh_by_id"]
    lines = [f"# {case_id}（中文译文）", ""]
    for seg in segments:
        zh = (zh_by_id.get(seg["id"], {}) or {}).get("zh", "").strip()
        if not zh:
            continue
        if seg["kind"] == "heading":
            lines.append(f"\n## {zh}\n")
        elif seg["kind"] == "title":
            lines.insert(1, f"\n**{zh}**\n")
        elif seg["kind"] == "caption":
            lines.append(f"**{zh}**")
            lines.append("")
        else:
            lines.append(zh)
            lines.append("")
    return "\n".join(lines)


def glossary_block(dirs: C.Dirs) -> list[dict]:
    rows = G.read_glossary_tsv(dirs.work / "glossary.lock.tsv") if (dirs.work / "glossary.lock.tsv").exists() else []
    out = []
    for row in rows:
        if (row.get("status") or "locked") != "locked":
            continue
        out.append(
            {
                "term_id": row.get("term_id", ""),
                "source": row.get("src", ""),
                "target": row.get("zh", ""),
                "first_segment": row.get("first_seg", ""),
                "category": row.get("category", ""),
            }
        )
    return out


def evidence_block(payload: dict) -> list[dict]:
    rows = []
    for seg in payload["segments"]:
        tr = payload["zh_by_id"].get(seg["id"], {})
        rows.append(
            {
                "segment_id": seg["id"],
                "page": seg["page"],
                "page_end": seg["page_end"],
                "section": seg["section"],
                "kind": seg["kind"],
                "block_ids": seg["block_ids"],
                "translated": bool(tr.get("zh")),
            }
        )
    return rows


def main(argv=None) -> int:
    parser = C.add_dir_args(argparse.ArgumentParser(description=__doc__))
    args = parser.parse_args(argv)
    dirs = C.dirs_from(args)

    payload = build_payload(dirs)
    if not payload["segments"]:
        C.fail("no_segments: 先运行 scripts/segment.py")
    report = payload["report"]
    rates = report.get("rates", {})
    translated = report.get("translated", len(payload["translations"]))
    total = len(payload["segments"])
    if translated == 0:
        status = "unable"
    elif report.get("pass") and translated >= total:
        status = "success"
    else:
        status = "partial"

    pre = payload["pre"]
    case = payload["case"]
    case_id = C.case_id_of({"case": case, "material": pre.get("material")})

    bi_text = bilingual_md(payload, case_id)
    zh_text = clean_md(payload, case_id)
    C.write_text(dirs.o("bilingual.md"), bi_text)
    C.write_text(dirs.o("translation.zh.md"), zh_text)

    uncertainty = []
    if status == "partial":
        missing = [s["id"] for s in payload["segments"] if s["id"] not in payload["zh_by_id"]]
        uncertainty.append(f"未翻译段落 {len(missing)} 个：{missing[:20]}")
        for issue in report.get("issues", []):
            if issue.get("severity") == "error":
                uncertainty.append(f"{issue['seg_id']} {issue['type']}: {issue['detail']}")
    for page in (pre.get("material_scan", {}) or {}).get("scanned_pages", []) or []:
        uncertainty.append(f"第 {page} 页无文字层，可能未覆盖（需 OCR/人工核对）")
    if pre.get("warnings"):
        uncertainty.extend(pre["warnings"])

    contract_fields = (pre.get("contract") or {}).get("fields") or []
    contract_notes = []
    ctx = {
        "translation_text": zh_text,
        "bilingual_text": bi_text,
        "glossary": glossary_block(dirs),
        "evidence": evidence_block(payload),
        "status": status,
        "uncertainty": uncertainty,
        "segments": [
            {"segment_id": s["id"], "page": s["page"], "section": s["section"], "kind": s["kind"],
             "source": s["text"], "target": (payload["zh_by_id"].get(s["id"], {}) or {}).get("zh", "")}
            for s in payload["segments"]
        ],
        "rates": rates,
        "summary": STATUS_TEXT[status],
        "artifacts": ["bilingual.md", "translation.zh.md", "result.json", "glossary.tsv", "verify_report.json", "pending_review.md"],
        "title": next((s["text"] for s in payload["segments"] if s["kind"] in {"title", "heading"}), ""),
        "language_pair": f"{pre.get('language', {}).get('source', 'en')}->{pre.get('language', {}).get('target', 'zh')}",
        "case_id": case_id,
    }
    contract_out = {}
    for field in contract_fields:
        value, note = contract_value(field, ctx)
        contract_out[field] = value
        if note:
            contract_notes.append(note)
            uncertainty.append(note)

    result = {
        "case_id": case_id,
        "status": status,
        "status_text": STATUS_TEXT[status],
        "skill": {"name": "paper-translate", "version": "1.0.0"},
        "input": {
            "case_json": pre.get("case_json"),
            "task_md": pre.get("task_md"),
            "material": pre.get("material"),
            "material_sha256": pre.get("material_sha256"),
            "pages": (pre.get("material_scan") or {}).get("pages"),
            "language_pair": ctx["language_pair"],
        },
        "summary": {
            "segments": total,
            "translated": translated,
            "glossary_terms": len(ctx["glossary"]),
            "rates": rates,
            "checks_pass": report.get("pass", False),
            "issue_counts": report.get("issue_counts", {}),
        },
        "output_contract": contract_out,
        "evidence": {"segments": ctx["evidence"], "coverage": payload["coverage"].get("totals", {})},
        "glossary": ctx["glossary"],
        "uncertainty": uncertainty,
        "changes": [
            {"step": "extract", "backend": (C.read_json(dirs.work / "extraction_report.json", {}) or {}).get("backend")},
            {"step": "segment", "segments": total, "char_ratio": payload["coverage"].get("totals", {}).get("char_ratio")},
            {"step": "glossary", "locked_terms": payload["lock"].get("locked"), "lock_sha256": payload["lock"].get("sha256")},
            {"step": "check", "errors": report.get("errors"), "warnings": report.get("warnings")},
        ],
        "artifacts": {
            "bilingual": "bilingual.md",
            "translation": "translation.zh.md",
            "glossary": "glossary.tsv",
            "verify_report": "verify_report.json",
            "pending_review": "pending_review.md",
        },
        "generated_at": C.now_iso(),
    }
    if contract_notes:
        result["contract_notes"] = contract_notes
    C.write_json(dirs.o("result.json"), result)

    for src, dst in [
        (dirs.work / "verify_report.json", "verify_report.json"),
        (dirs.work / "pending_review.md", "pending_review.md"),
        (dirs.work / "coverage.json", "coverage.json"),
        (dirs.work / "glossary.lock.tsv", "glossary.tsv"),
        (dirs.work / "segments.md", "segments.md"),
    ]:
        if Path(src).exists():
            C.write_text(dirs.o(dst), C.read_text(src))

    C.write_json(
        dirs.o("run_manifest.json"),
        {
            "generated_at": C.now_iso(),
            "skill_version": "1.0.0",
            "python": sys.version.split()[0],
            "input": {
                "case_json": pre.get("case_json"),
                "task_md": pre.get("task_md"),
                "material": pre.get("material"),
                "material_sha256": pre.get("material_sha256"),
            },
            "artifacts": sorted(p.name for p in dirs.output.glob("*") if p.is_file()),
            "work_dir": str(dirs.work),
        },
    )

    print(
        json.dumps(
            {
                "ok": True,
                "status": status,
                "case_id": case_id,
                "contract_fields": contract_fields,
                "contract_filled": sum(1 for v in contract_out.values() if v not in (None, "", [], {})),
                "artifacts": sorted(p.name for p in dirs.output.glob("*") if p.is_file()),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
