"""Deterministic fixers of findings (stage C6; registry audit/checks.json, field "fixer").

A fixer changes a copy of a document for one finding and says what it did; pipeline.fix saves the result as a new
revision and audits it again. Fixers only touch what the user may edit anyway (text geometry, size, colour, typeface of
text that is not locked) and choose values of the reference: sizes of its scale, colours of its palette, its typeface.
"""
from .audit import PT, READABLE, READABLE_LARGE, LARGE_PT, LARGE_BOLD_PT, _background
from .patterns import contrast
from .placement import estimate_lines


def finding_id(f):
    """Stable identifier of a finding within a revision: code, slide and element."""
    return "|".join(str(f.get(k) or "") for k in ("code", "slide", "element"))


def _element(doc, f):
    slide = next((s for s in doc["slides"] if s["id"] == f.get("slide")), None)
    element = next((e for e in slide["elements"] if e["id"] == f.get("element")), None) if slide else None
    return slide, element


def _fits(e, size):
    inset = e.get("inset") or [0, 0, 0, 0]
    width = max(1, e["w"] - inset[0] - inset[2])
    lines = estimate_lines(e["text"], width, size)
    # the gap before the first paragraph only with spcFirstLastPara (model.py, TEXT_LAYOUT)
    gaps = e["text"].count("\n") + (1 if e.get("space_first_last", True) else 0)
    return lines * size * e.get("line_height", 1.2) + (e.get("paragraph_gap") or 0) * gaps + inset[1] + inset[3] <= e["h"] + .5


def snap_size(doc, slide, e, smaller_only=False):
    """The nearest size of the scale of the reference at which the text still fits its frame (for text that overflows,
    smaller sizes only)."""
    scale = sorted({v / PT for v in (doc.get("template") or {}).get("sizes_pt", [])})
    if not scale:
        return None
    current = e["font_size"]
    candidates = [v for v in scale if v >= 6 and abs(v - current) > .01 and (v < current or not smaller_only)]
    for size in sorted(candidates, key=lambda v: (abs(v - current), v)):
        if _fits(e, size):
            e["font_size"] = round(size, 2)
            return {"font_size": [current, e["font_size"]]}
    return None


def fit_text(doc, slide, e):
    return snap_size(doc, slide, e, smaller_only=True)


def nearest_palette_colour(doc, slide, e):
    """The palette colour closest to the current one that reads on the background (4.5:1, large text 3:1)."""
    colours = (doc.get("template") or {}).get("colours", [])
    background = _background(slide, e)
    if not colours or background is None:
        return None
    large = e["font_size"] * PT >= LARGE_PT or (e.get("bold") and e["font_size"] * PT >= LARGE_BOLD_PT)
    need = READABLE_LARGE if large else READABLE
    current = bytes.fromhex(e["color"])
    readable = [c for c in colours if contrast(c, background) >= need]
    if not readable:
        return None
    choice = min(readable, key=lambda c: sum((a - b) ** 2 for a, b in zip(bytes.fromhex(c), current)))
    old, e["color"] = e["color"], choice
    return {"color": [old, choice]}


def readable_colour(doc, slide, e, finding):
    """LOW_RENDERED_CONTRAST (owner decision 48): the colour that reads on the background the check of the rendering measured
    under the glyphs (finding["background"]): of the palette of the template, of the other texts of the document or a neutral
    one; at least 4.5:1 (large text 3:1); of those, the nearest to the own colour. The check of the new revision measures it."""
    background = str(finding.get("background") or "").upper()
    if len(background) != 6:
        return None
    colours = [str(c).upper() for c in (doc.get("template") or {}).get("colours", [])]
    colours += [str(x["color"]).upper() for s in doc["slides"] for x in s["elements"] if x["type"] == "text" and x.get("color")]
    colours = [c for c in dict.fromkeys(colours + ["151515", "FFFFFF"]) if len(c) == 6 and c != str(e["color"]).upper()]
    large = e["font_size"] * PT >= LARGE_PT or (e.get("bold") and e["font_size"] * PT >= LARGE_BOLD_PT)
    need = READABLE_LARGE if large else READABLE
    readable = [c for c in colours if contrast(c, background) >= need]
    if not readable:
        return None
    current = bytes.fromhex(e["color"])
    choice = min(readable, key=lambda c: (sum((a - b) ** 2 for a, b in zip(bytes.fromhex(c), current)), c))
    old, e["color"] = e["color"], choice
    return {"color": [old, choice], "background": background}


readable_colour.takes_finding = True


def replace_font(doc, slide, e):
    font = doc["style"]["font"]
    if e["font"] == font:
        return None
    old, e["font"] = e["font"], font
    return {"font": [old, font]}


def move_into_bounds(doc, slide, e):
    w, h = doc["width"], doc["height"]
    old = [e[k] for k in ("x", "y", "w", "h")]
    e["w"], e["h"] = min(e["w"], w), min(e["h"], h)
    e["x"], e["y"] = min(max(0, e["x"]), w - e["w"]), min(max(0, e["y"]), h - e["h"])
    new = [e[k] for k in ("x", "y", "w", "h")]
    return {"box": [old, new]} if new != old else None


# C6: fixers carried out by an agent (pipeline.fix): the finding gets a checkbox as a deterministic fixer does.
MODEL_FIXERS = {"rewrite-title"}
FIXERS = {"snap-size": snap_size, "fit-text": fit_text, "nearest-palette-colour": nearest_palette_colour,
          "replace-font": replace_font, "move-into-bounds": move_into_bounds, "readable-colour": readable_colour}


def apply(doc, findings):
    """Apply the fixers of the given findings to doc in place; returns what was done and what could not be."""
    done, skipped = [], []
    for f in findings:
        fixer = FIXERS.get(f.get("fixer"))
        slide, e = _element(doc, f)
        if fixer is None or e is None or e.get("locked") or e["type"] != "text":
            skipped.append({"finding": finding_id(f), "reason": "no-fixer" if fixer is None else "not-editable"})
            continue
        change = fixer(doc, slide, e, f) if getattr(fixer, "takes_finding", False) else fixer(doc, slide, e)
        if change is None:
            skipped.append({"finding": finding_id(f), "reason": "no-value-of-the-reference-fits"})
        else:
            e.setdefault("style_adjustments", []).append({"reason": "fixer:" + f["fixer"], "finding": f["code"], **change})
            if e.get("binding", {}).get("kind") == "clone":
                # Cloned text keeps the run properties of the sample; the exporter applies these on top (carrier_export).
                e["binding"].setdefault("overrides", {}).update({k: e[k] for k in ("color", "font") if k in change})
            done.append({"finding": finding_id(f), "fixer": f["fixer"], "change": change})
    return done, skipped
