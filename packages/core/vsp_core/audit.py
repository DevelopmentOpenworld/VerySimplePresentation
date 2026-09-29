"""Deterministic checks of Appendix 1 of the case over the document model (stage C2; registry audit/checks.json).

template_tokens(style) keeps in the document what the reference allows: typefaces, the size scale, colours, layouts.
appendix_findings(doc) returns findings with a severity (error, warning, exception for the role of a slide) and the
fixer the registry names; they do not stop the output, the user chooses which to fix (C5, C6). package_findings checks
the finished PPTX for text the model does not know: placeholders and sample text left after cloning.
"""
import io
import json
from pathlib import Path
import re
import zipfile
import xml.etree.ElementTree as ET

from .patterns import blend, contains, contrast
from .placement import estimate_lines
from .reference import slide_tone

PT = .75                      # pt per px
SIZE_TOLERANCE = .5           # pt
COLOUR_TOLERANCE = 6          # largest RGB channel difference
ASPECT_TOLERANCE = .02
MAX_ITEMS, MAX_WORDS = 6, 15
MAX_TABLE_ROWS, MAX_TABLE_COLUMNS = 7, 5
SERIES_MAX = 5
FILL_RANGE = (.25, .75)
FILL_GRID = 4                 # px, cell of the union-area raster
READABLE, READABLE_LARGE, LARGE_PT, LARGE_BOLD_PT = 4.5, 3.0, 18, 14
# Text of a slide that is not ours: placeholders of Appendix 1 and typical sample text of templates.
PLACEHOLDER = re.compile(r"lorem ipsum|\bXXX\b|\bTODO\b|вставьте текст|insert text|click to add|щелкните|заголовок слайда", re.I)
ORDINAL = re.compile(r"^[\s\d.,:%№#()\-–—/]+$")
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
REGISTRY = Path(__file__).resolve().parents[3] / "audit/checks.json"
_CHECKS = {}


def checks():
    """The registry audit/checks.json by code (part of the release; C1)."""
    if not _CHECKS:
        _CHECKS.update({c["id"]: c for c in json.loads(REGISTRY.read_bytes().decode("utf-8"))["checks"]})
    return _CHECKS


def template_tokens(style):
    """Typefaces, sizes (pt), colours (RGB) and layout parts of a reference, from its inventory and observed text."""
    observations = style.get("observations") or []
    carrier = style.get("carrier") or {}
    fonts = {o["font"] for o in observations if o.get("font")} | {f["family"] for f in style.get("fonts") or []}
    # Session 15 (26.09.2026): a typeface the theme, a master, a layout or a slide of the template names is the template's
    # (SIGGRAPH Asia 2025: 101 FONT_NOT_IN_TEMPLATE errors named Calibri, the minor font of its own theme; 187 on two templates).
    fonts |= {f["family"] for f in (style.get("font_inventory") or {}).get("families") or []
              if set(f.get("parts") or {}) & {"theme", "master", "layout", "slide"}}
    sizes = {float(o["size_pt"]) for o in observations if o.get("size_pt")}
    colours = {c.upper() for c in (style.get("palette") or {}).values() if isinstance(c, str)}
    colours |= {o["color"].upper() for o in observations if isinstance(o.get("color"), str)}
    for layout in carrier.get("layouts", []):
        for ph in layout["placeholders"]:
            t = ph.get("typography") or {}
            if t.get("size_pt"):
                sizes.add(float(t["size_pt"]))
            if t.get("font"):
                fonts.add(t["font"])
            if isinstance(t.get("color"), str):
                colours.add(t["color"].upper())
    for sample in carrier.get("card_samples", []):
        for card in sample["cards"]:
            for slot in card["slots"]:
                t = slot["typography"]
                sizes.add(float(t["size_pt"]))
                fonts.add(t["font"])
                colours.add(t["color"].upper())
    # Proposals of 24.09.2026: content scenes of sample slides the parser recognised, and those that carry decor of their own
    # slide (furniture: bands, logos the author repeats on slides), for BRAND_SCENE_LOST.
    content = [p for p in (style.get("reference") or {}).get("patterns", []) if p.get("role") == "content" and p.get("source_kind") == "slide"]
    scenes = sorted({p["source_slide"] for p in content})
    furnished = sorted({p["source_slide"] for p in content if any(d.get("purpose") == "furniture" for d in p.get("decorations", []))})
    # Item 31 (25.09.2026): the mandatory decor of the sample slides (reference.brand_decor) — its items, what every layout
    # draws of it and what every sample slide shows of the items copies carry — for BRAND_SCENE_LOST.
    brand = carrier.get("brand_decor") or {}
    decor = {"items": [{k: i[k] for k in ("id", "kind", "box", "tones", "transferable", "occurrences")} for i in brand.get("items", [])],
             "layouts": brand.get("layouts", {}), "slides": brand.get("slides", {})} if brand.get("items") else None
    # Item 35, H3 (holdout 25.09.2026): text of their sample slides that layouts draw themselves, for LAYOUT_SAMPLE_TEXT.
    texts = {part: [t["text"] for t in rows] for part, rows in (brand.get("sample_texts") or {}).items()}
    # Item 37, cause 6 (holdout-next 25.09.2026): footer lines of layouts and masters that name the sample presentation; the
    # exporter rewrites them for the deck (carrier_export.rewrite_running_footers).
    footers = brand.get("running_footers") or {}
    running = {"cover_title": brand.get("cover_title"), "parts": {p: [s["shape"] for s in rows] for p, rows in sorted(footers.items())}}
    return {"fonts": sorted(fonts), "sizes_pt": sorted(sizes), "colours": sorted(colours),
            "layouts": sorted(l["part"] for l in carrier.get("layouts", [])), "scenes": scenes, "furnished_scenes": furnished,
            **({"brand_decor": decor} if decor else {}), **({"layout_texts": texts} if texts else {}),
            **({"running_footers": running} if footers else {})}


