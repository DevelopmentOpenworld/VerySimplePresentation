"""Item 34 of the plan (25.09.2026): the content package on input — materials of the user read into paragraphs of text with
their provenance, fonts for measurement and preview, and the text of the brief built from them.

The organizers (decision 17) describe a preparation stage without a time limit: the service takes a content package of the
company (brand book, presentations, logos, fonts, wordings) and, for a talk, the materials of the talk (a repository, the
documentation, the story of the team) and limits such as 7 minutes. Decision 22: on the interim review they upload three
unknown templates and a content package. This module reads what the package holds with own code only (decision 1: no
office suite on the server): plain text (txt, md, csv, json), Word documents (docx), presentations (pptx, potx) with their
speaker notes, zip archives of those; fonts (ttf, otf) are kept for measurement and preview; pictures are listed. PDF
(owner decision 37, 27.09.2026) is read by pdf.js in Node (apps/generator/pdf_text_worker.mjs, the runtime of the render
check): the worker gives the text items of every page, this module joins them into paragraphs; a PDF without a text layer
(a scan) or with a password is named, not read. Nothing is executed and nothing is written to paths from the package.
"""
import base64
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import posixpath
import re
import struct
import subprocess
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[3]

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
KINDS = {".txt": "text", ".md": "text", ".markdown": "text", ".csv": "text", ".json": "text", ".docx": "document",
         ".pptx": "presentation", ".potx": "presentation", ".ttf": "font", ".otf": "font", ".png": "image", ".jpg": "image",
         ".jpeg": "image", ".svg": "image", ".gif": "image", ".pdf": "pdf", ".zip": "archive"}
MAX_FILES, MAX_BYTES, MAX_UNPACKED = 200, 80 * 1024 ** 2, 200 * 1024 ** 2
BRIEF_LIMIT = 20000
# Owner decision 52 (29.09.2026): a request longer than BRIEF_LIMIT (a Habr article of 51 000 signs, the organizers' suggestion
# of item 51) is taken whole, up to REQUEST_MAX signs, as the material REQUEST_MATERIAL, one paragraph a line; what the analyst
# reads of it is chosen over the whole text (brief_text).
REQUEST_MAX = 200000
REQUEST_MATERIAL = "Текст запроса.txt"
SENTENCE = re.compile(r"(?<=[.!?…])\s+(?=[«\"(\[A-ZА-ЯЁ0-9])")
PDF_MAX_PAGES, PDF_TIMEOUT = 300, 120


def _xml(data):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("DTD и сущности XML не поддерживаются")
    return ET.fromstring(data)


def _decode(data):
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


HEADING = re.compile(r"#{1,6}\s")
OWN_LINE = re.compile(r"#{1,6}\s|(?:[-*•+]|\d{1,2}[.)])\s|\|")
RULE = re.compile(r"\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?|={3,}|\*{3,}|_{3,}")
FENCE = "```"
LINK = re.compile(r"!?\[([^\]]*)\]\([^)\s]*\)")
BOLD = re.compile(r"\*\*(.+?)\*\*")
ITALIC = re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])")
CODE = re.compile(r"`([^`]+)`")


def _row(line):
    return " | ".join(" ".join(c.split()) for c in line.strip().strip("|").split("|"))


def _inline(text):
    """Text of a Markdown paragraph without its inline marks: a link or a picture keeps its words, bold, italic with
    asterisks and code keep their text (underscores stay: they are parts of names like VSP_NODE_MODULES)."""
    return CODE.sub(r"\1", ITALIC.sub(r"\1", BOLD.sub(r"\1", LINK.sub(r"\1", text))))


