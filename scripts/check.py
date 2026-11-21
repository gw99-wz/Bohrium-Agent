"""Step 6 - conservation checks.

Translation quality is judged here by what a program can *prove*, not by how the
Chinese reads:

  completeness   every segment id present exactly once, non-empty, actually Chinese;
  terminology    locked names reused wherever the source term occurs (glossary.py);
  numbers/units  numeric tokens and units survive with the same multiplicity;
  references     [n] citation groups and Fig./Table/Eq. numbers survive;
  formulas       math markers survive rather than being paraphrased away;
  negation       a negative/short-scope cue in the source has a Chinese counterpart;
  length         translation is neither a stub nor an expansion.

Exit code is non-zero when a hard error exists, so the agent must repair and re-run
instead of accepting a silently damaged translation.
Outputs: work/translations.jsonl, work/verify_report.json, work/pending_review.md
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

import common as C
import glossary as G

BRACKET_RE = re.compile(r"\[[0-9]{1,3}(?:\s*[,\-\u2013]\s*[0-9]{1,3})*\]")
NUM_RE = re.compile(r"(?<![A-Za-z0-9])(\d+(?:[.,]\d+)?)(?![A-Za-z0-9])")
UNIT_RE = re.compile(
    r"(?<![A-Za-z])(?:eV|meV|keV|nm|\u03bcm|um|mm|cm|K|mA|\u03bcA|nA|V|mV|W|mW|Hz|kHz|MHz|GHz|"
    r"GPa|MPa|mT|ms|ns|ps|fs|\u03a9|S/cm|cm-1|cm\u22121|cm2|%)(?![A-Za-z])"
)
FIG_REF_RE = re.compile(r"(?i)\b(?:fig(?:ure)?s?|table|eq(?:uation)?s?)\.?\s*\(?(\d+[a-z]?)\)?")
CN_REF_RE = re.compile(r"(图|表|式)\s*\(?(\d+[a-z]?)\)?")
MATH_RE = re.compile(r"[=\u2248\u2264\u2265\u00b1\u00d7\u00f7\u2211\u222b\u221a\u221d\u221e]|\^\{?[0-9A-Za-z]|_\{?[0-9A-Za-z]")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
STRONG_NEG_EN = re.compile(r"(?i)\b(?:not|no|never|none|neither|nor|cannot|can't|without|except|unless|only|fails? to|absent)\b")
NEG_ZH = ("不", "未", "没", "无", "非", "仅", "只", "除", "缺", "免")


def merge_parts(work: Path) -> dict:
    """Collect per-packet output files into one ordered translations.jsonl."""
    segments = C.read_jsonl(work / "segments.jsonl")
    order = {s["id"]: i for i, s in enumerate(segments)}
    merged: dict[str, dict] = {}
    parts = sorted((work / "translation").glob("*.out.jsonl"))
    for part in parts:
        for row in C.read_jsonl(part):
            sid = row.get("id")
            if not sid:
                continue
            zh = (row.get("zh") or "").strip()
            if not zh:
                continue
            merged[sid] = {
                "id": sid,
                "zh": zh,
                "basis": row.get("basis", ""),
                "flags": row.get("flags", []),
                "packet": part.stem.replace(".out", ""),
            }
    rows = sorted(merged.values(), key=lambda r: order.get(r["id"], 10**6))
    C.write_jsonl(work / "translations.jsonl", rows)
    return {
        "parts": len(parts),
        "rows": len(rows),
        "unknown_ids": sorted({r["id"] for r in rows if r["id"] not in order})[:10],
        "missing_ids": [s["id"] for s in segments if s["id"] not in merged][:20],
        "missing_count": sum(1 for s in segments if s["id"] not in merged),
    }


def strip_brackets(text: str) -> str:
    return BRACKET_RE.sub(" ", text)


def numbers(text: str) -> Counter:
    return Counter(NUM_RE.findall(strip_brackets(unicodedata.normalize("NFKC", text))))


def units(text: str) -> Counter:
    return Counter(UNIT_RE.findall(unicodedata.normalize("NFKC", text)))


def figures(text: str) -> Counter:
    out: Counter = Counter()
    for m in FIG_REF_RE.finditer(text):
        kind = re.sub(r"s$", "", m.group(0).split()[0].lower().rstrip("."))
        kind = {"figure": "fig", "fig": "fig", "table": "table", "equation": "eq", "eq": "eq"}.get(kind, kind)
        out[(kind, m.group(1))] += 1
    for m in CN_REF_RE.finditer(text):
        kind = {"图": "fig", "表": "table", "式": "eq"}[m.group(1)]
        out[(kind, m.group(2))] += 1
    return out


def check_segment(seg: dict, tr: dict | None) -> list[dict]:
    sid = seg["id"]
    src = seg["text"]
    if not tr or not (tr.get("zh") or "").strip():
        return [{"type": "missing", "severity": "error", "seg_id": sid, "detail": f"{sid} 没有译文"}]
    zh = tr["zh"].strip()
    issues: list[dict] = []

    if not CJK_RE.search(zh) and len(zh) > 3:
        issues.append({"type": "not_chinese", "severity": "error", "seg_id": sid, "detail": f"{sid} 译文不含中文字符，疑似未翻译"})
    min_len = 1 if seg["kind"] in {"heading", "title", "caption"} else 4
    if len(zh) < min_len:
        issues.append({"type": "too_short", "severity": "error", "seg_id": sid, "detail": f"{sid} 译文过短（{len(zh)} 字符）"})

    ratio = len(zh) / max(1, len(src))
    if len(src) > 80 and (ratio < 0.12 or ratio > 1.3):
        issues.append(
            {
                "type": "length_ratio",
                "severity": "warn",
                "seg_id": sid,
                "detail": f"{sid} 译/原字符比 {ratio:.2f}（原文 {len(src)}，译文 {len(zh)}），可能漏译或扩写",
            }
        )

    src_nums, zh_nums = numbers(src), numbers(zh)
    missing_nums = src_nums - zh_nums
    if missing_nums:
        issues.append(
            {
                "type": "number_missing",
                "severity": "error",
                "seg_id": sid,
                "detail": f"{sid} 少了数字 {sorted(missing_nums.elements())[:8]}",
                "expected": sorted(missing_nums.elements())[:8],
            }
        )
    extra_nums = zh_nums - src_nums
    if extra_nums:
        issues.append(
            {
                "type": "number_added",
                "severity": "warn",
                "seg_id": sid,
                "detail": f"{sid} 多出原文没有的数字 {sorted(extra_nums.elements())[:8]}",
            }
        )

    src_units, zh_units = units(src), units(zh)
    if src_units - zh_units:
        issues.append(
            {
                "type": "unit_missing",
                "severity": "warn",
                "seg_id": sid,
                "detail": f"{sid} 少了单位 {sorted((src_units - zh_units).elements())[:8]}",
            }
        )

    src_brackets = Counter(BRACKET_RE.findall(src))
    zh_brackets = Counter(BRACKET_RE.findall(zh))
    if src_brackets - zh_brackets:
        issues.append(
            {
                "type": "citation_missing",
                "severity": "error",
                "seg_id": sid,
                "detail": f"{sid} 少了文献标记 {sorted((src_brackets - zh_brackets).elements())[:8]}",
            }
        )

    src_figs, zh_figs = figures(src), figures(zh)
    if src_figs - zh_figs:
        issues.append(
            {
                "type": "figure_missing",
                "severity": "warn",
                "seg_id": sid,
                "detail": f"{sid} 少了图表/公式编号 {sorted((src_figs - zh_figs).elements())[:8]}",
            }
        )

    if MATH_RE.search(src) and not MATH_RE.search(zh):
        issues.append({"type": "formula_lost", "severity": "warn", "seg_id": sid, "detail": f"{sid} 原文含公式符号，译文里没有"})

    if STRONG_NEG_EN.search(src) and not any(cue in zh for cue in NEG_ZH):
        issues.append(
            {
                "type": "negation_lost",
                "severity": "warn",
                "seg_id": sid,
                "detail": f"{sid} 原文有否定/限定词（{STRONG_NEG_EN.search(src).group(0)}），译文未见对应中文",
            }
        )

    if tr.get("flags") and "uncertain" in tr["flags"]:
        issues.append({"type": "uncertain", "severity": "info", "seg_id": sid, "detail": f"{sid} 译者标注为不确定"})
    return issues


def run(work: Path, strict: bool) -> dict:
    segments = C.read_jsonl(work / "segments.jsonl")
    translations = C.read_jsonl(work / "translations.jsonl")
    if not segments:
        C.fail("no_segments: 先运行 scripts/segment.py")
    if not translations:
        C.fail("no_translations: 先翻译 work/packets/*.md 并写入 work/translation/*.out.jsonl，然后 check.py --merge")

    zh_by_id = {t["id"]: t for t in translations}
    issues: list[dict] = []
    for seg in segments:
        issues.extend(check_segment(seg, zh_by_id.get(seg["id"])))

    dup_ids = [sid for sid, n in Counter(t["id"] for t in translations).items() if n > 1]
    for sid in dup_ids:
        issues.append({"type": "duplicate", "severity": "error", "seg_id": sid, "detail": f"{sid} 出现多次译文"})

    gloss = G.check(work, work / "translations.jsonl", strict=False)
    issues.extend(gloss.get("issues", []) if isinstance(gloss, dict) else [])

    by_type = Counter(i["type"] for i in issues)
    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warn"]
    infos = [i for i in issues if i["severity"] == "info"]

    total = len(segments)
    translated = sum(1 for s in segments if (zh_by_id.get(s["id"], {}) or {}).get("zh"))
    seg_with_numbers = [s for s in segments if numbers(s["text"])]
    num_bad = {i["seg_id"] for i in issues if i["type"] in {"number_missing"}}
    seg_with_cites = [s for s in segments if BRACKET_RE.search(s["text"])]
    cite_bad = {i["seg_id"] for i in issues if i["type"] == "citation_missing"}
    seg_with_refs = [s for s in segments if figures(s["text"])]
    ref_bad = {i["seg_id"] for i in issues if i["type"] == "figure_missing"}

    def rate(bad: set, pool: list) -> float:
        if not pool:
            return 1.0
        pool_ids = {s["id"] for s in pool}
        return round(1 - len(bad & pool_ids) / len(pool_ids), 4)

    coverage = C.read_json(work / "coverage.json", {}) or {}
    rates = {
        "completeness": round(translated / total, 4) if total else 1.0,
        "segment_coverage": coverage.get("totals", {}).get("char_ratio", 1.0),
        "glossary_consistency": gloss.get("consistency_rate", 1.0) if isinstance(gloss, dict) else 1.0,
        "number_accuracy": rate(num_bad, seg_with_numbers),
        "citation_accuracy": rate(cite_bad, seg_with_cites),
        "reference_accuracy": rate(ref_bad, seg_with_refs),
    }
    report = {
        "generated_at": C.now_iso(),
        "segments": total,
        "translated": translated,
        "missing": total - translated,
        "rates": rates,
        "issue_counts": dict(by_type),
        "errors": len(errors),
        "warnings": len(warnings),
        "infos": len(infos),
        "issues": issues[:400],
        "pass": len(errors) == 0 and rates["completeness"] >= 1.0,
        "strict": strict,
    }
    C.write_json(work / "verify_report.json", report)
    C.write_text(work / "pending_review.md", render_pending(report, segments))
    return {
        "ok": report["pass"],
        "segments": total,
        "translated": translated,
        "rates": rates,
        "issue_counts": dict(by_type),
        "errors": len(errors),
        "warnings": len(warnings),
        "pass": report["pass"],
        "next": "修复 error 项后重跑 check.py，然后 python scripts/render.py",
    }


def render_pending(report: dict, segments: list[dict]) -> str:
    by_id: dict[str, list[dict]] = {}
    for issue in report["issues"]:
        by_id.setdefault(issue.get("seg_id", "-"), []).append(issue)
    lines = [
        "# 需要人工确认的清单",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 段落：{report['segments']}，已译：{report['translated']}",
        f"- 错误 {report['errors']} 条，警告 {report['warnings']} 条",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
    ]
    for key, value in report["rates"].items():
        lines.append(f"| {key} | {value} |")
    lines.append("")
    if not by_id:
        lines.append("没有需要人工确认的项。")
        return "\n".join(lines) + "\n"
    order = [s["id"] for s in segments]
    for sid in sorted(by_id, key=lambda x: order.index(x) if x in order else 10**6):
        items = by_id[sid]
        if all(i["severity"] == "info" for i in items):
            continue
        lines.append(f"## {sid}")
        for item in items:
            lines.append(f"- **{item['severity']}** / {item['type']}：{item['detail']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = C.add_dir_args(argparse.ArgumentParser(description=__doc__))
    parser.add_argument("--merge", action="store_true", help="先把 work/translation/*.out.jsonl 合并成 translations.jsonl")
    parser.add_argument("--strict", action="store_true", help="警告也算失败")
    args = parser.parse_args(argv)
    dirs = C.dirs_from(args)

    if args.merge:
        merged = merge_parts(dirs.work)
        C.write_json(dirs.work / "merge_report.json", merged)
        if merged["rows"] == 0:
            C.fail("no_translations_found: work/translation/ 下没有可合并的 .out.jsonl")
    result = run(dirs.work, args.strict)
    print(json.dumps(result, ensure_ascii=False))
    if not result["ok"]:
        return 1
    if args.strict and result["warnings"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
