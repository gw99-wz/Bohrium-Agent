"""Step 1 - preflight.

Reads /input/case.json and /input/task.md, identifies the real source material, and
measures what the extractor will be able to see: page count, per-page character
density, image coverage, encryption, language mix. Writes work/preflight.json.

Nothing here guesses content. If a page has no text layer the report says so and the
run is marked degraded instead of silently producing an empty translation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import common as C


def load_pdf_lib():
    try:
        import pymupdf  # PyMuPDF >= 1.24
        return pymupdf, "pymupdf"
    except Exception:
        pass
    try:
        import fitz  # older PyMuPDF
        return fitz, "fitz"
    except Exception:
        return None, None


def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    letters = len(re.findall(r"[A-Za-z\u4e00-\u9fff]", text))
    return round(cjk / letters, 4) if letters else 0.0


def guess_pair(ratio: float, task: str) -> dict:
    target_zh = bool(re.search(r"中文|汉语|翻译成中|译为中|Chinese", task, re.I))
    target_en = bool(re.search(r"英文|English|translate into English", task, re.I))
    if target_zh and not target_en:
        target = "zh"
    elif target_en and not target_zh:
        target = "en"
    else:
        target = "zh" if ratio < 0.2 else "en"
    return {"source": "en" if ratio < 0.2 else "zh", "target": target, "cjk_ratio": ratio}


def scan_pdf(path: Path) -> dict:
    doc_lib, backend = load_pdf_lib()
    info = {
        "kind": "pdf",
        "backend": backend,
        "pages": 0,
        "page_text_chars": [],
        "page_images": [],
        "scanned_pages": [],
        "low_text_pages": [],
        "encrypted": False,
        "fonts": [],
    }
    if doc_lib is None:
        # Degraded path: no PDF library at all. Count pages from the raw file and let
        # the agent supply text through the vision/OCR fallback documented in SKILL.md.
        info["backend"] = None
        info["degraded"] = "no_pdf_library"
        return info

    try:
        doc = doc_lib.open(str(path))
    except Exception as exc:  # noqa: BLE001 - report, do not crash the pipeline
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info

    if getattr(doc, "needs_pass", False):
        info["encrypted"] = True
        doc.close()
        return info

    fonts = set()
    info["pages"] = doc.page_count
    for pno in range(doc.page_count):
        page = doc.load_page(pno)
        text = page.get_text("text") or ""
        chars = len(re.sub(r"\s+", "", text))
        images = len(page.get_images(full=True))
        info["page_text_chars"].append(chars)
        info["page_images"].append(images)
        try:
            for font in page.get_fonts(full=True):
                fonts.add(str(font[3]))
        except Exception:
            pass
        if chars < 40 and images > 0:
            info["scanned_pages"].append(pno + 1)
        elif chars < 200:
            info["low_text_pages"].append(pno + 1)
        page = None
    doc.close()
    info["fonts"] = sorted(fonts)[:40]
    total = sum(info["page_text_chars"])
    pages = max(1, info["pages"])
    info["chars_per_page"] = round(total / pages, 1)
    info["text_layer"] = "ok" if info["chars_per_page"] >= 200 else "thin"
    if not info["scanned_pages"] and info["text_layer"] == "ok":
        info["text_layer"] = "ok"
    return info


def scan_docx(path: Path) -> dict:
    import zipfile

    info = {"kind": "docx", "pages": None, "text_layer": "ok", "chars_per_page": None}
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info
    text = re.sub(r"<[^>]+>", "", xml)
    info["chars"] = len(re.sub(r"\s+", "", text))
    info["pages"] = max(1, round(info["chars"] / 2200))
    return info


def scan_text(path: Path) -> dict:
    text = C.read_text(path)
    return {
        "kind": "text",
        "chars": len(re.sub(r"\s+", "", text)),
        "pages": max(1, round(len(text) / 3000)),
        "text_layer": "ok",
    }


def detect_contract(case: dict) -> dict:
    """Describe the output contract without inventing field names."""
    contract = case.get("output_contract")
    fields: list[str] = []
    shape = "absent"
    if isinstance(contract, dict):
        shape = "mapping"
        fields = list(contract.keys())
    elif isinstance(contract, list):
        shape = "list"
        for item in contract:
            if isinstance(item, str):
                fields.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("field") or item.get("key")
                if isinstance(name, str):
                    fields.append(name)
    elif isinstance(contract, str):
        shape = "string"
    return {"shape": shape, "fields": fields, "raw": contract if shape == "string" else None}


def main(argv=None) -> int:
    parser = C.add_dir_args(argparse.ArgumentParser(description=__doc__))
    parser.add_argument("--json", action="store_true", help="print the full report instead of a summary")
    args = parser.parse_args(argv)
    dirs = C.dirs_from(args)

    case_info = C.load_case(dirs)
    material = case_info["material"]
    report = {
        "generated_at": C.now_iso(),
        "input_dir": str(dirs.input),
        "work_dir": str(dirs.work),
        "output_dir": str(dirs.output),
        "case_json": case_info["case_json_path"],
        "task_md": case_info["task_md_path"],
        "case_id": C.case_id_of(case_info),
        "material": material,
        "materials_found": case_info["materials"],
        "material_found_by": case_info["material_found_by"],
        "contract": detect_contract(case_info.get("case") or {}),
        "blocking": [],
        "warnings": [],
    }

    if not material:
        report["blocking"].append("no_material: /input 下没有找到可翻译的原文文件（pdf/docx/md/txt）")
        report["status"] = "blocked"
        C.write_json(dirs.w("preflight.json"), report)
        print(json.dumps(report if args.json else {"status": "blocked", "reason": report["blocking"]}, ensure_ascii=False))
        return 3

    path = Path(material)
    ext = path.suffix.lower()
    if ext == ".pdf":
        report["material_scan"] = scan_pdf(path)
    elif ext == ".docx":
        report["material_scan"] = scan_docx(path)
    else:
        report["material_scan"] = scan_text(path)

    native = report["material_scan"]
    report["material_sha256"] = C.sha256_file(path)
    report["material_bytes"] = path.stat().st_size
    report["task_chars"] = len(case_info["task"])
    if not case_info["task"]:
        report["warnings"].append("no_task_md: 未找到 task.md，行为按 SKILL.md 默认值执行并标注")

    samples = []
    if ext in {".md", ".markdown", ".txt"}:
        samples.append(C.read_text(path)[:4000])
    report["language"] = guess_pair(cjk_ratio(" ".join(samples)), case_info["task"] or "")

    if native.get("error"):
        report["blocking"].append(f"material_unreadable: {native['error']}")
    if native.get("encrypted"):
        report["blocking"].append("pdf_encrypted: 需要密码，无法自动抽取")
    if native.get("kind") == "pdf" and native.get("backend") is None:
        report["warnings"].append("no_pdf_library: 未安装 PyMuPDF 且无 pdftotext，将走降级路径")
        if shutil.which("pdftotext") is None:
            report["blocking"].append("no_pdf_extractor: 既无 PyMuPDF 也无 pdftotext")
    if native.get("scanned_pages"):
        pages = native["scanned_pages"]
        report["warnings"].append(
            f"scanned_pages: 第 {pages[:12]} 页无文字层（共 {len(pages)} 页），需 OCR 或视觉转写"
        )
    elif native.get("low_text_pages"):
        report["warnings"].append(f"low_text_pages: 第 {native['low_text_pages'][:12]} 页文字很少，需人工核对是否漏图注")

    report["status"] = "blocked" if report["blocking"] else ("degraded" if report["warnings"] else "ok")
    report["next"] = "python scripts/extract.py" if report["status"] != "blocked" else "见 blocking 字段"
    C.write_json(dirs.w("preflight.json"), report)
    # keep an untouched copy of the case description: result.json must echo the case's
    # own output_contract, and the reviewer must be able to see what was promised
    if isinstance(case_info.get("raw_case"), (dict, list)):
        C.write_json(dirs.w("case_snapshot.json"), case_info["raw_case"])

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        summary = {
            "status": report["status"],
            "case_id": report["case_id"],
            "material": path.name,
            "pages": native.get("pages"),
            "text_layer": native.get("text_layer"),
            "scanned_pages": len(native.get("scanned_pages") or []),
            "contract_fields": report["contract"]["fields"],
            "warnings": report["warnings"],
            "blocking": report["blocking"],
            "next": report["next"],
        }
        print(json.dumps(summary, ensure_ascii=False))
    return 0 if report["status"] != "blocked" else 3


if __name__ == "__main__":
    sys.exit(main())
