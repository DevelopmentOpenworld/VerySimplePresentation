"""Dashes that mark the items of a sample slide (session 15, 26.09.2026; holdout-next2, deck 36, sample slide 19).

The sample sets four numbered items, each under a short orange dash: the dash, a large number, a heading and a text. The
number and the heading are narrower than a text region may be (patterns.py), so the composition keeps only the text of each
item; the dashes, which carry no text and stand above none, were decor of the whole slide. The composer placed the facts in
the text regions and drew every dash: on 22 of 33 content slides 88 dashes, 46 of them beside no text.

A dash: a filled shape of the sample slide itself, thin (at most THIN of the height), at least LONG times longer than thick
and no longer than SHORT of the width (not a rule across the slide: deck 12, template A 52; not a dot: the markers of the template A
cards come with the cloned cards), at most SMALL of the slide, with a twin of the same size on the slide, close to a text of
the sample and not to its title. Its item: the text frames whose top lies from NEAR_Y above to BAND_BELOW below the dash and
whose left edge lies from SIDE left of it to the next dash of the band. The choice of compositions does not change (research
variant A, widening the regions before the choice, lost the three blue scenes of deck 36 to the white slide 19); after the
composer a dash stays only where the text region of its item holds a fact, and that text starts under the dash; slides of
key numbers, charts and processes on the composition keep none.

Researched outside the product in analysis/style-experiments/20260926-hn2-rest/process/research-unisinos-siggraph (variant
B): stray dashes 46 -> 0 on deck 36; on the 57 templates of the regressions the rule finds dashes only there.
"""

THIN = 0.015
SMALL = 0.005
LONG = 4
SHORT = 0.2
NEAR_Y = 0.04
BAND_BELOW = 0.08
SIDE = 0.03


def _near(a, b, height):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    xover = min(ax + aw, bx + bw) - max(ax, bx)
    gap_y = max(by - (ay + ah), ay - (by + bh), 0)
    return xover > 0 and gap_y <= NEAR_Y * height


def _frames(style, slide):
    out = {}
    for o in style.get("observations") or []:
        if o.get("slide") == slide and o.get("box"):
            out.setdefault(str(o["shape"]), list(o["box"]))
    return out


def find(style, p):
    """[(dash decoration, text region of its item or None, frames of the item)] of composition p."""
    if p.get("source_kind") != "slide":
        return []
    W, H = style["width"], style["height"]
    dashes = [d for d in p["decorations"] if d.get("kind") == "shape" and d["source"].get("part") == p["source_part"]
              and not d["source"].get("kind") and d["box"][2] * d["box"][3] <= SMALL * W * H
              and min(d["box"][2], d["box"][3]) <= THIN * H and max(d["box"][2], d["box"][3]) >= LONG * max(1, min(d["box"][2], d["box"][3]))
              and max(d["box"][2], d["box"][3]) <= SHORT * W]
    size = lambda d: (round(d["box"][2] / 3), round(d["box"][3] / 3))  # noqa: E731
    twins = {}
    for d in dashes:
        twins.setdefault(size(d), []).append(d)
    dashes = [d for d in dashes if len(twins[size(d)]) >= 2]
    if not dashes:
        return []
    frames = _frames(style, p["source_slide"])
    title_shape = str((p["title"].get("source") or {}).get("shape"))
    title_box = frames.get(title_shape, p["title"]["box"])
    texts = {s: b for s, b in frames.items() if s != title_shape}
    marks = [d for d in dashes if not _near(d["box"], title_box, H) and any(_near(d["box"], b, H) for b in texts.values())]
    out = []
    for m in marks:
        mx, my = m["box"][0], m["box"][1]
        band = [o for o in marks if o is not m and abs(o["box"][1] - my) <= NEAR_Y * H and o["box"][0] > mx]
        limit = min((o["box"][0] for o in band), default=W + SIDE * W) - SIDE * W
        item = {s: b for s, b in texts.items() if my - NEAR_Y * H <= b[1] <= my + BAND_BELOW * H and mx - SIDE * W <= b[0] < limit}
        bodies = [s for s in p["slots"] if (s.get("classification") or {}).get("role") == "body" and str(s["source"].get("shape")) in item]
        out.append((m, bodies[0] if len(bodies) == 1 else None, item))
    return out


def widening(style):
    """({pattern id: {dash shape: text shape of its item or None}}, {pattern id: {text shape: (x, right edge)}}): the dashes
    of every composition and the text regions of their items widened to start under the dash."""
    table, wide = {}, {}
    for p in style["reference"]["patterns"]:
        found = find(style, p)
        if not found:
            continue
        table[p["id"]] = {}
        for m, body, _ in found:
            table[p["id"]][str(m["source"]["shape"])] = str(body["source"]["shape"]) if body else None
            if body is None:
                continue
            x, _, w, _ = body["box"]
            left = m["box"][0] - (body.get("inset") or [9.6])[0]
            if left < x:
                wide.setdefault(p["id"], {})[str(body["source"].get("shape"))] = (left, x + w)
    return table, wide


def apply(docs, table, wide):
    """Remove the dashes of items without a fact (with their entry of the style contract) and let the text of an item
    start under its dash. Slides of key numbers, charts and processes on the composition use none of its item regions, so
    they keep no dash (deck 36, brief visuals: four dashes over three metric cards); cloned cards bring their own shapes.
    Returns (dashes removed, texts widened)."""
    removed = widened = 0
    for doc in docs:
        for s in doc["slides"]:
            pid = (s.get("reference") or {}).get("pattern")
            if pid not in table or (s.get("selection") or {}).get("method") == "sample-units-v1":
                continue
            used = {str(r["source"].get("shape")) for r in (s.get("selection") or {}).get("body_regions", [])}
            keep = []
            for e in s["elements"]:
                shape = str((e.get("source_style") or {}).get("shape"))
                if e.get("template_decoration") and str(e["id"]).startswith("decor") and shape in table[pid] and table[pid][shape] not in used:
                    removed += 1
                    (s.get("style_contract") or {}).get("decorations", {}).pop(e["id"], None)
                    continue
                keep.append(e)
            s["elements"] = keep
            for e in s["elements"]:
                shape = str((e.get("source_style") or {}).get("shape"))
                if e.get("type") == "text" and shape in wide.get(pid, {}) and abs(e["x"] + e["w"] - wide[pid][shape][1]) < 1:
                    x, right = wide[pid][shape]
                    e["x"], e["w"] = round(x, 2), round(right - x, 2)
                    if e.get("reference_box"):
                        e["reference_box"] = [e["x"], e["reference_box"][1], e["w"], e["reference_box"][3]]
                    widened += 1
    return removed, widened
