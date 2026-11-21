"""Regenerate examples/output from examples/input.

Kept in the repo so the example is reproducible rather than a hand-copied artifact:
    python examples/build_example.py

The text in examples/input is written for this repo (not taken from any paper), so the
example can be published without reproducing copyrighted material. The translations
below are illustrative only - this directory is NOT an answer to any official case.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
EX = Path(__file__).resolve().parent
CASE = EX / "case"
OUT = EX / "output"
BANNER = (
    "> **示例输出**：本文件由 `examples/build_example.py` 用 `examples/case/input/material.md`"
    "（仓库自写的示例文本）生成，用于说明接口、段号与对齐格式，**不是任何正式案例的答案**。\n\n"
)

TRANSLATIONS = {
    "S0001": ("一份用于演示最小调用流程的样例文本", "标题按陈述句译出"),
    "S0002": ("摘要", "Abstract 作“摘要”"),
    "S0003": (
        "我们测量了厚度为 3 层的二维晶体的光学响应。测得带隙为 1.29 eV，比体材料的数值大 0.6 eV [12]。"
        "在硅衬底上生长的样品中，10 K 下未观察到光致发光（图 2a）。",
        "two-dimensional→二维、band gap→带隙、photoluminescence→光致发光，均为锁定译名；Fig. 2a→图 2a",
    ),
    "S0004": ("结果", "Results 作“结果”"),
    "S0005": (
        "拉曼光谱给出的单层线宽为 2.4 cm-1，并且样品仅在 350 K 以下稳定。与早期工作 [7, 8] 相比，"
        "我们未检测到任何可测量的激子结合能移动。",
        "only below→仅在……以下；in contrast to→与……相比；did not detect→未检测到",
    ),
}


def run(script: str, *args: str) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    payload: dict = {}
    for line in reversed((proc.stdout or "").strip().splitlines()):
        try:
            payload = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    return proc.returncode, payload


def main() -> int:
    base = ["--input", str(CASE / "input"), "--work", str(CASE / "work"), "--output", str(OUT)]
    if (CASE / "work").exists():
        shutil.rmtree(CASE / "work", ignore_errors=True)
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    (CASE / "work").mkdir(parents=True)
    OUT.mkdir(parents=True)

    steps = [
        ("preflight.py", base),
        ("extract.py", base),
        ("segment.py", base),
        ("glossary.py", ["suggest"] + base),
        ("glossary.py", ["lock", "--seed-only"] + base),
        ("packet.py", ["build"] + base),
    ]
    for script, args in steps:
        code, payload = run(script, *args)
        print(f"[{script}] exit={code} {json.dumps(payload, ensure_ascii=False)[:200]}")
        if code not in (0, 1):
            return code

    segments = [json.loads(l) for l in (CASE / "work" / "segments.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    out_dir = CASE / "work" / "translation"
    out_dir.mkdir(exist_ok=True)
    rows = []
    for seg in segments:
        zh, basis = TRANSLATIONS.get(seg["id"], ("【未译】", "示例未覆盖"))
        rows.append({"id": seg["id"], "zh": zh, "basis": basis, "flags": []})
    (out_dir / "packet_0001.out.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )

    for script, args in (("check.py", ["--merge"] + base), ("render.py", base)):
        code, payload = run(script, *args)
        print(f"[{script}] exit={code} {json.dumps(payload, ensure_ascii=False)[:300]}")

    for name in ("bilingual.md", "translation.zh.md", "pending_review.md"):
        path = OUT / name
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if not text.startswith("> **示例输出**"):
                path.write_text(BANNER + text, encoding="utf-8")
    print(f"example written to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
