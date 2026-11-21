"""Step 5 - translation packets.

Long papers do not fit one prompt, and splitting them naively is what produces
dropped paragraphs, repeated paragraphs and drifting terminology. This step cuts the
document at semantic boundaries (never mid-sentence, never mid-segment) and packages
each batch with exactly the context a translator needs:

  * the locked terminology that actually occurs in this batch (not the whole table);
  * the tail of the previous segment and the head of the next one;
  * the section the batch belongs to and the paragraph ids to answer.

Output: work/packets/packet_0001.json (+ .md view), work/packets/index.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import common as C

DEFAULT_BUDGET = 4500
DEFAULT_MAX_SEGS = 12


def load_lock(work: Path) -> list[dict]:
    path = work / "glossary.lock.tsv"
    if not path.exists():
        C.fail("glossary_not_locked: 先运行 scripts/glossary.py suggest 并 lock")
    import glossary as G

    rows = G.read_glossary_tsv(path)
    return [r for r in rows if (r.get("status") or "locked") == "locked"]


def terms_in(text: str, rows: list[dict]) -> list[dict]:
    import glossary as G

    hits = []
    for row in rows:
        if G.term_pattern(row["src"]).search(text):
            hits.append({"src": row["src"], "zh": row["zh"], "zh_alt": row.get("zh_alt", "")})
    return hits


def build_batches(segments: list[dict], budget: int, max_segs: int) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    chars = 0
    for seg in segments:
        size = len(seg["text"])
        new_section = bool(current) and seg["section"] != current[-1]["section"]
        over = chars + size > budget or len(current) >= max_segs
        boundary = new_section and (chars + size > budget * 0.6 or seg["kind"] == "heading")
        if current and (over or boundary):
            batches.append(current)
            current = []
            chars = 0
        current.append(seg)
        chars += size
    if current:
        batches.append(current)
    return batches


def build(work: Path, budget: int, max_segs: int) -> dict:
    segments = C.read_jsonl(work / "segments.jsonl")
    if not segments:
        C.fail("no_segments: 先运行 scripts/segment.py")
    locked = load_lock(work)
    batches = build_batches(segments, budget, max_segs)

    packets_dir = work / "packets"
    for old in packets_dir.glob("packet_*.json"):
        old.unlink()

    index = []
    for idx, batch in enumerate(batches, start=1):
        packet_id = f"packet_{idx:04d}"
        text_all = "\n".join(s["text"] for s in batch)
        first, last = batch[0], batch[-1]
        prev_seg = segments[segments.index(first) - 1] if segments.index(first) > 0 else None
        next_seg = segments[segments.index(last) + 1] if segments.index(last) + 1 < len(segments) else None
        packet = {
            "packet_id": packet_id,
            "sections": sorted({s["section"] for s in batch}, key=str),
            "page_range": [first["page"], last["page_end"]],
            "chars": len(text_all),
            "segments": [
                {
                    "id": s["id"],
                    "kind": s["kind"],
                    "page": s["page"],
                    "section": s["section"],
                    "figure": s.get("figure", ""),
                    "text": s["text"],
                }
                for s in batch
            ],
            "glossary": terms_in(text_all, locked),
            "context": {
                "prev_tail": (prev_seg["text"][-220:] if prev_seg else ""),
                "next_head": (next_seg["text"][:160] if next_seg else ""),
                "note": "prev_tail 只用于理解指代，不要翻译；next_head 只用于判断句末是否延续",
            },
            "output_schema": {
                "file": f"work/translation/{packet_id}.out.jsonl",
                "per_line": {"id": "段落 id，必须与输入一致", "zh": "中文译文", "basis": "可选：译法依据/术语选择理由", "flags": "可选：['uncertain'] 等"},
            },
        }
        C.write_json(packets_dir / f"{packet_id}.json", packet)
        C.write_text(packets_dir / f"{packet_id}.md", render_packet_md(packet))
        index.append(
            {
                "packet_id": packet_id,
                "segments": len(batch),
                "chars": packet["chars"],
                "page_range": packet["page_range"],
                "glossary_terms": len(packet["glossary"]),
                "expected_output": f"work/translation/{packet_id}.out.jsonl",
            }
        )

    C.write_json(
        packets_dir / "index.json",
        {
            "generated_at": C.now_iso(),
            "budget_chars": budget,
            "max_segments": max_segs,
            "segments": len(segments),
            "packets": index,
            "total_chars": sum(p["chars"] for p in index),
        },
    )
    C.write_text(packets_dir / "INSTRUCTIONS.md", instructions(len(index)))
    return {
        "ok": True,
        "packets": len(index),
        "segments": len(segments),
        "total_chars": sum(p["chars"] for p in index),
        "dir": str(packets_dir),
        "next": "逐个翻译 work/packets/packet_*.md，把结果写入 work/translation/<packet_id>.out.jsonl",
    }


def instructions(count: int) -> str:
    return f"""# 翻译包说明