def _finding(code, severity=None, slide=None, element=None, **details):
    """A finding; severity None takes the default of the registry, a given one (C3: a colour of the reference) stays."""
    entry = checks()[code]
    out = {"code": code, "severity": severity or entry["severity"], "check": "audit", "title": entry["title"]}
    if entry["fixer"] and "fixer" not in details:
        out["fixer"] = entry["fixer"]
    if slide is not None:
        out["slide"] = slide
    if element is not None:
        out["element"] = element
    out.update(details)
    return out


def _near(colour, palette):
    value = bytes.fromhex(colour)
    return any(max(abs(a - b) for a, b in zip(value, bytes.fromhex(c))) <= COLOUR_TOLERANCE for c in palette)


def _background(slide, e):
    """Solid colour under a text element, None when a picture of the reference lies under it (rendered check decides)."""
    box = [e[k] for k in ("x", "y", "w", "h")]
    background = slide["background"]
    for s in slide["elements"]:
        if s is e:
            break
        frame = [s[k] for k in ("x", "y", "w", "h")]
        if s["type"] == "shape" and not s.get("ghost") and contains(frame, box):
            background = blend(s["fill"], background, s.get("opacity", 1))
        elif s["type"] == "image" and not s.get("protected_asset") and contains(frame, box):
            return None
    return background


def _source_colour(e):
    """Colour the reference itself gives this text: a placeholder or a cloned slot keeps it; the rule of contrast or a
    fixer may have changed it, and their adjustment records the colour before."""
    for a in e.get("style_adjustments", []):
        if a.get("reason") == "solid-background-contrast":
            return a["from"]
        if str(a.get("reason", "")).startswith("fixer:") and "color" in a:
            return a["color"][0]
    return e["color"]


