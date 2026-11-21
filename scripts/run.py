"""One-command orchestration with resumable state.

    python scripts/run.py all      # preflight -> extract -> segment -> glossary -> packets
    (translate each work/packets/packet_XXXX.md into work/translation/*.out.jsonl)
    python scripts/run.py finish   # merge -> check -> render result.json
    python scripts/run.py status   # what is done, what is left

Every step is idempotent: re-running `all` re-uses nothing it cannot recompute, and
`finish` can be repeated after repairing a paragraph without redoing the extraction.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import common as C

HERE = Path(__file__).resolve().parent


def run_step(script: str, args: list[str], verbose: bool = True) -> tuple[int, dict]:
    cmd = [sys.executable, str(HERE / script)] + args
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    payload: dict = {}
    out = (proc.stdout or "").strip().splitlines()
    if out:
        try:
            payload = json.loads(out[-1])
        except json.JSONDecodeError:
            payload = {"raw": out[-1]}
    if verbose:
        line = json.dumps(payload, ensure_ascii=False)
        print(f"[{script}] exit={proc.returncode} {line[:400]}")
    if proc.returncode != 0 and verbose and proc.stderr.strip():
        print(proc.stderr.strip()[-800:], file=sys.stderr)
    return proc.returncode, payload


def dir_args(dirs: C.Dirs) -> list[str]:
    return ["--input", str(dirs.input), "--work", str(dirs.work), "--output", str(dirs.output)]


def cmd_all(dirs: C.Dirs, budget: int, max_segs: int) -> int:
    base = dir_args(dirs)
    steps = [
        ("preflight.py", base),
        ("extract.py", base),
        ("segment.py", base),
        ("glossary.py", ["suggest"] + base),
    ]
    for script, args in steps:
        code, payload = run_step(script, args)
        if code != 0 and not payload:
            print(json.dumps({"ok": False, "failed_step": script, "exit": code}, ensure_ascii=False))
            return code
        if code not in (0, 1) or payload.get("ok") is False:
            print(json.dumps({"ok": False, "failed_step": script, "payload": payload}, ensure_ascii=False))
            return code if code else 2

    # terminology has to be filled and frozen by a human/agent before batching, so this
    # is a deliberate stop point rather than a failure
    if not (dirs.work / "glossary.lock.tsv").exists():
        print(
            json.dumps(
                {
                    "ok": True,
                    "stage": "term_table_ready",
                    "glossary": str(dirs.work / "glossary.tsv"),
                    "instruction": [
                        "打开 work/glossary.tsv，为每行的 zh 列填写统一译名（不需要保留的术语把 status 设为 skip）",
                        "python scripts/glossary.py lock",
                        "python scripts/packet.py build",
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0

    code, payload = run_step("packet.py", ["build", "--budget", str(budget), "--max-segments", str(max_segs)] + base)
    if code != 0:
        print(json.dumps({"ok": False, "failed_step": "packet.py", "payload": payload}, ensure_ascii=False))
        return code
    index = C.read_json(dirs.work / "packets" / "index.json", {}) or {}
    print(
        json.dumps(
            {
                "ok": True,
                "stage": "ready_to_translate",
                "packets": len(index.get("packets", [])),
                "segments": index.get("segments"),
                "instruction": "逐个翻译 work/packets/packet_*.md，写入 work/translation/<packet_id>.out.jsonl，然后 run.py finish",
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_finish(dirs: C.Dirs, strict: bool) -> int:
    base = dir_args(dirs)
    code, payload = run_step("check.py", base + ["--merge"] + (["--strict"] if strict else []))
    if payload.get("ok") is False and "no_translations" in json.dumps(payload):
        print(json.dumps({"ok": False, "stage": "check", "payload": payload}, ensure_ascii=False))
        return code or 1
    code2, payload2 = run_step("render.py", base)
    ok = payload.get("pass") and payload2.get("ok")
    print(
        json.dumps(
            {
                "ok": bool(ok),
                "stage": "finished",
                "checks": payload,
                "render": payload2,
                "next": None if ok else "按 pending_review.md 修复后重跑 run.py finish",
            },
            ensure_ascii=False,
        )
    )
    return 0 if ok else 1


def cmd_status(dirs: C.Dirs) -> int:
    work = dirs.work
    index = C.read_json(work / "packets" / "index.json", {}) or {}
    translations = C.read_jsonl(work / "translations.jsonl")
    verify = C.read_json(work / "verify_report.json", {}) or {}
    done = 0
    missing = []
    for packet in index.get("packets", []):
        out = work / "translation" / f"{packet['packet_id']}.out.jsonl"
        if out.exists() and C.read_jsonl(out):
            done += 1
        else:
            missing.append(packet["packet_id"])
    state = {
        "input": str(dirs.input),
        "work": str(dirs.work),
        "output": str(dirs.output),
        "steps": {
            "preflight": (work / "preflight.json").exists(),
            "extract": (work / "blocks.jsonl").exists(),
            "segment": (work / "segments.jsonl").exists(),
            "glossary_suggest": (work / "glossary.tsv").exists(),
            "glossary_lock": (work / "glossary.lock.tsv").exists(),
            "packets": bool(index),
            "translations": len(translations),
            "verify": bool(verify),
            "result": (dirs.output / "result.json").exists(),
        },
        "packets": {"total": len(index.get("packets", [])), "done": done, "missing": missing[:20]},
        "rates": verify.get("rates", {}),
        "verdict": verify.get("pass"),
        "next": (
            "run.py finish"
            if not missing and index
            else ("翻译 work/packets/ 下的包" if index else "run.py all")
        ),
    }
    print(json.dumps(state, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--input", default=argparse.SUPPRESS)
    common.add_argument("--work", default=argparse.SUPPRESS)
    common.add_argument("--output", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_all = sub.add_parser("all", parents=[common])
    p_all.add_argument("--budget", type=int, default=4500)
    p_all.add_argument("--max-segments", type=int, default=12)
    p_finish = sub.add_parser("finish", parents=[common])
    p_finish.add_argument("--strict", action="store_true")
    sub.add_parser("status", parents=[common])
    args = parser.parse_args(argv)
    for name, default in (("input", C.DEFAULT_INPUT), ("work", C.DEFAULT_WORK), ("output", C.DEFAULT_OUTPUT)):
        if not getattr(args, name, None):
            setattr(args, name, default)
    dirs = C.dirs_from(args)

    if args.cmd == "all":
        return cmd_all(dirs, args.budget, args.max_segments)
    if args.cmd == "finish":
        return cmd_finish(dirs, args.strict)
    return cmd_status(dirs)


if __name__ == "__main__":
    sys.exit(main())
