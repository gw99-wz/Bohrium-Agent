"""Shared helpers: path resolution, JSON IO, case loading, material discovery.

All scripts are stdlib-only by design; PyMuPDF is an optional accelerator that is
imported lazily by extract.py. Every path is resolved from CLI flags or environment
variables so the same code runs under the evaluator contract
(/input -> /app/work -> /app/output) and under a local simulation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_INPUT = os.environ.get("PAPER_TRANSLATE_INPUT", "/input")
DEFAULT_WORK = os.environ.get("PAPER_TRANSLATE_WORK", "/app/work")
DEFAULT_OUTPUT = os.environ.get("PAPER_TRANSLATE_OUTPUT", "/app/output")

MATERIAL_EXTS = {".pdf", ".docx", ".md", ".markdown", ".txt", ".tex", ".html", ".htm"}

# ---------------------------------------------------------------- path handling


def add_dir_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--input", default=DEFAULT_INPUT, help="directory holding case.json/task.md and the source material")
    parser.add_argument("--work", default=DEFAULT_WORK, help="scratch directory (intermediate artifacts)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="directory for final, reviewer-facing artifacts")
    return parser


class Dirs:
    def __init__(self, input_dir, work_dir, output_dir):
        self.input = Path(input_dir).expanduser().resolve()
        self.work = Path(work_dir).expanduser().resolve()
        self.output = Path(output_dir).expanduser().resolve()

    def ensure(self) -> "Dirs":
        self.work.mkdir(parents=True, exist_ok=True)
        self.output.mkdir(parents=True, exist_ok=True)
        return self

    def w(self, *parts) -> Path:
        p = self.work.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def o(self, *parts) -> Path:
        p = self.output.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def rel(self, path) -> str:
        """Path as reported to a reviewer: relative to output when inside it."""
        path = Path(path).resolve()
        for base in (self.output, self.work, self.input):
            try:
                return str(path.relative_to(base))
            except ValueError:
                continue
        return str(path)

    def __repr__(self) -> str:
        return f"Dirs(input={self.input}, work={self.work}, output={self.output})"


def dirs_from(args) -> Dirs:
    return Dirs(args.input, args.work, args.output).ensure()


# ---------------------------------------------------------------------- JSON IO


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def write_json(path, obj) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
    tmp.replace(path)
    return path


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def write_jsonl(path, rows) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=False) + "\n")
    tmp.replace(path)
    return path


def write_text(path, text) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    tmp.replace(path)
    return path


def read_text(path, default=""):
    path = Path(path)
    if not path.exists():
        return default
    return path.read_text(encoding="utf-8-sig", errors="replace")


# -------------------------------------------------------------------- case load


def _walk_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _walk_strings(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _walk_strings(value)


def find_case_files(input_dir: Path):
    """Locate case.json / task.md, tolerating alternative spellings."""
    input_dir = Path(input_dir)
    case_json = input_dir / "case.json"
    task_md = input_dir / "task.md"
    if not task_md.exists():
        for cand in ("TASK.md", "task.markdown", "prompt.md"):
            alt = input_dir / cand
            if alt.exists():
                task_md = alt
                break
    return case_json, task_md


def discover_materials(input_dir: Path, case_json: dict | None):
    """Find the source documents that belong to this case.

    Order of evidence: paths named inside case.json, then any material file in the
    input tree. Returns (chosen_path, all_candidates, how_it_was_found).
    """
    input_dir = Path(input_dir)
    resolved: list[tuple[Path, str]] = []
    if isinstance(case_json, dict):
        for s in _walk_strings(case_json):
            cand = Path(s)
            candidates = [cand] if cand.is_absolute() else [input_dir / cand, input_dir / cand.name]
            for c in candidates:
                try:
                    if c.is_file() and c.suffix.lower() in MATERIAL_EXTS:
                        resolved.append((c.resolve(), "case.json"))
                        break
                except OSError:
                    continue

    seen = {p for p, _ in resolved}
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in MATERIAL_EXTS:
            continue
        if path.name in {"case.json", "task.md", "GITHUB_URL.txt"}:
            continue
        if path.resolve() in seen:
            continue
        resolved.append((path.resolve(), "input_scan"))
        seen.add(path.resolve())

    if not resolved:
        return None, [], "none"
    # Prefer a PDF: it is the material the runtime contract actually hands over.
    def score(item):
        path, how = item
        return (0 if path.suffix.lower() == ".pdf" else 1, -path.stat().st_size, how != "case.json")

    resolved.sort(key=score)
    return resolved[0][0], [str(p) for p, _ in resolved], resolved[0][1]


def load_case(dirs: Dirs):
    case_json_path, task_md_path = find_case_files(dirs.input)
    case_json = read_json(case_json_path, None)
    task_md = read_text(task_md_path, "")
    material, all_materials, found_by = discover_materials(dirs.input, case_json)
    return {
        "case_json_path": str(case_json_path) if case_json_path.exists() else None,
        "task_md_path": str(task_md_path) if task_md_path.exists() else None,
        "case": case_json if isinstance(case_json, dict) else {},
        "raw_case": case_json,
        "task": task_md,
        "material": str(material) if material else None,
        "materials": all_materials,
        "material_found_by": found_by,
    }


def case_id_of(case_info) -> str:
    case = case_info.get("case") or {}
    for key in ("case_id", "id", "caseId", "case", "slug", "name"):
        value = case.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    material = case_info.get("material")
    if material:
        stem = Path(material).stem
        return re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]+", "-", stem)[:80]
    return "unknown-case"


# ---------------------------------------------------------------- misc utilities


def normalize_space(text: str) -> str:
    text = text.replace("\u00ad", "")
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    return text.strip()


def unify_punct(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def strip_front_matter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4 :].lstrip("\n")
    return text


def parse_id_ranges(spec: str) -> set[str]:
    """Parse 'S0001-S0012,S0020' into a set of segment ids."""
    ids: set[str] = set()
    for part in re.split(r"[,\s;，；]+", spec or ""):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"S(\d{1,4})\s*[-–~]\s*S?(\d{1,4})", part, re.I)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            lo, hi = min(start, end), max(start, end)
            ids.update(f"S{n:04d}" for n in range(lo, hi + 1))
            continue
        m = re.fullmatch(r"S?(\d{1,4})", part, re.I)
        if m:
            ids.add(f"S{int(m.group(1)):04d}")
    return ids


def fail(message: str, code: int = 2):
    """Machine-readable failure: reason on stdout, exit code for the caller."""
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
    sys.exit(code)


def ok(payload: dict):
    payload = {"ok": True, **payload}
    print(json.dumps(payload, ensure_ascii=False))
    return payload
