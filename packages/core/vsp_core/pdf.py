"""PDF export (stage H2 of docs/SOLUTION_PLAN_2026-09-21.md; docs/PDF_EXPORT.md).

The PDF of a revision is its self-contained deck.html printed by headless Chromium (apps/generator/pdf_worker.mjs, the
runtime of the mandatory render check), one page of the slide size per slide, text as text, charts as vectors, the
fonts of the reference embedded. It shows what the preview shows: decor that only PowerPoint draws from a layout or
master (PREVIEW_INCOMPLETE_DECOR) is missing there too. inspect_pdf checks the file with the standard library: page
count equals slide count, every font embedded, no page without text or picture, the typeface of the template drawn.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import zlib

from .quality import node_runtime

ROOT = Path(__file__).resolve().parents[3]
OBJECT = re.compile(rb"(\d+)\s+0\s+obj\b(.*?)\bendobj", re.S)
STREAM = re.compile(rb"stream\r?\n", re.S)
LENGTH = re.compile(rb"/Length\s+(\d+)(?!\s+\d+\s+R)")
PAGE = re.compile(rb"/Type\s*/Page\b(?!s)")
CONTENTS = re.compile(rb"/Contents\s*(\[[^\]]*\]|\d+\s+0\s+R)")
NAME = rb"/([^\s/<>\[\]()]+)"
# Operators that draw text (Tj, TJ) or a picture or form (Do); a page with neither shows only a background.
DRAWN = re.compile(rb"(?<![A-Za-z])(?:Tj|TJ|Do)(?![A-Za-z])")


def export_pdfs(jobs, timeout=300):
    """Print every (html, pdf) pair in one Chromium session; returns the worker report of each job."""
    node, env = node_runtime()
    payload = {"jobs": [{"html": str(html), "pdf": str(pdf)} for html, pdf in jobs]}
    try:
        process = subprocess.run([node, str(ROOT / "apps/generator/pdf_worker.mjs")], input=json.dumps(payload, ensure_ascii=False),
                                 capture_output=True, text=True, encoding="utf-8", env=env, timeout=timeout,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Экспорт PDF не выполнен: " + str(exc)) from exc
    if process.returncode:
        raise ValueError("Экспорт PDF не выполнен: " + process.stderr[-1800:])
    report = json.loads(process.stdout)
    if len(report.get("outputs", [])) != len(jobs):
        raise ValueError("Неполный ответ экспорта PDF")
    return [{**o, "runtime": report["runtime"]} for o in report["outputs"]]


def inspect_pdf(data, slides, family):
    """Facts of a printed PDF and its issues (codes of audit/checks.json, source "export")."""
    objects = {int(n): body for n, body in OBJECT.findall(data)}
    pages = [body for body in objects.values() if PAGE.search(body)]
    fonts = sorted({m.decode("latin-1") for m in re.findall(rb"/BaseFont\s*" + NAME, data)})
    # A Type3 font carries its glyphs as drawing procedures inside the PDF; its descriptor has no font file by design.
    # Chromium prints a variable font this way (Arimo[wght], the substitute of Arial on template C, K3 of 28.09.2026:
    # a false PDF_FONT_NOT_EMBEDDED on every page with such text).
    type3 = {int(m) for body in objects.values() if re.search(rb"/Subtype\s*/Type3", body)
             for m in re.findall(rb"/FontDescriptor\s+(\d+)\s+0\s+R", body)}
    descriptors = [body for number, body in objects.items() if re.search(rb"/Type\s*/FontDescriptor", body) and number not in type3]
    not_embedded = sorted((re.search(rb"/FontName\s*" + NAME, d).group(1).decode("latin-1") if re.search(rb"/FontName\s*" + NAME, d) else "?")
                          for d in descriptors if not re.search(rb"/FontFile[23]?\s", d))
    empty = []
    for number, page in enumerate(pages, 1):
        found = CONTENTS.search(page)
        content = b""
        for ref in (re.findall(rb"(\d+)\s+0\s+R", found.group(1)) if found else []):
            body = objects.get(int(ref), b"")
            stream = STREAM.search(body)
            if stream:
                # The data is /Length bytes after "stream" and its line end; a regular expression up to "endstream" cut the
                # last byte of a compressed stream once (a false empty page, docs/PDF_EXPORT.md, journal item 4).
                length = LENGTH.search(body[:stream.start()])
                raw = body[stream.end():stream.end() + int(length.group(1))] if length else body[stream.end():body.rfind(b"endstream")]
                try:
                    content += zlib.decompressobj().decompress(raw) if b"/FlateDecode" in body[:stream.start()] else raw
                except zlib.error:
                    content += raw
        if not DRAWN.search(content):
            empty.append(number)
    issues = []
    if not data.startswith(b"%PDF-"):
        issues.append({"code": "PDF_INVALID"})
    if len(pages) != slides:
        issues.append({"code": "PDF_PAGE_COUNT", "pages": len(pages), "slides": slides})
    if not_embedded:
        issues.append({"code": "PDF_FONT_NOT_EMBEDDED", "fonts": not_embedded})
    if empty:
        issues.append({"code": "PDF_EMPTY_PAGE", "pages": empty})
    # PostScript names drop spaces ("Open Sans" -> "OpenSans-Regular"); a subset prefix ("AAAAAA+") comes first.
    key = re.sub(r"[\s-]", "", family or "").lower()
    if key and not any(key in re.sub(r"[\s-]", "", f.split("+")[-1]).lower() for f in fonts):
        issues.append({"code": "PDF_FONT_SUBSTITUTED", "family": family, "fonts": fonts})
    return {"pages": len(pages), "fonts": fonts, "fonts_not_embedded": not_embedded, "empty_pages": empty, "issues": issues}
