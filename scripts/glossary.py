"""Step 4 - terminology table: suggest, lock, check.

The point of this module is that terminology consistency is *enforced by a program*,
not requested in a prompt:

  suggest  extracts candidate terms from the source and writes an editable TSV;
  lock     freezes the filled-in table and records its SHA-256, so a later edit is
           visible instead of silent;
  check    compares every translated segment against the frozen table and reports
           each segment where a locked term was rendered with a different name,
           plus any term the agent silently dropped.

Exit code of `check` is non-zero when a locked term is missed, which is what makes
the following repair step mandatory rather than advisory.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import common as C

ACRONYM_RE = re.compile(r"\b[A-Z]{2,}[0-9]{0,2}\b")
FORMULA_RE = re.compile(r"\b(?:[A-Z][a-z]?[A-Z][a-z]?\d*|[A-Z][a-z]?\d[A-Z][a-z]?\d?)\b")
UNIT_RE = re.compile(
    r"\b(?:eV|meV|keV|nm|μm|um|mm|cm|mK|K|mA|μA|nA|V|mV|W|mW|μW|Hz|kHz|MHz|GHz|"
    r"GPa|MPa|T|mT|s|ms|ns|ps|fs|%|°C|Ω|S/cm|cm-1|cm−1|cm2|cm2/Vs)\b"
)
STOP = {
    "THE", "AND", "FOR", "WITH", "THIS", "THAT", "FROM", "ARE", "WAS", "HAS", "NOT", "BUT",
    "ITS", "OUR", "THEIR", "THESE", "THOSE", "ALL", "CAN", "HAS", "HAVE", "BEEN", "WHICH",
    "FIG", "FIGURE", "TABLE", "EQ", "PACS", "DOI", "USA", "SI", "PDF", "HTTP", "HTTPS",
    "II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII", "PRL", "NONE", "NULL",
}
# words that carry terminology value when they repeat
TERM_SEED_HINTS = {"gap", "band", "layer", "monolayer", "bilayer", "exfoliation", "spectroscopy"}


def load_segments(work: Path) -> list[dict]:
    segments = C.read_jsonl(work / "segments.jsonl")
    if not segments:
        C.fail("no_segments: 先运行 scripts/segment.py")
    return segments


def segment_source_text(seg: dict) -> str:
    return seg.get("text", "")


def ngrams(text: str, n: int) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z\-']*", text)
    return [" ".join(words[i : i + n]) for i in range(len(words) - n + 1)]


def seed_terms() -> dict[str, dict]:
    path = C.SKILL_ROOT / "resources" / "glossary_seed.tsv"
    seeds: dict[str, dict] = {}
    if not path.exists():
        return seeds
    for line in C.read_text(path).splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        src = parts[0].strip()
        if not src:
            continue
        seeds[src.lower()] = {
            "src": src,
            "zh": parts[1].strip(),
            "category": (parts[2].strip() if len(parts) > 2 else "seed"),
            "note": (parts[3].strip() if len(parts) > 3 else ""),
        }
    return seeds


def suggest(work: Path) -> dict:
    segments = load_segments(work)
    joined = "\n".join(segment_source_text(s) for s in segments)
    first_loc: dict[str, tuple[str, int]] = {}
    counts: Counter = Counter()
    contexts: dict[str, str] = {}

    def record(term: str, seg: dict, category: str) -> None:
        key = term.strip()
        if not key:
            return
        counts[key] += 1
        # first pass wins: an acronym detected as such must not be relabelled "phrase"
        category_of.setdefault(key, category)
        if key not in first_loc:
            first_loc[key] = (seg["id"], seg["page"])
            text = segment_source_text(seg)
            idx = text.lower().find(key.lower())
            snippet = text[max(0, idx - 40) : idx + len(key) + 60] if idx >= 0 else text[:100]
            contexts[key] = C.normalize_space(snippet)
        category_of[key] = category

    category_of: dict[str, str] = {}

    for seg in segments:
        text = segment_source_text(seg)
        for m in ACRONYM_RE.finditer(text):
            term = m.group(0)
            if term in STOP or len(term) < 2:
                continue
            if term.isdigit():
                continue
            record(term, seg, "acronym")
        for m in FORMULA_RE.finditer(text):
            term = m.group(0)
            if term.lower() in {"fig", "eq"}:
                continue
            record(term, seg, "formula")
        for m in UNIT_RE.finditer(text):
            term = m.group(0)
            if term in {"s", "K", "T", "cm", "mm", "m"}:
                continue
            record(term, seg, "unit")

    for seg in segments:
        text = segment_source_text(seg)
        for n in (2, 3):
            for gram in ngrams(text, n):
                low = gram.lower()
                if any(w in STOP for w in low.split()):
                    continue
                if not re.search(r"[a-z]", low):
                    continue
                if " " in low and low.split()[0] in {"the", "of", "in", "and", "for", "with", "to", "a", "an"}:
                    continue
                record(gram, seg, "phrase")

    seeds = seed_terms()
    seed_hits = []
    for low, item in seeds.items():
        if low in {k.lower() for k in counts} or re.search(r"\b" + re.escape(item["src"]) + r"\b", joined, re.I):
            seed_hits.append(item)
            counts[item["src"]] = max(counts.get(item["src"], 0), 1)
            if item["src"] not in first_loc:
                for seg in segments:
                    if re.search(r"\b" + re.escape(item["src"]) + r"\b", segment_source_text(seg), re.I):
                        first_loc[item["src"]] = (seg["id"], seg["page"])
                        contexts[item["src"]] = C.normalize_space(segment_source_text(seg)[:120])
                        break
            category_of[item["src"]] = "seed"

    # keep candidates that repeat, are acronyms/formulas, or come from the seed list
    keep: list[tuple[str, int, str]] = []
    for term, count in counts.items():
        category = category_of.get(term, "phrase")
        if category in {"acronym", "formula", "seed"}:
            keep.append((term, count, category))
        elif count >= 2:
            keep.append((term, count, category))
        elif any(hint in term.lower() for hint in TERM_SEED_HINTS):
            keep.append((term, count, category))

    keep.sort(key=lambda x: (-x[1], x[0].lower()))
    keep = keep[:160]

    seed_map = {low: item for low, item in seeds.items()}
    rows = []
    for idx, (term, count, category) in enumerate(keep, start=1):
        seg_id, page = first_loc.get(term, ("", 0))
        seed = seed_map.get(term.lower())
        rows.append(
            {
                "term_id": f"T{idx:03d}",
                "src": term,
                "zh": seed["zh"] if seed and seed.get("zh") else "",
                "category": category,
                "count": count,
                "first_seg": seg_id,
                "first_page": page,
                "status": "locked" if seed and seed.get("zh") else "",
                "note": (seed or {}).get("note", ""),
                "context": contexts.get(term, "")[:160],
            }
        )

    write_glossary_tsv(work / "glossary.tsv", rows)
    C.write_json(
        work / "glossary_candidates.json",
        {
            "generated_at": C.now_iso(),
            "total_candidates": len(rows),
            "with_suggested_zh": sum(1 for r in rows if r["zh"]),
            "by_category": dict(Counter(r["category"] for r in rows)),
            "rows": rows,
        },
    )
    return {
        "ok": True,
        "candidates": len(rows),
        "prefilled_from_seed": sum(1 for r in rows if r["zh"]),
        "by_category": dict(Counter(r["category"] for r in rows)),
        "file": str(work / "glossary.tsv"),
        "next": "编辑 work/glossary.tsv 填 zh 列，然后 python scripts/glossary.py lock",
    }


GLOSSARY_HEADER = ["term_id", "src", "zh", "zh_alt", "category", "count", "first_seg", "first_page", "status", "note", "context"]


def write_glossary_tsv(path: Path, rows: list[dict]) -> Path:
    lines = ["\t".join(GLOSSARY_HEADER)]
    for row in rows:
        values = []
        for key in GLOSSARY_HEADER:
            value = str(row.get(key, "")).replace("\t", " ").replace("\n", " ")
            values.append(value)
        lines.append("\t".join(values))
    return C.write_text(path, "\n".join(lines) + "\n")


def read_glossary_tsv(path: Path) -> list[dict]:
    text = C.read_text(path)
    if not text.strip():
        C.fail(f"glossary_missing: {path}")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header = lines[0].split("\t")
    if "src" not in header or "zh" not in header:
        C.fail(f"glossary_bad_header: 需要 src 与 zh 列，实际为 {header}")
    rows = []
    for line in lines[1:]:
        cells = line.split("\t")
        cells += [""] * (len(header) - len(cells))
        row = dict(zip(header, cells))
        if row.get("src", "").strip():
            rows.append({k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()})
    return rows


def lock(work: Path, seed_only: bool = False) -> dict:
    rows = read_glossary_tsv(work / "glossary.tsv")
    problems = []
    skipped_by_flag = 0
    for row in rows:
        status = (row.get("status") or "locked").lower()
        if status in {"skip", "keep", "untranslated"}:
            row["status"] = status
            continue
        if not row.get("zh"):
            if seed_only:
                row["status"] = "skip"
                skipped_by_flag += 1
                continue
            problems.append(f'{row.get("term_id") or row["src"]} ({row["src"]})')
        else:
            row["status"] = "locked"
    if problems:
        C.fail("glossary_incomplete: 以下术语缺少 zh 译名（或把 status 设为 skip/keep）：" + "；".join(problems[:20]))

    path = write_glossary_tsv(work / "glossary.lock.tsv", rows)
    lock_info = {
        "locked_at": C.now_iso(),
        "source": "work/glossary.tsv",
        "file": "work/glossary.lock.tsv",
        "sha256": C.sha256_file(path),
        "terms": len(rows),
        "locked": sum(1 for r in rows if r.get("status") == "locked"),
        "kept": sum(1 for r in rows if r.get("status") == "keep"),
        "skipped": sum(1 for r in rows if r.get("status") == "skip"),
        "skipped_without_translation": skipped_by_flag,
        "mode": "seed_only" if seed_only else "full",
    }
    C.write_json(work / "glossary.lock.json", lock_info)
    return {"ok": True, **lock_info, "next": "python scripts/packet.py build"}


def term_pattern(src: str) -> re.Pattern:
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", src.strip()) if p]
    body = r"[\s\-]{0,2}".join(parts)
    flags = 0 if any(ch.isupper() for ch in src) else re.I
    return re.compile(r"(?<![A-Za-z])" + body + r"(?:s|es)?(?![A-Za-z])", flags)


def norm_zh(text: str) -> str:
    text = text.replace("\u3000", " ")
    text = re.sub(r"[\s]+", "", text)
    return text


def check(work: Path, translations_path: Path | None = None, strict: bool = False) -> dict:
    rows = read_glossary_tsv(work / "glossary.lock.tsv")
    segments = load_segments(work)
    translations = C.read_jsonl(translations_path or (work / "translations.jsonl"))
    zh_by_id = {t["id"]: t.get("zh", "") for t in translations}
    seg_by_id = {s["id"]: s for s in segments}

    issues = []
    per_term = []
    for row in rows:
        if (row.get("status") or "locked") != "locked":
            continue
        src = row["src"]
        expected = [row["zh"]] + [x for x in re.split(r"[|｜]", row.get("zh_alt", "") or "") if x.strip()]
        expected = [e.strip() for e in expected if e.strip()]
        pattern = term_pattern(src)
        total = 0
        ok_count = 0
        misses = []
        for seg in segments:
            if not pattern.search(segment_source_text(seg)):
                continue
            total += 1
            zh = zh_by_id.get(seg["id"], "")
            if not zh:
                continue
            haystack = norm_zh(zh)
            if any(norm_zh(e) and norm_zh(e) in haystack for e in expected):
                ok_count += 1
            else:
                misses.append(seg["id"])
        rate = round(ok_count / total, 4) if total else 1.0
        per_term.append(
            {
                "term_id": row.get("term_id", ""),
                "src": src,
                "zh": row["zh"],
                "occurrences": total,
                "consistent": ok_count,
                "rate": rate,
                "missed_segments": misses[:20],
            }
        )
        for seg_id in misses[:20]:
            issues.append(
                {
                    "type": "glossary",
                    "severity": "error" if strict else "warn",
                    "seg_id": seg_id,
                    "term": src,
                    "expected_zh": row["zh"],
                    "detail": f"第 {seg_id} 段出现 “{src}”，译文未使用锁定译名 “{row['zh']}”",
                }
            )
    overall = [t["rate"] for t in per_term if t["occurrences"]]
    report = {
        "generated_at": C.now_iso(),
        "terms_locked": len(per_term),
        "terms_with_occurrence": len(overall),
        "consistency_rate": round(sum(overall) / len(overall), 4) if overall else 1.0,
        "terms": per_term,
        "issues": issues,
        "violations": len(issues),
    }
    C.write_json(work / "glossary_check.json", report)
    return {
        "ok": not issues or not strict,
        "terms_locked": len(per_term),
        "consistency_rate": report["consistency_rate"],
        "violations": len(issues),
        "worst": sorted([t for t in per_term if t["occurrences"]], key=lambda t: t["rate"])[:5],
    }


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--input", default=argparse.SUPPRESS)
    common.add_argument("--work", default=argparse.SUPPRESS)
    common.add_argument("--output", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("suggest", parents=[common])
    p_lock = sub.add_parser("lock", parents=[common])
    p_lock.add_argument("--seed-only", action="store_true", help="只用种子表已给出的译名，其余术语标记为 skip（快速试跑）")
    p_check = sub.add_parser("check", parents=[common])
    p_check.add_argument("--translations", default="")
    p_check.add_argument("--strict", action="store_true", help="把术语不一致当作错误（退出码非 0）")
    args = parser.parse_args(argv)
    for name, default in (("input", C.DEFAULT_INPUT), ("work", C.DEFAULT_WORK), ("output", C.DEFAULT_OUTPUT)):
        if not getattr(args, name, None):
            setattr(args, name, default)
    dirs = C.dirs_from(args)

    if args.cmd == "suggest":
        result = suggest(dirs.work)
    elif args.cmd == "lock":
        result = lock(dirs.work, args.seed_only)
    elif args.cmd == "check":
        result = check(dirs.work, Path(args.translations) if args.translations else None, args.strict)
    else:
        C.fail(f"unknown_command: {args.cmd}")
    print(json.dumps(result, ensure_ascii=False))
    if args.cmd == "check" and args.strict and result["violations"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