def _paragraphs(text, table=False, markdown=False):
    """Paragraphs of plain text: blocks between empty lines, lines of a block joined. A Markdown heading, list item or table
    row is a paragraph of its own (a README keeps them on adjacent lines); a fenced code block is one paragraph that starts
    with ``` whatever empty lines it holds; a table row, like every line of a CSV file (table=True), is written as cells
    joined by ' | ', the way rows of Word tables are; rule lines are left out. A Markdown file (markdown=True) loses its
    inline marks outside code blocks (ui-3 of the journal: links and bold of a README reached the notes as they were)."""
    text = text.replace("\r\n", "\n")
    if table:
        lines = [l for l in text.split("\n") if l.strip()]
        delimiter = ";" if lines and lines[0].count(";") > lines[0].count(",") else ","
        return [" | ".join(" ".join(c.split()) for c in row) for row in csv.reader(lines, delimiter=delimiter) if any(c.strip() for c in row)]
    out, joined, code = [], False, None
    for line in text.split("\n"):
        line = line.strip()
        if code is not None:
            if line.startswith(FENCE):
                out.append(code)
                code, joined = None, False
            else:
                code += " " + line
            continue
        if line.startswith(FENCE):
            code, joined = line, False
        elif not line:
            joined = False
        elif RULE.fullmatch(line):
            continue
        elif joined and not OWN_LINE.match(line) and not HEADING.match(out[-1]) and not out[-1].startswith("|"):
            out[-1] += " " + line
        else:
            out.append(line)
            joined = True
    if code is not None:
        out.append(code)
    out = [_row(p) if p.startswith("|") else " ".join(p.split()) for p in out]
    return [_inline(p) if markdown and not p.startswith(FENCE) else p for p in out]


