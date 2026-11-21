"""Regression tests for the paper-translate pipeline.

Run with:  python tests/test_pipeline.py
Each test drives the real command-line entry points in a temporary directory, so a
pass here means the shipped scripts actually work end to end, not that helpers do.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

CASES = [
    ("mini_happy", "两份都翻译正确", True),
    ("mini_partial", "漏一段 + 改一个数字", False),
]


def run(script: str, *args: str) -> tuple[int, dict]:
    cmd = [sys.executable, str(SCRIPTS / script), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    payload: dict = {}
    for line in reversed((proc.stdout or "").strip().splitlines()):
        try:
            payload = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    return proc.returncode, payload


def dirs(base: Path) -> list[str]:
    return ["--input", str(base / "input"), "--work", str(base / "work"), "--output", str(base / "output")]


def prepare(base: Path, case_id: str) -> None:
    (base / "input").mkdir(parents=True)
    (base / "work").mkdir()
    (base / "output").mkdir()
    shutil.copy(FIXTURES / "mini_en.md", base / "input" / "material.md")
    (base / "input" / "case.json").write_text(
        json.dumps(
            {
                "case_id": case_id,
                "source": "material.md",
                "output_contract": {"translation": "全文译文", "glossary": "术语表", "status": "状态", "mystery_field": "未知字段"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (base / "input" / "task.md").write_text("# 任务\n把文本翻译成中文，术语统一。\n", encoding="utf-8")


def lock_small_glossary(base: Path) -> None:
    rows = [
        ["T001", "two-dimensional", "二维", "", "phrase", "3", "S0003", "1", "locked", "", ""],
        ["T002", "band gap", "带隙", "", "phrase", "3", "S0003", "1", "locked", "", ""],
        ["T003", "photoluminescence", "光致发光", "", "phrase", "3", "S0003", "1", "locked", "", ""],
    ]
    header = ["term_id", "src", "zh", "zh_alt", "category", "count", "first_seg", "first_page", "status", "note", "context"]
    text = "\t".join(header) + "\n" + "\n".join("\t".join(r) for r in rows) + "\n"
    (base / "work" / "glossary.tsv").write_text(text, encoding="utf-8")


def translate(base: Path, segments: list[dict], mode: str) -> None:
    good = {
        0: "一份用于流水线的最小样例",
        1: "摘要",
        2: "我们测量了厚度为 3 层的二维晶体的光学响应。测得带隙为 1.29 eV，比体材料的数值大 0.6 eV [12]。在硅衬底上生长的样品中，10 K 下未观察到光致发光（图 2a）。",
        3: "结果",
        4: "拉曼光谱给出的单层线宽为 2.4 cm-1，并且样品仅在 350 K 以下稳定。与早期工作 [7, 8] 相比，我们未检测到任何可测量的激子结合能移动。",
    }
    rows = []
    for idx, seg in enumerate(segments):
        if mode == "mini_partial" and idx == 4:
            continue  # deliberately drop the last paragraph
        zh = good.get(idx, "")
        if mode == "mini_partial" and idx == 2:
            zh = zh.replace("1.29 eV", "1.30 eV")  # deliberately damage a number
        rows.append({"id": seg["id"], "zh": zh, "basis": "回归测试样例", "flags": []})
    out_dir = base / "work" / "translation"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "packet_0001.out.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )


def test_case(name: str, expect_pass: bool) -> bool:
    # keep scratch space inside the repo: the Windows sandbox blocks writes to the
    # default temp directory, and a test that cannot run there is not a test
    tmp_root = Path(__file__).resolve().parent / "_tmp"
    tmp_root.mkdir(exist_ok=True)
    base = tmp_root / name
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    try:
        prepare(base, name)
        code, pre = run("preflight.py", *dirs(base))
        assert pre.get("status") in {"ok", "degraded"}, pre
        code, ext = run("extract.py", *dirs(base))
        assert ext.get("ok"), ext
        code, seg = run("segment.py", *dirs(base))
        assert seg.get("ok"), seg
        segments = [json.loads(l) for l in (base / "work" / "segments.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(segments) >= 5, f"expected >=5 segments, got {len(segments)}"
        assert seg["char_ratio"] >= 0.98, seg

        lock_small_glossary(base)
        code, locked = run("glossary.py", "lock", *dirs(base))
        assert locked.get("ok"), locked
        code, pkt = run("packet.py", "build", *dirs(base))
        assert pkt.get("ok"), pkt
        assert pkt["packets"] >= 1

        translate(base, segments, name)
        code, chk = run("check.py", "--merge", *dirs(base))
        code2, ren = run("render.py", *dirs(base))
        result = json.loads((base / "output" / "result.json").read_text(encoding="utf-8"))

        ok = True
        def expect(cond: bool, msg: str) -> None:
            nonlocal ok
            if not cond:
                ok = False
                print(f"   FAIL: {msg}")

        expect(chk.get("pass") is expect_pass, f"check pass expected {expect_pass}, got {chk.get('pass')} (issues={chk.get('issue_counts')})")
        expect((base / "output" / "bilingual.md").exists(), "bilingual.md missing")
        expect((base / "output" / "translation.zh.md").exists(), "translation.zh.md missing")
        expect("mystery_field" in result.get("output_contract", {}), "unknown contract field not echoed")
        expect(result["output_contract"].get("mystery_field") is None, "unknown contract field must be null, not invented")
        expect(result["output_contract"].get("translation"), "translation contract field is empty")
        expect(result["output_contract"].get("glossary"), "glossary contract field is empty")
        expect(result.get("case_id") == name, "case_id not preserved")
        expect(result["status"] == ("success" if expect_pass else "partial"), f"status={result['status']}")
        if not expect_pass:
            types = set(chk.get("issue_counts", {}))
            expect("missing" in types, f"dropped paragraph not reported: {types}")
            expect("number_missing" in types, f"altered number not reported: {types}")
            expect(result.get("uncertainty"), "partial result must carry uncertainty notes")
        return ok
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_reading_order() -> bool:
    sys.path.insert(0, str(SCRIPTS))
    import extract

    left = {"bbox": [50, 100, 250, 110], "text": "L1"}
    left2 = {"bbox": [50, 120, 250, 130], "text": "L2"}
    right = {"bbox": [300, 100, 500, 110], "text": "R1"}
    right2 = {"bbox": [300, 120, 500, 130], "text": "R2"}
    title = {"bbox": [50, 50, 500, 80], "text": "T"}
    ordered = extract.reading_order([right2, left2, title, right, left], 550)
    texts = [b["text"] for b in ordered]
    ok = texts == ["T", "L1", "L2", "R1", "R2"]
    print(f"   reading order: {texts} -> {'ok' if ok else 'WRONG'}")
    return ok


def main() -> int:
    failures = 0
    print("[test] two-column reading order")
    if not test_reading_order():
        failures += 1
    for name, desc, expect_pass in CASES:
        print(f"[test] {name}: {desc}")
        try:
            if not test_case(name, expect_pass):
                failures += 1
        except AssertionError as exc:
            failures += 1
            print(f"   FAIL: {exc}")
    print("=" * 40)
    print("ALL TESTS PASSED" if failures == 0 else f"{failures} TEST CASE(S) FAILED")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