def _fill(doc, slide):
    """Share of the slide under content blocks: the title and the fact text (their estimated lines, not a large empty
    frame), tables and the cloned cards that hold facts. A raster of FILL_GRID px counts every point once."""
    w, h = doc["width"], doc["height"]
    boxes = []
    facts = [e for e in slide["elements"] if e["type"] == "text" and (e.get("fact_ids") or e.get("label_of") or e.get("metric_of"))]
    for e in facts + [e for e in slide["elements"] if e["type"] == "text" and e["id"] == "title"]:
        inset = e.get("inset") or [0, 0, 0, 0]
        width = max(1, e["w"] - inset[0] - inset[2])
        height = estimate_lines(e["text"], width, e["font_size"]) * e["font_size"] * e.get("line_height", 1.2) + inset[1] + inset[3]
        boxes.append([e["x"], e["y"], e["w"], min(e["h"], height)])
    boxes += [[e[k] for k in ("x", "y", "w", "h")] for e in slide["elements"] if e["type"] in ("table", "chart")]
    for e in slide["elements"]:
        b = e.get("binding", {})
        if b.get("kind") == "clone" and e["type"] == "shape" and not e.get("ghost"):
            frame = [e[k] for k in ("x", "y", "w", "h")]
            if any(contains(frame, [t[k] for k in ("x", "y", "w", "h")], 2) for t in facts) and frame[2] * frame[3] < w * h * .9:
                boxes.append(frame)
    cells = set()
    for x, y, bw, bh in boxes:
        for i in range(max(0, int(x // FILL_GRID)), min(int(w // FILL_GRID), int((x + bw) // FILL_GRID) + 1)):
            for j in range(max(0, int(y // FILL_GRID)), min(int(h // FILL_GRID), int((y + bh) // FILL_GRID) + 1)):
                cells.add((i, j))
    return len(cells) * FILL_GRID * FILL_GRID / (w * h)


def appendix_findings(doc):
    """Findings of the checks of audit/checks.json with source "audit"; template checks need doc["template"]."""
    tokens = doc.get("template")
    found = []
    # Item 31: text of mandatory decor copied from a sample slide (decor) is the author's; its typeface, size and colour are
    # the sample's, so the checks of the scale and the palette do not apply to it.
    texts = [(s, e) for s in doc["slides"] for e in s["elements"] if e["type"] == "text" and not e.get("decor")]
    # Template: typefaces, size scale, colours, layouts, contrast.
    if tokens:
        fonts, sizes, colours = set(tokens["fonts"]), tokens["sizes_pt"], set(tokens["colours"])
        for s, e in texts:
            if e["font"] not in fonts:
                found.append(_finding("FONT_NOT_IN_TEMPLATE", None, s["id"], e["id"], font=e["font"]))
            size = e["font_size"] * PT
            if sizes and min(abs(size - v) for v in sizes) > SIZE_TOLERANCE:
                found.append(_finding("SIZE_OFF_SCALE", None, s["id"], e["id"], size_pt=round(size, 2)))
            if colours and not _near(e["color"].upper(), colours):
                found.append(_finding("COLOR_OFF_PALETTE", None, s["id"], e["id"], color=e["color"]))
        typefaces = sorted({e["font"] for _, e in texts})
        if len(typefaces) > 2:
            found.append(_finding("TOO_MANY_TYPEFACES", None, typefaces=typefaces))
        if doc.get("export", {}).get("mode") == "carrier":
            for s in doc["slides"]:
                part = (s.get("layout") or {}).get("part")
                if part not in tokens["layouts"]:
                    found.append(_finding("LAYOUT_NOT_FROM_TEMPLATE", None, s["id"], layout=part))
                # Proposals of 24.09.2026: the audit checked the document against what the parser kept, so a slide that left
                # the style of the sample for a plain layout passed (blind test 23.09, cause 7: deck 29; cycle g2c: the band of
                # deck 18). Reported for a look: a content slide of the fallback path while the reference has content scenes of
                # its own, and a content slide on a layout with none of the slide decor its sample slides carry.
                selection, reference = s.get("selection") or {}, s.get("reference") or {}
                fallback = selection.get("method") == "carrier-fallback-v1" and tokens.get("scenes")
                # Item 31 (25.09.2026): a content slide lacks mandatory decor of the sample slides (reference.brand_decor) its
                # layout does not draw and no copy on it carries: every item on a slide on a layout; on a slide built on a
                # sample slide, the items that sample shows itself. Replaces the rule "a scene with any furniture picture",
                # which reported logos the chosen layout draws (deck 03, deck 14: 99 of 232 findings of n1) and missed bands and
                # rules of layouts (deck 04, deck 05, Office).
                decor = tokens.get("brand_decor")
                missing = []
                if decor and s["role"] != "cover":
                    # Drawn: by the layout, by a copy the composer placed (brand_item), or by a decoration of the sample slide
                    # the exporter clones (its source shape is an occurrence of the item).
                    sources = {(src.get("part"), str(src.get("shape"))) for e in s["elements"]
                               for src in ((e.get("binding") or {}).get("source") or {}, e.get("source_style") or {}) if src.get("shape") is not None}
                    drawn = set(decor["layouts"].get(part, [])) | {e.get("brand_item") for e in s["elements"] if e.get("brand_item")}
                    drawn |= {i["id"] for i in decor["items"] if any((k, str(v)) in sources for k, v in (i.get("occurrences") or {}).items())}
                    scene = reference.get("source_part") if reference.get("source_kind") == "slide" else None
                    title = next((e for e in s["elements"] if e["id"] == "title" and e["type"] == "text"), None)
                    tone = slide_tone(title["color"]) if title else "light"
                    wanted = ([i for i in decor["items"] if i["transferable"] and i["id"] in decor["slides"].get(scene, [])] if scene
                              else [i for i in decor["items"] if tone in (i.get("tones") or [tone])])
                    missing = [i for i in wanted if i["id"] not in drawn]
                if s["role"] != "cover" and (fallback or missing):
                    details = {"missing": [{"item": i["id"], "kind": i["kind"], "box": [round(v) for v in i["box"]]} for i in missing]} if missing else {}
                    found.append(_finding("BRAND_SCENE_LOST", None, s["id"], scenes=len(tokens["scenes"]),
                                          reason=selection.get("reason") if fallback else "mandatory-decor-missing", **details))
                # Item 35, H3 (holdout 25.09.2026): the layout of a content slide draws text of its sample slide itself
                # ("LEVEL 3 WILL:" of deck 08; the labels of the form layouts of deck 15), shown as the text of this slide.
                drawn_texts = (tokens.get("layout_texts") or {}).get(part)
                if drawn_texts and s["role"] != "cover":
                    found.append(_finding("LAYOUT_SAMPLE_TEXT", None, s["id"], layout=part, texts=drawn_texts[:6]))
    for s, e in texts:
        background = _background(s, e)
        if background is None:
            continue
        ratio = contrast(e["color"], background)
        large = e["font_size"] * PT >= LARGE_PT or (e.get("bold") and e["font_size"] * PT >= LARGE_BOLD_PT)
        if ratio < (READABLE_LARGE if large else READABLE):
            # C3: a colour of the reference itself is kept and reported as a warning.
            own = _source_colour(e) == e["color"]
            found.append(_finding("CONTRAST_BELOW_AA", "warning" if own else "error", s["id"], e["id"], contrast=round(ratio, 2),
                                  background=background, template_colour=own, **({"fixer": None} if own else {})))
    # Owner decision 47 (28.09.2026): a statement content_writer added to a short request is shown to the user as such.
    by_model = {f["id"] for sec in (doc.get("brief") or {}).get("sections", []) for f in sec.get("facts", []) if f.get("source") == "model"}
    if by_model:
        for s in doc["slides"]:
            for e in s["elements"]:
                ids = [fid for fid in e.get("fact_ids") or [] if fid in by_model]
                if ids:
                    found.append(_finding("CONTENT_ADDED_BY_MODEL", None, s["id"], e["id"], facts=ids))
    # Layout: pictures moved by the product keep the proportions of the reference.
    for s in doc["slides"]:
        for e in s["elements"]:
            b = e.get("binding", {})
            source = (b.get("source") or {}).get("box")
            if e["type"] == "image" and b.get("kind") == "clone" and source and source[2] > 0 and source[3] > 0:
                frame = b["frame"]
                change = (frame[2] / frame[3]) / (source[2] / source[3])
                if abs(change - 1) > ASPECT_TOLERANCE:
                    found.append(_finding("IMAGE_DISTORTED", None, s["id"], e["id"], aspect_change=round(change, 3)))
    # Density and integrity; the cover is exempt by the registry.
    seen = {}
    for s in doc["slides"]:
        items = [p for e in s["elements"] if e["type"] == "text" and e.get("fact_ids") for p in e["text"].split("\n") if p.strip()]
        tables = [e for e in s["elements"] if e["type"] == "table"]
        if len(items) > MAX_ITEMS:
            found.append(_finding("TOO_MANY_ITEMS", None, s["id"], items=len(items)))
        for e in s["elements"]:
            if e["type"] == "text" and e.get("fact_ids"):
                for p in e["text"].split("\n"):
                    if len(p.split()) > MAX_WORDS:
                        found.append(_finding("ITEM_TOO_LONG", None, s["id"], e["id"], words=len(p.split())))
        for t in tables:
            if len(t["rows"]) - 1 > MAX_TABLE_ROWS or len(t["rows"][0]) > MAX_TABLE_COLUMNS:
                found.append(_finding("TABLE_TOO_LARGE", None, s["id"], t["id"], rows=len(t["rows"]) - 1, columns=len(t["rows"][0])))
        # G1: a chart names its values (label of the value axis with the unit, the title of a pie), its categories and,
        # with more than one series or on a pie, shows a legend (charts.legend_shown); more than 5 series is too many to read.
        for c in (e for e in s["elements"] if e["type"] == "chart"):
            data = c["chart"]
            named = all(str(v).strip() for v in data["categories"] + [x["name"] for x in data["series"]])
            if not (str(data.get("unit", "")).strip() and str(data.get("value_label", "")).strip() and named):
                found.append(_finding("CHART_UNLABELED", None, s["id"], c["id"]))
            if len(data["series"]) > SERIES_MAX:
                found.append(_finding("CHART_TOO_MANY_SERIES", None, s["id"], c["id"], series=len(data["series"])))
        if not items and not tables and s["role"] != "cover":
            found.append(_finding("EMPTY_SLIDE", None, s["id"]))
        if not any(e["type"] in ("text", "table") and e["id"] not in ("disclosure", "page") and not e.get("decor") for e in s["elements"]):
            found.append(_finding("SLIDE_IS_IMAGE", None, s["id"]))
        title = next((e["text"] for e in s["elements"] if e["id"] == "title"), "")
        key = (title.strip().lower(), tuple(sorted(items)))
        if key in seen:
            found.append(_finding("DUPLICATE_SLIDE", None, s["id"], duplicate_of=seen[key]))
        seen.setdefault(key, s["id"])
        fill = _fill(doc, s)
        if not FILL_RANGE[0] <= fill <= FILL_RANGE[1]:
            found.append(_finding("FILL_LOW" if fill < FILL_RANGE[0] else "FILL_HIGH", None, s["id"], fill=round(fill, 3)))
    return with_roles(doc, found)


def with_roles(doc, found):
    """C3: the severity of a finding on a slide whose role the registry names (a cover) becomes that role's."""
    roles = {s["id"]: s["role"] for s in doc["slides"]}
    for f in found:
        role = roles.get(f.get("slide"))
        severity = checks()[f["code"]]["roles"].get(role)
        if severity:
            f["severity"], f["role"] = severity, role
    return found


def package_findings(doc, pptx):
    """Text of the slides of a finished PPTX that the model does not have: placeholders and sample text of the reference."""
    known = {}
    for n, s in enumerate(doc["slides"], 1):
        values = set()
        for e in s["elements"]:
            if e["type"] == "text":
                values |= {line.strip() for line in e["text"].split("\n")}
                # Item 31: paragraphs of copied decor as the PPTX joins them (a line break inside a paragraph included).
                values |= set(e.get("package_lines") or [])
            elif e["type"] == "table":
                values |= {str(c).strip() for row in e["rows"] for c in row}
        known[n] = values
    found = []
    with zipfile.ZipFile(io.BytesIO(pptx)) as z:
        for n, s in enumerate(doc["slides"], 1):
            root = ET.fromstring(z.read(f"ppt/slides/slide{n}.xml"))
            for p in root.iter(f"{{{A}}}p"):
                text = "".join(t.text or "" for t in p.iter(f"{{{A}}}t")).strip()
                if not text or text in known[n]:
                    continue
                if PLACEHOLDER.search(text) or not ORDINAL.match(text):
                    found.append(_finding("PLACEHOLDER_TEXT", None, s["id"], text=text[:80]))
    return found