共 {count} 个包。每个包是一个独立的、带上下文的翻译单元。

## 输入
- `packet_XXXX.json`：机器可读（段号、原文、本批术语、上下文）
- `packet_XXXX.md`：同样内容的易读版，供人/Agent 直接读

## 输出
写到 `work/translation/packet_XXXX.out.jsonl`，每行一个 JSON：

```json
{{"id": "S0007", "zh": "……中文译文……", "basis": "术语/句式取舍说明（可选）", "flags": []}}
```

硬性要求：
1. `id` 必须与输入完全一致，不新增、不改号、不跳过；
2. 一段一行，段落内部不要换行（用 `\\n` 会破坏对齐）；
3. 术语列中给出的译名必须原样使用；
4. 出现数字、单位、公式、[编号] 引用、Fig./Table 编号时必须原样保留；
5. 无法确定的地方，仍要给出译文，并在 `flags` 里写 `["uncertain"]` 与 `basis` 说明；
6. 不要在译文里添加原文没有的解释、扩写或删减。
"""


def render_packet_md(packet: dict) -> str:
    lines = [
        f"# {packet['packet_id']}",
        "",
        f"- 章节：{' / '.join(packet['sections'])}",
        f"- 页码：p{packet['page_range'][0]}–p{packet['page_range'][1]}",
        f"- 字符数：{packet['chars']}",
        f"- 段数：{len(packet['segments'])}",
        "",
        "## 本批锁定术语（必须原样使用）",
        "",
    ]
    if packet["glossary"]:
        lines.append("| 英文 | 中文 |")
        lines.append("| --- | --- |")
        for term in packet["glossary"]:
            alt = f"（或 {term['zh_alt']}）" if term.get("zh_alt") else ""
            lines.append(f"| {term['src']} | {term['zh']}{alt} |")
    else:
        lines.append("（本批无需锁定术语）")
    lines += ["", "## 上文（仅供理解指代，不要翻译）", "", f"> {packet['context']['prev_tail'] or '（无）'}", ""]
    lines += ["## 待译段落", ""]
    for seg in packet["segments"]:
        tag = f"{seg['id']} · p{seg['page']} · {seg['kind']}"
        if seg.get("figure"):
            tag += f" · {seg['figure']}"
        lines.append(f"### {tag}")
        lines.append("")
        lines.append(seg["text"])
        lines.append("")
    lines += ["## 下文（仅供判断句末是否延续，不要翻译）", "", f"> {packet['context']['next_head'] or '（无）'}", ""]
    lines += [
        "## 输出",
        "",
        f"写入 `work/translation/{packet['packet_id']}.out.jsonl`，每行 `{{\"id\", \"zh\"}}`。",
        "",
    ]
    return "\n".join(lines)


def status(work: Path) -> dict:
    index_path = work / "packets" / "index.json"
    index = C.read_json(index_path, None)
    if not index:
        C.fail("no_packets: 先运行 scripts/packet.py build")
    done, missing, partial = [], [], []
    for packet in index["packets"]:
        out_path = work / "translation" / f"{packet['packet_id']}.out.jsonl"
        spec = C.read_json(work / "packets" / f"{packet['packet_id']}.json", {}) or {}
        want = {s["id"] for s in spec.get("segments", [])}
        rows = C.read_jsonl(out_path)
        got = {r.get("id") for r in rows if r.get("zh")}
        if not rows:
            missing.append(packet["packet_id"])
        elif want - got:
            partial.append({"packet_id": packet["packet_id"], "missing_ids": sorted(want - got)[:10]})
        else:
            done.append(packet["packet_id"])
    return {
        "ok": True,
        "packets": len(index["packets"]),
        "done": len(done),
        "partial": partial,
        "missing": missing,
        "next": "python scripts/check.py --merge" if not missing and not partial else "继续翻译缺失的包",
    }


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--input", default=argparse.SUPPRESS)
    common.add_argument("--work", default=argparse.SUPPRESS)
    common.add_argument("--output", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build", parents=[common])
    p_build.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    p_build.add_argument("--max-segments", type=int, default=DEFAULT_MAX_SEGS)
    sub.add_parser("status", parents=[common])
    args = parser.parse_args(argv)
    for name, default in (("input", C.DEFAULT_INPUT), ("work", C.DEFAULT_WORK), ("output", C.DEFAULT_OUTPUT)):
        if not getattr(args, name, None):
            setattr(args, name, default)
    dirs = C.dirs_from(args)

    if args.cmd == "build":
        result = build(dirs.work, args.budget, args.max_segments)
    else:
        result = status(dirs.work)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
