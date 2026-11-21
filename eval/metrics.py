"""Compare a baseline translation and a Skill-run translation on the same source.

Metrics are deliberately runnable by a reviewer with no LLM in the loop:

  term_variants        同一英文术语在译文里出现了几种不同中文写法（越少越好）
  term_locked_share    锁定译名占该术语全部出现次数的比例
  number_recall        原文数字在译文中保留的比例
  citation_recall      原文 [n] 引用组在译文中保留的比例
  figure_localized     Fig./Table 引用是否本地化（图/表），且编号不变
  traceability         译文段落能回指原文段号的比例（无段号即 0）
  uncertainty_marked   译文是否显式标注了无法确定之处

Usage:
    python eval/metrics.py eval/runs/<case>            # 目录下需有 baseline.* 与 skill.*
    python eval/metrics.py --baseline a.md --skill b.md --source src.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

NUM_RE = re.compile(r"(?<![A-Za-z0-9])(\d+(?:[.,]\d+)?)(?![A-Za-z0-9])")
BRACKET_RE = re.compile(r"\[[0-9]{1,3}(?:\s*[,\-\u2013]\s*[0-9]{1,3})*\]")
FIG_EN_RE = re.compile(r"(?i)\bfig(?:ure)?s?\.?\s*(\d+[a-z]?)")
FIG_ZH_RE = re.compile(r"(图|表)\s*(\d+[a-z]?)")


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def term_stats(text: str, terms: dict) -> list[dict]:
    out = []
    for src, spec in terms.items():
        if src.startswith("_"):
            continue
        counts = {}
        for variant in spec["variants"]:
            n = len(re.findall(re.escape(variant), text))
            if n:
                counts[variant] = n
        total = sum(counts.values())
        locked = counts.get(spec["locked"], 0)
        out.append(
            {
                "term": src,
                "locked": spec["locked"],
                "variants_seen": counts,
                "n_variants": len(counts),
                "total": total,
                "locked_share": round(locked / total, 3) if total else None,
            }
        )
    return out


def numbers_recall(source: str, target: str) -> dict:
    src = re.findall(NUM_RE, source)
    tgt = set(re.findall(NUM_RE, target))
    kept = [n for n in src if n in tgt]
    return {
        "source_numbers": len(src),
        "kept": len(kept),
        "recall": round(len(kept) / len(src), 3) if src else 1.0,
        "missing": sorted({n for n in src if n not in tgt})[:10],
    }


def citations_recall(source: str, target: str) -> dict:
    src = re.findall(BRACKET_RE, source)
    tgt = set(re.findall(BRACKET_RE, target))
    kept = [c for c in src if c in tgt]
    return {
        "source_citations": len(src),
        "kept": len(kept),
        "recall": round(len(kept) / len(src), 3) if src else 1.0,
        "missing": sorted({c for c in src if c not in tgt})[:10],
    }


def figure_stats(source: str, target: str) -> dict:
    en = re.findall(FIG_EN_RE, source)
    zh = [m[1] for m in FIG_ZH_RE.findall(target)]
    unlocalized = [m.group(0) for m in FIG_EN_RE.finditer(target)]
    return {
        "source_figure_refs": len(en),
        "localized": len(zh),
        "numbers_in_target": zh,
        "still_english": unlocalized,
        "localized_share": round(len(zh) / len(en), 3) if en else 1.0,
    }


def traceability(text: str, source_paragraphs: int) -> dict:
    """Share of source paragraphs that the output can point back to by segment id."""
    ids = sorted(set(re.findall(r"\bS\d{4}\b", text)))
    total = max(1, source_paragraphs)
    return {
        "segments_referenced": len(ids),
        "source_paragraphs": source_paragraphs,
        "traceable_share": round(min(1.0, len(ids) / total), 3),
    }


def uncertainty(text: str) -> dict:
    cues = ["待确认", "不确定", "存疑", "无法确定", "[不确定]", "疑为"]
    hits = [c for c in cues if c in text]
    return {"marked": bool(hits), "cues": hits}


def evaluate(source: str, target: str, terms: dict, trace_text: str | None = None) -> dict:
    source_paras = len(paragraphs(source))
    return {
        "terms": term_stats(target, terms),
        "numbers": numbers_recall(source, target),
        "citations": citations_recall(source, target),
        "figures": figure_stats(source, target),
        "traceability": traceability(trace_text if trace_text is not None else target, source_paras),
        # uncertainty may live in the bilingual deliverable rather than the clean copy
        "uncertainty": uncertainty(trace_text if trace_text is not None else target),
    }


def scorecard(name: str, res: dict) -> dict:
    terms = res["terms"]
    with_occ = [t for t in terms if t["total"]]
    consistent = [t for t in with_occ if t["n_variants"] == 1]
    return {
        "arm": name,
        "term_consistency": round(len(consistent) / len(with_occ), 3) if with_occ else 1.0,
        "term_variants_total": sum(t["n_variants"] for t in with_occ),
        "number_recall": res["numbers"]["recall"],
        "citation_recall": res["citations"]["recall"],
        "figure_localized_share": res["figures"]["localized_share"],
        "traceable_share": res["traceability"]["traceable_share"],
        "uncertainty_marked": res["uncertainty"]["marked"],
        "paragraphs": res["traceability"]["segments_referenced"],
    }


def render_table(rows: list[dict]) -> str:
    keys = [
        ("term_consistency", "术语一致率（术语只出现一种译法）"),
        ("term_variants_total", "术语变体总数（越少越好）"),
        ("number_recall", "数字保留率"),
        ("citation_recall", "引用标记保留率"),
        ("figure_localized_share", "图表引用本地化率"),
        ("traceable_share", "段号可回指率"),
        ("uncertainty_marked", "是否标注不确定"),
        ("paragraphs", "可回指段数"),
    ]
    head = "| 指标 | " + " | ".join(r["arm"] for r in rows) + " |"
    sep = "| --- | " + " | ".join("---" for _ in rows) + " |"
    lines = [head, sep]
    for key, label in keys:
        cells = []
        for row in rows:
            value = row[key]
            if isinstance(value, bool):
                cells.append("是" if value else "否")
            elif isinstance(value, float):
                cells.append(f"{value:.3f}")
            else:
                cells.append(str(value))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_dir", nargs="?", help="目录，内含 baseline.md / skill.md / source.md")
    parser.add_argument("--baseline", default="")
    parser.add_argument("--skill", default="")
    parser.add_argument("--source", default="")
    parser.add_argument("--terms", default=str(Path(__file__).resolve().parent / "terms.json"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write", default="", help="把报告写到这个路径")
    args = parser.parse_args(argv)

    base_dir = Path(args.case_dir) if args.case_dir else None
    paths = {
        "baseline": Path(args.baseline) if args.baseline else (base_dir / "baseline.md" if base_dir else None),
        "skill": Path(args.skill) if args.skill else (base_dir / "skill.md" if base_dir else None),
        "source": Path(args.source) if args.source else (base_dir / "source.md" if base_dir else None),
    }
    for name, path in paths.items():
        if path is None or not path.exists():
            print(json.dumps({"ok": False, "error": f"{name}_missing", "path": str(path)}, ensure_ascii=False))
            return 2

    terms = json.loads(Path(args.terms).read_text(encoding="utf-8"))
    source = load_text(paths["source"])
    # a bilingual render carries the segment ids; the clean copy does not
    trace_path = base_dir / "skill_bilingual.md" if base_dir else None

    results = {}
    rows = []
    for arm in ("baseline", "skill"):
        trace_text = load_text(trace_path) if (arm == "skill" and trace_path and trace_path.exists()) else None
        res = evaluate(source, load_text(paths[arm]), terms, trace_text)
        results[arm] = res
        rows.append(scorecard(arm, res))

    table = render_table(rows)
    payload = {"ok": True, "scorecard": rows, "detail": results, "table_md": table}
    if args.write:
        Path(args.write).parent.mkdir(parents=True, exist_ok=True)
        Path(args.write).write_text(table + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(table)
        for arm in ("baseline", "skill"):
            print(f"\n[{arm}] 术语明细：")
            for t in results[arm]["terms"]:
                if t["total"]:
                    print(f"  {t['term']:<32} 出现 {t['total']:>2} 次，译法 {t['variants_seen']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