def docx_paragraphs(data):
    """Paragraphs of a Word document in order: body paragraphs, table rows as cells joined by ' | '."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        body = _xml(z.read("word/document.xml")).find(W + "body")
    out = []
    for node in body if body is not None else []:
        if node.tag == W + "p":
            text = "".join(t.text or "" for t in node.iter(W + "t")).strip()
            if text:
                out.append({"text": text, "where": f"абзац {len(out) + 1}"})
        elif node.tag == W + "tbl":
            for row in node.iter(W + "tr"):
                cells = [" ".join("".join(t.text or "" for t in c.iter(W + "t")).split()) for c in row.iter(W + "tc")]
                if any(cells):
                    out.append({"text": " | ".join(cells), "where": f"таблица, строка {len(out) + 1}"})
    return out


def pptx_paragraphs(data):
    """Paragraphs of a presentation in slide order, then the speaker notes of each slide."""
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        presentation = _xml(z.read("ppt/presentation.xml"))
        rels = _xml(z.read("ppt/_rels/presentation.xml.rels"))
        targets = {r.get("Id"): posixpath.normpath(posixpath.join("ppt", r.get("Target"))) for r in rels}
        for number, node in enumerate(presentation.iter(P + "sldId"), 1):
            part = targets.get(node.get(R + "id"))
            if part not in names:
                continue
            for p in _xml(z.read(part)).iter(A + "p"):
                text = "".join(t.text or "" for t in p.iter(A + "t")).strip()
                if text:
                    out.append({"text": text, "where": f"слайд {number}"})
            rels_name = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
            if rels_name in names:
                for r in _xml(z.read(rels_name)):
                    if r.get("Type", "").endswith("/notesSlide"):
                        notes = posixpath.normpath(posixpath.join(posixpath.dirname(part), r.get("Target")))
                        if notes in names:
                            for p in _xml(z.read(notes)).iter(A + "p"):
                                text = "".join(t.text or "" for t in p.iter(A + "t")).strip()
                                if text and not re.fullmatch(r"\d+", text):
                                    out.append({"text": text, "where": f"заметки к слайду {number}"})
    return out


BULLET = re.compile(r"(?:[•▪◦‣∙·*–—-]|\d{1,2}[.)])\s")
EDGE = 0.08   # share of the page height at the top and the bottom where running heads and page numbers stand
END = tuple(".!?:;…»\"”)")


def _pdf_lines(page):
    """Lines of a page from the text items of pdf.js in their order: an item after an end of line, or on another
    baseline, starts a line; items apart by more than a quarter of their size get a space between them, by more than
    one and a half sizes (cells of a table row) — " | ", the way rows of Word tables are written."""
    lines, line = [], None
    for item in page["items"]:
        text, size = item["s"], item["h"] or 1
        if line is not None and (line["eol"] or abs(item["y"] - line["y"]) > 0.5 * max(size, line["h"])):
            lines.append(line)
            line = None
        if line is None:
            if text.strip():
                line = {"text": text.rstrip(), "x": item["x"], "y": item["y"], "h": size, "end": item["x"] + item["w"],
                        "eol": item["eol"], "space": text != text.rstrip()}
            continue
        if text.strip():
            # A space pdf.js puts between items is not text of the line: the gap is measured from the last word.
            gap = item["x"] - line["end"]
            if gap > 1.5 * size:
                line["text"] += " | "
            elif gap > 0.25 * size or line["space"] or text[:1].isspace():
                line["text"] += " "
            line["text"] += text.strip()
            line["end"] = max(line["end"], item["x"] + item["w"])
            line["h"] = max(line["h"], size)
            line["space"] = text != text.rstrip()
        elif text:
            line["space"] = True
        line["eol"] = item["eol"]
    if line is not None:
        lines.append(line)
    return [{**l, "text": " ".join(l["text"].split())} for l in lines if l["text"].strip()]


def _join(left, right):
    """Two lines of one paragraph. A soft hyphen at the end of the line is dropped and the word joined back; a hard hyphen
    stays and joins without a space: in Russian it more often ends the first half of a compound («контент-» «пакет»)
    than a word broken by hyphenation, which word processors leave off by default."""
    if len(left) > 1 and left[-1] in "-\u00ad" and left[-2].isalpha() and right[:1].isalpha():
        return (left[:-1] if left[-1] == "\u00ad" else left) + right
    return left + " " + right


def pdf_paragraphs(pages):
    """Paragraphs of a PDF from the pages of pdf_text_worker.mjs: lines of a page join while they follow at the spacing
    of body text in one size; a wider gap, a jump up (a new column or block), another size, a list marker, or a short
    line that ends a sentence before a line in capitals (list items whose markers are not in the text) starts a
    paragraph. Running heads and footers (the same line, digits aside, at the top or bottom of at least half of three or
    more pages) and bare page numbers are left out. Each paragraph says the page it stands on."""
    lines = {page["n"]: _pdf_lines(page) for page in pages}
    heights = {page["n"]: page.get("height") or 0 for page in pages}
    edge = lambda n, l: bool(heights[n]) and (l["y"] > heights[n] * (1 - EDGE) or l["y"] < heights[n] * EDGE)
    key = lambda l: re.sub(r"\d+", "#", l["text"].casefold())
    seen = {}
    for n, page_lines in lines.items():
        for k in {key(l) for l in page_lines if edge(n, l)}:
            seen[k] = seen.get(k, 0) + 1
    running = {k for k, count in seen.items() if len(pages) >= 3 and count >= max(3, len(pages) / 2)}
    out, previous = [], None
    for n, page_lines in lines.items():
        kept = [l for l in page_lines if not (edge(n, l) and (key(l) in running or re.fullmatch(r"[\d\s/–-]+", l["text"])))]
        paragraph, last = None, None
        widest = max((l["end"] - l["x"] for l in kept), default=0)
        for l in kept:
            new = (last is None or BULLET.match(l["text"]) or last["y"] - l["y"] > 1.6 * max(l["h"], last["h"])
                   or l["y"] > last["y"] + 0.5 * l["h"] or abs(l["h"] - last["h"]) > 0.15 * max(l["h"], last["h"])
                   or (last["text"].endswith(END) and last["end"] - last["x"] < 0.8 * widest and l["text"][:1].isupper()))
            if (last is None and previous and previous["page"] == n - 1 and l["text"][:1].islower()
                    and not previous["paragraph"]["text"].endswith(END)
                    and abs(l["h"] - previous["h"]) <= 0.15 * max(l["h"], previous["h"])):
                # A paragraph that goes on over the page break: the first line of the page starts in lower case after a
                # paragraph of the same size that did not end a sentence.
                paragraph = out.pop()
                paragraph["text"] = _join(paragraph["text"], l["text"])
                paragraph["where"] = f"страницы {n - 1}–{n}"
            elif new:
                if paragraph:
                    out.append(paragraph)
                paragraph = {"text": l["text"], "where": f"страница {n}"}
            else:
                paragraph["text"] = _join(paragraph["text"], l["text"])
            last = l
        if paragraph:
            out.append(paragraph)
            previous = {"page": n, "paragraph": paragraph, "h": last["h"]}
        else:
            previous = None
    return [p for p in out if any(c.isalnum() for c in p["text"])]


def pdf_texts(blobs):
    """The answer of pdf_text_worker.mjs for the bytes of several PDF files, one Node process for all of them."""
    from .quality import node_runtime
    node, env = node_runtime()
    payload = {"files": [{"data": base64.b64encode(b).decode()} for b in blobs], "max_pages": PDF_MAX_PAGES}
    process = subprocess.run([node, str(ROOT / "apps/generator/pdf_text_worker.mjs")], input=json.dumps(payload), capture_output=True,
                             text=True, encoding="utf-8", env=env, timeout=PDF_TIMEOUT,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if process.returncode:
        raise ValueError(process.stderr[-600:])
    report = json.loads(process.stdout)
    if len(report.get("outputs", [])) != len(blobs):
        raise ValueError("Неполный ответ чтения PDF")
    return report


def _read_pdfs(pending):
    """Paragraphs and warnings of the PDF materials: pdf-encrypted, pdf-invalid, pdf-no-text (no page has a text layer: a
    scan), pdf-pages-without-text (some pages are pictures only), pdf-pages-over-limit (only the first PDF_MAX_PAGES are
    read), pdf-reader-unavailable (no Node or pdf.js on this machine)."""
    try:
        report = pdf_texts([data for _, data in pending])
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        for item, _ in pending:
            item["warnings"].append("pdf-reader-unavailable")
            item["pdf"] = {"error": str(exc)[-300:]}
        return
    for (item, _), result in zip(pending, report["outputs"]):
        if result.get("error"):
            item["warnings"].append("pdf-encrypted" if result["error"] == "encrypted" else "pdf-invalid")
            item["pdf"] = {"error": result.get("message", "")}
            continue
        pages = result["page_list"]
        empty = [p["n"] for p in pages if not any(i["s"].strip() for i in p["items"])]
        item["paragraphs"] = pdf_paragraphs(pages)
        item["pdf"] = {"pages": result["pages"], "read": result["read"], "without_text": empty, "pdfjs": report["runtime"]["pdfjs"]}
        if not item["paragraphs"]:
            item["warnings"].append("pdf-no-text")
        elif empty:
            item["warnings"].append("pdf-pages-without-text")
        if result["read"] < result["pages"]:
            item["warnings"].append("pdf-pages-over-limit")


def font_family(data):
    """Family and whether embedding is allowed (OS/2 fsType 0) of a static TrueType or OpenType font; None when not one."""
    if data[:4] not in (b"\0\1\0\0", b"OTTO", b"true"):
        return None
    tables = {}
    for i in range(struct.unpack_from(">H", data, 4)[0]):
        tag, _, offset, length = struct.unpack_from(">4sIII", data, 12 + 16 * i)
        tables[tag] = data[offset:offset + length]
    name = tables.get(b"name")
    if not name:
        return None
    _, count, start = struct.unpack_from(">HHH", name)
    found = {}
    for i in range(count):
        platform, encoding, language, key, length, offset = struct.unpack_from(">6H", name, 6 + 12 * i)
        if platform == 3 and key in (1, 2, 16, 17):
            found.setdefault(key, name[start + offset:start + offset + length].decode("utf-16-be", "replace"))
    os2 = tables.get(b"OS/2")
    return {"family": found.get(16) or found.get(1), "style": found.get(17) or found.get(2),
            "embeddable": bool(os2) and struct.unpack_from(">H", os2, 8)[0] == 0, "variable": b"fvar" in tables}


def _files(files, depth=0):
    """(name, bytes) of the package with zip archives opened one level deep; unsafe names are refused."""
    for name, data in files:
        clean = posixpath.normpath(name.replace("\\", "/")).lstrip("/")
        if clean.startswith("..") or not clean or clean.startswith("__MACOSX/"):
            continue
        if clean.lower().endswith(".zip") and depth == 0:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                infos = [i for i in z.infolist() if not i.is_dir()]
                if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_UNPACKED:
                    raise ValueError("Архив контент-пакета слишком большой")
                yield from _files([(posixpath.join(clean[:-4], i.filename), z.read(i)) for i in infos], depth + 1)
        else:
            yield clean, data


def read_package(files):
    """vsp.content-package/1 of a list of (file name, bytes): per material its SHA-256, kind, paragraphs with where
    they stand, and warnings; fonts with their family; the SHA-256 of the whole package (the key of its preparation)."""
    files = list(files)
    if len(files) > MAX_FILES or sum(len(d) for _, d in files) > MAX_BYTES:
        raise ValueError("Контент-пакет слишком большой")
    materials, fonts, pdfs = [], [], []
    for name, data in _files(files):
        suffix = posixpath.splitext(name)[1].lower()
        kind = KINDS.get(suffix, "other")
        item = {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "kind": kind, "bytes": len(data), "paragraphs": [], "warnings": []}
        try:
            if kind == "text":
                table = suffix == ".csv"
                item["paragraphs"] = [{"text": t, "where": f"{'строка' if table else 'абзац'} {n}"}
                                      for n, t in enumerate(_paragraphs(_decode(data), table, suffix in (".md", ".markdown")), 1)]
            elif kind == "document":
                item["paragraphs"] = docx_paragraphs(data)
            elif kind == "presentation":
                item["paragraphs"] = pptx_paragraphs(data)
            elif kind == "font":
                face = font_family(data)
                if face and face["family"] and not face["variable"]:
                    fonts.append({"name": name, "sha256": item["sha256"], **face})
                    item["font"] = face
                else:
                    item["warnings"].append("font-not-read")
            elif kind == "pdf":
                pdfs.append((item, data))
            elif kind == "other":
                item["warnings"].append("type-not-supported")
        except (zipfile.BadZipFile, KeyError, ET.ParseError, struct.error, ValueError) as exc:
            item["warnings"].append("not-read: " + type(exc).__name__)
        materials.append(item)
    if pdfs:
        _read_pdfs(pdfs)
    digest = hashlib.sha256("\n".join(sorted(m["sha256"] + " " + m["name"] for m in materials)).encode("utf-8")).hexdigest()
    return {"schema": "vsp.content-package/1", "sha256": digest, "materials": materials, "fonts": fonts,
            "paragraphs": sum(len(m["paragraphs"]) for m in materials), "chars": sum(len(p["text"]) for m in materials for p in m["paragraphs"])}


def request_material(text):
    """Owner decision 52: a long request as a text material, every non-empty line a paragraph (a text pasted from a page puts
    one line to a paragraph; blocks between empty lines, as a .txt file is read, would join a whole article into a few)."""
    lines = [l.strip() for l in text.replace("\r\n", "\n").split("\n") if l.strip()]
    return REQUEST_MATERIAL, "\n\n".join(lines).encode("utf-8")


def _spread(paragraphs, share):
    """Owner decision 52: the texts taken from paragraphs over the whole material within `share` signs: the first sentence of
    every paragraph, then the second of every paragraph, and so on, in the order of the text (a heading is a paragraph of one
    sentence and comes whole); when the first sentences alone do not fit, those of paragraphs spread evenly over the text."""
    sentences = [SENTENCE.split(p["text"]) for p in paragraphs]
    firsts = sum(len(ss[0]) + 2 for ss in sentences)
    step = max(1, math.ceil(firsts / share)) if share > 0 else len(sentences) + 1
    taken, used, depth, grew = [0] * len(sentences), 0, 0, True
    while grew:
        grew = False
        for i, ss in enumerate(sentences):
            if depth == 0 and i % step or taken[i] != depth or depth >= len(ss):
                continue
            cost = len(ss[depth]) + (2 if depth == 0 else 1)
            if used + cost <= share:
                taken[i] += 1
                used += cost
                grew = True
        depth += 1
    return [" ".join(ss[:k]) for ss, k in zip(sentences, taken) if k], used


def brief_text(package, request="", limit=BRIEF_LIMIT):
    """Text of the brief the analyst reads: the request of the user first, then every material with text under its name,
    whole paragraphs from its start. When the text is longer than the limit, each material keeps a share of the room left
    proportional to its text; the report lists what went in and what was left out, so nothing is dropped silently. Owner
    decision 52 (29.09.2026): a material over its share is read over its whole text (_spread), not from its start only (a Habr
    article of 66 paragraphs gave its first 4, 19 % of the text)."""
    request = (request or "").strip()
    sources = [m for m in package["materials"] if m["paragraphs"]]
    head = request + ("\n\n" if request else "")
    room = max(0, limit - len(head) - sum(len(m["name"]) + 4 for m in sources))
    total = sum(len(p["text"]) + 2 for m in sources for p in m["paragraphs"]) or 1
    parts, report = [head] if head else [], []
    for m in sources:
        share = room if total <= room else int(room * sum(len(p["text"]) + 2 for p in m["paragraphs"]) / total)
        whole = sum(len(p["text"]) + 2 for p in m["paragraphs"])
        taken, used = ([p["text"] for p in m["paragraphs"]], whole) if whole <= share else _spread(m["paragraphs"], share)
        if taken:
            parts.append(f"## {m['name']}\n" + "\n\n".join(taken) + "\n\n")
        chars = sum(len(p["text"]) for p in m["paragraphs"])
        report.append({"name": m["name"], "paragraphs": len(m["paragraphs"]), "taken": len(taken), "chars": chars,
                       "taken_chars": used, "whole": whole <= share, "share": round(min(1, used / max(1, whole)), 3)})
    text = "".join(parts).strip()
    return text[:limit], {"limit": limit, "chars": len(text[:limit]), "materials": report,
                          "left_out": [r for r in report if not r["whole"]]}


def package_fonts(style, package, files):
    """Fonts of the package in the typefaces the sample uses (its main font and the fonts of its layout placeholders) join
    the fonts of the preview, the HTML, the PDF and the measurement, when the sample neither embeds them nor has a pinned
    asset. They are not embedded in the PPTX (the licence of a font of the user is not ours to pass on)."""
    import base64
    wanted = {style.get("font")} | {((p.get("typography") or {}).get("font")) for l in (style.get("carrier") or {}).get("layouts", [])
                                   for p in l.get("placeholders", [])}
    have = {(f["family"], f["weight"]) for f in style["fonts"]}
    data = {}
    for name, raw in _files(files):
        data[hashlib.sha256(raw).hexdigest()] = raw
    added = []
    for face in package["fonts"]:
        weight = "bold" if "bold" in (face.get("style") or "").lower() else "regular"
        if face["family"] not in wanted or (face["family"], weight) in have or face["sha256"] not in data:
            continue
        style["fonts"].append({"family": face["family"], "weight": weight, "sha256": face["sha256"],
                               "data": base64.b64encode(data[face["sha256"]]).decode(), "source_part": "content-package/" + face["name"],
                               "origin": "content-package", "embeddable": face["embeddable"]})
        have.add((face["family"], weight))
        added.append(face["family"])
    if added:
        style["diagnostics"] = [d for d in style["diagnostics"] if not (d.get("code") == "FONT_AVAILABILITY" and style.get("font") in added)]
        style["diagnostics"].append({"level": "warning", "code": "PACKAGE_FONT", "families": sorted(set(added)),
                                     "message": "Шрифт образца взят из контент-пакета для предпросмотра и замера; в PPTX он не встраивается."})
    return style


SOURCE_CHARS = 700


def slide_sources(package, brief, limit=SOURCE_CHARS):
    """Variant b of the text of every slide (owner decision 21, answer 1): per section of the brief, the paragraphs of the
    materials that hold the quotes of its facts, with the paragraph after each, up to `limit` signs; {} without materials.
    The speaker may say what these paragraphs say around their facts; numbers stay those of the facts (notes.check_notes)."""
    if not package:
        return {}
    flat = lambda s: " ".join((s or "").split()).casefold()
    paragraphs = [(m["name"], n, p["text"]) for m in package["materials"] for n, p in enumerate(m["paragraphs"])]
    index = {(name, n): text for name, n, text in paragraphs}
    found = {}
    for section in brief.get("sections", []):
        quotes = [flat((f.get("origin") or {}).get("quote")) for f in section.get("facts", [])]
        quotes = [q for q in quotes if len(q) >= 12]
        taken, used = [], 0
        for name, n, text in paragraphs:
            if not any(q in flat(text) for q in quotes):
                continue
            for key in ((name, n), (name, n + 1)):
                piece = index.get(key)
                if piece and piece not in taken and used + len(piece) <= limit:
                    taken.append(piece)
                    used += len(piece)
        if taken:
            found[section["id"]] = taken
    return found
