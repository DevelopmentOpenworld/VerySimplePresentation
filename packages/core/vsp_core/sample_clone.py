"""A7: repeated cards of a sample slide, cloned as whole units (docs/CARRIER_PRODUCT_2026-09-21.md, "Проект A7").

read_card_samples finds on every sample slide of the reference a set of equal cards: a filled
shape or a picture that holds text, together with every top-level shape lying inside it.
plan_cards lays facts out on such a set: one fact per card, surplus cards removed, the rest
spread over the original span, each card moved as a whole. card_elements turns a plan into
document elements bound "clone": the exporter copies the source XML of every top-level shape
with a new frame and, in the text slot, writes the facts with the properties of its first run.
"""
import base64
import re
import statistics

from . import png, typography
from .importer import EMU, digest
from .opc import parse_xml
from .patterns import blend, contains, contrast, rgb
from .placement import estimate_lines
from .reference import NS

P, A, R = NS["p"], NS["a"], NS["r"]
KINDS = ("sp", "grpSp", "pic", "cxnSp", "graphicFrame")
CARD_GEOMETRY = ("rect", "roundRect", "snip1Rect", "snip2SameRect", "round1Rect", "round2SameRect")
PREVIEW_GEOMETRY = {"rect":"rect", "roundRect":"roundRect", "ellipse":"ellipse"}
DEFAULT_INSETS = (91440, 45720, 91440, 45720)
# DrawingML presetShapeDefinitions, roundRect: the corner radius is adj/100000 of the shorter side (adj 16667 by default,
# pinned to 0..50000) and the text rectangle is inset from the frame by 29289/100000 of that radius on every side;
# PowerPoint lays the text out inside it, before the bodyPr insets (template B cards: 3.5..15.5 px, journal of the tails
# of the sixth session, 23.09).
ROUND_RECT_DEFAULT_ADJ = .16667
ROUND_RECT_TEXT = .29289
# A card is a container of 1.2..45 % of the slide, at least 8 % of its width and height.
CARD_AREA = (.012, .45)
CARD_SIDE = .08
SAME_SIZE = .04
# A card of the same row or column that differs more (a highlighted card of another width) is a sibling: it holds no
# fact, is removed with the unused cards, and its place counts in the span the kept cards are spread over.
SIBLING_SIZE = .15
# A primary slot that is a number or a metric ("10%", "259 ₽", "01") marks a metric card, not a text card.
METRIC = re.compile(r"^[\d\s%₽$€.,:+\-−–xX×]+$")
MAX_PRIMARY_PT = 28
MAX_REMOVED = 3
# Largest share of the slide an undrawable cloned shape may cover to become an invisible obstacle for the preview.
GHOST_MAX_AREA = .05
# Largest share of the slide an icon may cover to go together with the sample text it accompanies.
COMPANION_AREA = .03
# Appendix 1 of the case: text contrast below 4.5:1 is a defect. A label of a fact is a heading, and for large text
# (18 pt, or 14 pt bold) WCAG 2.1, criterion 1.4.3, asks 3:1; the refinement is justified in docs/VARIANT_AXIS.md.
READABLE = 4.5
READABLE_LARGE = 3.0
LARGE_PT, LARGE_BOLD_PT = 18, 14
# Owner decision 49 (29.09.2026), limitation 3 (template A 26): the fourth card of the sample is another picture than the three
# plain ones, with a glass disc drawn in it; a long fact of the fourth card ran onto the disc, blue on light blue (K3-8, 3
# findings LOW_RENDERED_CONTRAST). A point of a card picture that differs from the plain card of its sample by more than
# DRAWN_DIFF in a channel is drawn; points within DRAWN_EDGE of the smaller side of the card from its edges are not looked at
# (the fourth card of template A 26 also has an outline of its own); the drawing is the frame of at least DRAWN_MIN_POINTS such
# points sampled every DRAWN_STEP px, and the text of that card stops DRAWN_GAP px above it.
DRAWN_DIFF = 40
DRAWN_EDGE = .04
DRAWN_STEP = 3
DRAWN_MIN_POINTS = 6
DRAWN_GAP = 4
# Sample text of a slot meant for running text, as template authors write it.
BODY_SAMPLE = re.compile(r"^(текст|text|описание|description|lorem ipsum.*)$", re.I)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _xfrm(node):
    kind = _local(node.tag)
    path = {"grpSp":"p:grpSpPr/a:xfrm", "graphicFrame":"p:xfrm"}.get(kind, "p:spPr/a:xfrm")
    return node.find(path, NS)


def _box_flags(node):
    x = _xfrm(node)
    if x is None:
        return None, {}
    off, ext = x.find("a:off", NS), x.find("a:ext", NS)
    if off is None or ext is None:
        return None, {}
    flags = {k: x.get(k) for k in ("rot", "flipH", "flipV") if x.get(k) not in (None, "0", "false")}
    return [int(off.get("x")) / EMU, int(off.get("y")) / EMU, int(ext.get("cx")) / EMU, int(ext.get("cy")) / EMU], flags


def _fill(node):
    """Kind of the own fill of a shape: solidFill, gradFill, blipFill, pattFill, style or None."""
    sppr = node.find("p:spPr", NS)
    if sppr is not None:
        for child in sppr:
            tag = _local(child.tag)
            if tag in ("solidFill", "gradFill", "blipFill", "pattFill"):
                return tag
            if tag == "noFill":
                return None
    ref = node.find("p:style/a:fillRef", NS)
    return "style" if ref is not None and ref.get("idx", "0") != "0" else None


def _line_visible(node):
    ln = node.find("p:spPr/a:ln", NS)
    if ln is not None:
        return ln.find("a:noFill", NS) is None and len(ln) > 0
    ref = node.find("p:style/a:lnRef", NS)
    return ref is not None and ref.get("idx", "0") != "0"


def _text_of(paragraph):
    return "".join("\n" if _local(c.tag) == "br" else "".join(t.text or "" for t in c.iter(f"{{{A}}}t")) for c in paragraph)


def _spacing(ppr, tag):
    node = ppr.find(f"a:{tag}", NS) if ppr is not None else None
    if node is None:
        return None
    pct, pts = node.find("a:spcPct", NS), node.find("a:spcPts", NS)
    if pct is not None:
        return {"pct": int(pct.get("val")) / 1000}
    if pts is not None:
        return {"pt": int(pts.get("val")) / 100}
    return None


class _Context:
    """Colours and fonts of one sample slide."""
    def __init__(self, package, part, layout, presentation):
        self.presentation = presentation
        self.slide = parse_xml(package.parts[part])
        self.layout = parse_xml(package.parts[layout]) if layout else None
        masters = package.related(layout, "slideMaster") if layout else []
        self.master = parse_xml(package.parts[masters[0]]) if masters else None
        themes = package.related(masters[0], "theme") if masters else []
        self.theme = parse_xml(package.parts[themes[0]]) if themes else None
        self.palette, self.mapping = typography.color_context(self.theme, self.master, (self.layout, self.slide))
        self.fonts = typography.theme_fonts(self.theme, "Arial")

    def colour(self, node):
        return rgb(node, self.palette, self.mapping) if node is not None else None

    def typeface(self, name):
        if name and name.startswith("+"):
            return self.fonts.get("major" if name.startswith("+mj") else "minor", "Arial")
        return name


def _round_adj(node):
    """Corner radius of a roundRect shape as a share of its shorter side; None for any other geometry."""
    geometry = node.find("p:spPr/a:prstGeom", NS)
    if geometry is None or geometry.get("prst") != "roundRect":
        return None
    gd = geometry.find("a:avLst/a:gd[@name='adj']", NS)
    value = re.match(r"val\s+(-?\d+)$", (gd.get("fmla") or "").strip()) if gd is not None else None
    return min(50000, max(0, int(value.group(1)))) / 100000 if value else ROUND_RECT_DEFAULT_ADJ


def _text_rect(slot, frame):
    """Inset of the text rectangle of the shape of a slot from a frame of that shape, px, the same on every side."""
    adj = slot["typography"].get("round_adj")
    return ROUND_RECT_TEXT * adj * min(frame[2], frame[3]) if adj else 0.0


def _slot(node, index, paragraph, box, ctx):
    """Typography of paragraph `index` of a free text shape, by the inheritance of patterns.frame()."""
    props, _ = typography.run_candidates([node], ctx.master, ctx.presentation, None)
    first = paragraph.find("a:r/a:rPr", NS)
    chain = ([first] if first is not None else []) + [n for n in paragraph.findall("a:pPr/a:defRPr", NS)] + props
    size = next((int(n.get("sz")) / 100 for n in chain if n.get("sz")), 18.0)
    colour = next((ctx.colour(n.find("a:solidFill/*", NS)) for n in chain if ctx.colour(n.find("a:solidFill/*", NS))), None)
    colour = colour or ctx.colour(node.find("p:style/a:fontRef/*", NS)) or ctx.palette.get(ctx.mapping.get("tx1", "dk1")) or "000000"
    face = next((n.find("a:latin", NS).get("typeface") for n in chain if n.find("a:latin", NS) is not None), None)
    bold = next((n.get("b") in ("1", "true") for n in chain if n.get("b") is not None), False)
    ppr = paragraph.find("a:pPr", NS)
    body = node.find("p:txBody/a:bodyPr", NS)
    insets = [int(body.get(k, str(d))) / EMU if body is not None else d / EMU for k, d in zip(("lIns", "tIns", "rIns", "bIns"), DEFAULT_INSETS)]
    anchor = body.get("anchor", "t") if body is not None else "t"
    return {"shape": node.find("*/p:cNvPr", NS).get("id"), "paragraph": index, "box": box, "sample": _text_of(paragraph).strip(),
            "typography": {"size_pt": size, "font": ctx.typeface(face) or ctx.typeface(ctx.fonts.get("minor")) or "Arial",
                           "color": colour, "bold": bold,
                           "align": {"ctr": "center", "r": "right", "just": "left"}.get(ppr.get("algn") if ppr is not None else None, "left"),
                           "line_spacing": _spacing(ppr, "lnSpc") or {"pct": 100.0}, "space_before": _spacing(ppr, "spcBef") or {"pt": 0.0},
                           "insets": insets, "anchor": anchor if anchor in ("t", "ctr", "b") else "t", "round_adj": _round_adj(node),
                           # the gap before the first paragraph of the shape only with spcFirstLastPara (PowerPoint)
                           "space_first_last": body is not None and body.get("spcFirstLastPara") in ("1", "true")}}


def _primitives(node, box, ctx, package, part, rels, assets):
    """What the preview draws for a top-level shape, in slide coordinates; groups are flattened."""
    kind = _local(node.tag)
    if kind == "grpSp":
        x = _xfrm(node)
        off, ext, ch_off, ch_ext = (x.find("a:" + t, NS) if x is not None else None for t in ("off", "ext", "chOff", "chExt"))
        if any(n is None for n in (off, ext, ch_off, ch_ext)) or int(ch_ext.get("cx")) == 0 or int(ch_ext.get("cy")) == 0:
            return [{"type": "ghost", "box": box, "reason": "group-without-child-frame"}]
        sx, sy = int(ext.get("cx")) / int(ch_ext.get("cx")), int(ext.get("cy")) / int(ch_ext.get("cy"))
        out = []
        for child in node:
            if _local(child.tag) not in KINDS:
                continue
            cbox, flags = _box_flags(child)
            if cbox is None:
                continue
            mapped = [(int(off.get("x")) + (cbox[0] * EMU - int(ch_off.get("x"))) * sx) / EMU,
                      (int(off.get("y")) + (cbox[1] * EMU - int(ch_off.get("y"))) * sy) / EMU, cbox[2] * sx, cbox[3] * sy]
            out += _primitives(child, mapped, ctx, package, part, rels, assets)
        return out
    if kind == "pic":
        blip = node.find("p:blipFill/a:blip", NS)
        rel = rels.get(blip.get(f"{{{R}}}embed")) if blip is not None else None
        target = package.resolve(part, rel.target) if rel is not None and not rel.external else None
        if not target or not target.lower().endswith((".png", ".jpg", ".jpeg")) or target not in package.parts:
            return [{"type": "ghost", "box": box, "reason": "unsupported-picture-format"}]
        raw = package.parts[target]
        key = digest(raw)
        assets.setdefault(key, {"mime": "image/png" if target.lower().endswith(".png") else "image/jpeg", "data": base64.b64encode(raw).decode()})
        src = node.find("p:blipFill/a:srcRect", NS)
        crop = {k: int(src.get(k, "0")) / 100000 if src is not None else 0 for k in ("l", "t", "r", "b")}
        if min(crop.values()) < 0 or crop["l"] + crop["r"] >= 1 or crop["t"] + crop["b"] >= 1:
            return [{"type": "ghost", "box": box, "reason": "invalid-crop"}]
        return [{"type": "image", "box": box, "asset": key, "crop": crop}]
    if kind != "sp":
        return [{"type": "ghost", "box": box, "reason": "unsupported-object-" + kind}]
    fill = _fill(node)
    geometry = node.find("p:spPr/a:prstGeom", NS)
    geometry = geometry.get("prst") if geometry is not None else "custom"
    out = []
    if fill == "solidFill" and geometry in PREVIEW_GEOMETRY:
        colour = node.find("p:spPr/a:solidFill/*", NS)
        alpha = colour.find("a:alpha", NS) if colour is not None else None
        value = ctx.colour(colour)
        if value:
            out.append({"type": "shape", "box": box, "fill": value, "geometry": PREVIEW_GEOMETRY[geometry],
                        "opacity": int(alpha.get("val")) / 100000 if alpha is not None else 1,
                        **({"adj": _round_adj(node)} if geometry == "roundRect" else {})})
    elif fill == "gradFill" and geometry in PREVIEW_GEOMETRY:
        # The preview draws a gradient as the mean of its stops: a surface text may lie on, not an obstacle.
        stops = [s[0] for s in node.findall("p:spPr/a:gradFill/a:gsLst/a:gs", NS) if len(s)]
        colours = [(ctx.colour(s), s.find("a:alpha", NS)) for s in stops]
        colours = [(c, int(a.get("val")) / 100000 if a is not None else 1) for c, a in colours if c]
        if colours:
            mean = "".join(f"{round(sum(int(c[i:i + 2], 16) for c, _ in colours) / len(colours)):02X}" for i in (0, 2, 4))
            out.append({"type": "shape", "box": box, "fill": mean, "geometry": PREVIEW_GEOMETRY[geometry],
                        "opacity": sum(a for _, a in colours) / len(colours), "approximation": "gradient-mean",
                        **({"adj": _round_adj(node)} if geometry == "roundRect" else {})})
        else:
            out.append({"type": "ghost", "box": box, "reason": "unsupported-fill-or-line"})
    elif fill or _line_visible(node):
        out.append({"type": "ghost", "box": box, "reason": "unsupported-fill-or-line"})
    return out


def _slide_nodes(package, part, layout, presentation, assets):
    ctx = _Context(package, part, layout, presentation)
    tree = ctx.slide.find("p:cSld/p:spTree", NS)
    rels = {r.id: r for r in package.rels(part)}
    nodes = []
    for z, node in enumerate(n for n in tree if _local(n.tag) in KINDS):
        box, flags = _box_flags(node)
        nv = node.find("*/p:cNvPr", NS)
        if nv is None or box is None or nv.get("hidden") in ("1", "true"):
            continue
        kind = _local(node.tag)
        ph = node.find("*/p:nvPr/p:ph", NS)
        paragraphs = node.findall("p:txBody/a:p", NS) if kind == "sp" else []
        slots = [_slot(node, i, p, box, ctx) for i, p in enumerate(paragraphs) if _text_of(p).strip()]
        group_text = kind == "grpSp" and any((t.text or "").strip() for t in node.iter(f"{{{A}}}t"))
        fill = "picture" if kind == "pic" else _fill(node) if kind == "sp" else None
        geometry = node.find("p:spPr/a:prstGeom", NS)
        visible = kind != "sp" or bool(fill) or _line_visible(node) or bool(slots)
        nodes.append({"id": nv.get("id"), "kind": kind, "z": z, "box": box, "flags": flags,
                      "placeholder": [ph.get("type", "obj"), ph.get("idx", "0")] if ph is not None else None,
                      "fill": fill, "geometry": geometry.get("prst") if geometry is not None else None,
                      "slots": slots, "group_text": group_text, "visible": visible,
                      "primitives": _primitives(node, box, ctx, package, part, rels, assets)})
    return nodes


def _same_size(a, b):
    return abs(a[2] - b[2]) <= SAME_SIZE * max(a[2], b[2]) and abs(a[3] - b[3]) <= SAME_SIZE * max(a[3], b[3])


def _slot_background(slot, card, nodes, slide_background):
    """Colour under a text slot: the smallest filled shape of the card containing the slot, over the slide background.
    None when that shape is a picture or a fill the preview cannot resolve (contrast unknown)."""
    below = []
    for nid in [card["container"]] + card["members"]:
        node = nodes[nid]
        if contains(node["box"], slot["box"], 3) and (node["fill"] or node["kind"] == "pic"):
            below.append(node)
    if not below:
        return slide_background
    node = min(below, key=lambda n: n["box"][2] * n["box"][3])
    shape = next((p for p in node["primitives"] if p["type"] == "shape"), None)
    if node["kind"] == "pic" or shape is None:
        return None
    return blend(shape["fill"], slide_background, shape["opacity"])


def _drawn_regions(cards, nodes, assets):
    """Limitation 3 of owner decision 49: sets card["drawn"] (slide px of the sample) on a card whose picture differs from the
    picture most cards of the set share (at least two, PNG, the same size of card): the frame of the points drawn in it."""
    by_id = {n["id"]: n for n in nodes}
    pictures = []
    for card in cards:
        prims = by_id[card["container"]]["primitives"]
        if len(prims) == 1 and prims[0]["type"] == "image" and (assets.get(prims[0]["asset"]) or {}).get("mime") == "image/png":
            pictures.append((card, prims[0]))
    counts = {}
    for _, prim in pictures:
        counts[prim["asset"]] = counts.get(prim["asset"], 0) + 1
    plain = max(counts, key=lambda k: (counts[k], k), default=None)
    if plain is None or counts[plain] < 2:
        return
    base = next(c for c, prim in pictures if prim["asset"] == plain)
    decoded = {}
    def image(key):
        if key not in decoded:
            try:
                decoded[key] = png.decode(base64.b64decode(assets[key]["data"]))
            except (ValueError, KeyError):
                decoded[key] = None
        return decoded[key]
    def colour(picture, crop, fx, fy):
        w, h, px = picture
        u = crop["l"] + fx * (1 - crop["l"] - crop["r"])
        v = crop["t"] + fy * (1 - crop["t"] - crop["b"])
        i = (min(h - 1, int(v * h)) * w + min(w - 1, int(u * w))) * 4
        a = px[i + 3] / 255
        return [px[i + k] * a + 255 * (1 - a) for k in range(3)]
    plain_prim = next(prim for c, prim in pictures if c is base)
    for card, prim in pictures:
        if prim["asset"] == plain or any(abs(card["box"][k] - base["box"][k]) > SAME_SIZE * base["box"][k] for k in (2, 3)):
            continue
        own, other = image(prim["asset"]), image(plain)
        if own is None or other is None:
            continue
        x, y, w, h = card["box"]
        edge = DRAWN_EDGE * min(w, h)
        points = []
        py = y + edge
        while py <= y + h - edge:
            px_ = x + edge
            while px_ <= x + w - edge:
                fx, fy = (px_ - x) / w, (py - y) / h
                a, b = colour(own, prim["crop"], fx, fy), colour(other, plain_prim["crop"], fx, fy)
                if max(abs(p - q) for p, q in zip(a, b)) > DRAWN_DIFF:
                    points.append((px_, py))
                px_ += DRAWN_STEP
            py += DRAWN_STEP
        if len(points) >= DRAWN_MIN_POINTS:
            x0, y0 = min(p[0] for p in points) - DRAWN_STEP / 2, min(p[1] for p in points) - DRAWN_STEP / 2
            x1, y1 = max(p[0] for p in points) + DRAWN_STEP / 2, max(p[1] for p in points) + DRAWN_STEP / 2
            card["drawn"] = [round(x0, 1), round(y0, 1), round(x1 - x0, 1), round(y1 - y0, 1)]


def _off_drawing(card, frame, slot, insets, start, limit):
    """Lowest y the text of a slot may reach in a card with a drawing in its picture (_drawn_regions): DRAWN_GAP above the
    drawing where it lies across the text area below the start of the text; a drawing across that start leaves no room."""
    drawn = card.get("drawn")
    if not drawn:
        return limit
    box = _map_box(drawn, card["box"], frame)
    left, _, right, _ = insets
    if min(box[0] + box[2], slot[0] + slot[2] - right) - max(box[0], slot[0] + left) <= 1 or box[1] + box[3] <= start:
        return limit
    return min(limit, box[1] - DRAWN_GAP if box[1] >= start else start)


def _primary(card, nodes, slide_background):
    """Slot for a fact: readable on its own background first, then a slot meant for running text, then the largest size."""
    def key(i):
        slot = card["slots"][i]
        background = _slot_background(slot, card, nodes, slide_background)
        ratio = contrast(slot["typography"]["color"], background) if background else None
        slot["contrast"] = round(ratio, 2) if ratio is not None else None
        readable = ratio is None or ratio >= READABLE
        body = ratio is not None and ratio >= READABLE and bool(BODY_SAMPLE.match(slot["sample"]) or len(slot["sample"]) >= 40)
        return (readable, body, slot["typography"]["size_pt"], -i)
    return max(range(len(card["slots"])), key=key)


def _text_top(slot):
    """Top of the text of a slot: its shape top plus the top inset; None for text not anchored at the top."""
    t = slot["typography"]
    return slot["box"][1] + t["insets"][1] + _text_rect(slot, slot["box"]) if t["anchor"] == "t" else None


def _heading(card, nodes, slide_background):
    """Slot for the label of a fact (A7 v2): the nearest slot before the fact slot, an earlier paragraph of the same shape
    or a shape whose text starts above, that reads on its background; None when the card has none."""
    primary = card["slots"][card["primary"]]
    top = _text_top(primary)
    found = []
    for i, slot in enumerate(card["slots"]):
        if i == card["primary"] or slot["typography"]["anchor"] != "t":
            continue
        if slot["shape"] == primary["shape"]:
            if slot["paragraph"] >= primary["paragraph"]:
                continue
            rank = (1, slot["paragraph"])
        else:
            box, other = slot["box"], primary["box"]
            if top is None or _text_top(slot) >= top - 1 or min(box[0] + box[2], other[0] + other[2]) - max(box[0], other[0]) <= 0:
                continue
            rank = (0, _text_top(slot))
        t = slot["typography"]
        background = _slot_background(slot, card, nodes, slide_background)
        ratio = contrast(t["color"], background) if background else None
        slot["contrast"] = round(ratio, 2) if ratio is not None else None
        large = t["size_pt"] >= LARGE_PT or (t["bold"] and t["size_pt"] >= LARGE_BOLD_PT)
        if ratio is not None and ratio < (READABLE_LARGE if large else READABLE):
            continue
        found.append((rank, i))
    return max(found)[1] if found else None


def _card_set(nodes, width, height, slide_background="FFFFFF"):
    """The largest set of equal cards of a slide, or None."""
    area = width * height
    candidates = [n for n in nodes if not n["placeholder"] and not n["flags"]
                  and (n["kind"] == "pic" or (n["kind"] == "sp" and n["fill"] in ("solidFill", "gradFill", "blipFill", "style")
                                              and n["geometry"] in CARD_GEOMETRY))
                  and CARD_AREA[0] * area <= n["box"][2] * n["box"][3] <= CARD_AREA[1] * area
                  and n["box"][2] >= CARD_SIDE * width and n["box"][3] >= CARD_SIDE * height]
    # The outermost candidate is the container; a candidate inside it (a picture over the card) is a member.
    candidates.sort(key=lambda n: (-n["box"][2] * n["box"][3], n["z"]))
    containers = []
    for n in candidates:
        if not any(contains(c["box"], n["box"], 3) for c in containers):
            containers.append(n)
    cards = []
    for c in containers:
        members = [m for m in nodes if m is not c and not m["placeholder"] and contains(c["box"], m["box"], 3)]
        slots = c["slots"] + [s for m in members for s in m["slots"]]
        if members and any(m["group_text"] for m in members):
            continue  # text inside a group cannot be replaced slot by slot (v1)
        if slots:
            cards.append({"container": c["id"], "box": c["box"], "members": [m["id"] for m in members], "slots": slots})
    sets = []
    for card in cards:
        for group in sets:
            if _same_size(group[0]["box"], card["box"]):
                group.append(card)
                break
        else:
            sets.append([card])
    found = []
    by_id = {n["id"]: n for n in nodes}
    for group in sets:
        if len(group) < 2 or len({len(c["slots"]) for c in group}) != 1:
            continue
        # Cards of one set do not share members and do not overlap.
        members = [m for c in group for m in c["members"]]
        if len(members) != len(set(members)) or any(_overlap(a["box"], b["box"]) > 4 for i, a in enumerate(group) for b in group[i + 1:]):
            continue
        for c in group:
            c["primary"] = _primary(c, by_id, slide_background)
            c["heading"] = _heading(c, by_id, slide_background)
        first = group[0]["slots"][group[0]["primary"]]
        if METRIC.match(first["sample"] or "0") or first["typography"]["size_pt"] > MAX_PRIMARY_PT:
            continue
        group.sort(key=lambda c: (round(c["box"][1] / 12), c["box"][0]))
        rows = {}
        for c in group:
            rows.setdefault(round(c["box"][1] / 12), []).append(c)
        counts = [len(r) for r in rows.values()]
        arrangement = "row" if len(rows) == 1 else "column" if max(counts) == 1 else "grid"
        if arrangement == "grid" and len(set(counts[:-1])) > 1:
            continue
        found.append({"arrangement": arrangement, "rows": len(rows), "cols": max(counts), "cards": group})
    if not found:
        return None
    best = max(found, key=lambda s: (len(s["cards"]), s["cards"][0]["box"][2] * s["cards"][0]["box"][3]))
    if best["arrangement"] in ("row", "column"):
        axis = 0 if best["arrangement"] == "row" else 1
        first = best["cards"][0]["box"]
        used = {m for c in best["cards"] for m in [c["container"]] + c["members"]}
        best["siblings"] = [c for c in cards if c not in best["cards"] and c["container"] not in used
                            and abs(c["box"][1 - axis] - first[1 - axis]) <= 12
                            and all(abs(c["box"][k] - first[k]) <= SIBLING_SIZE * first[k] for k in (2, 3))]
    else:
        best["siblings"] = []
    return best


# G3: a unit is a pictogram (on its backing, if any) above a free text or before it in the line, with no container
# (template C 26-28: "Тезис под тематической иконкой"). A set of equal units is laid out as cards whose container is
# virtual: the union of the unit, grown down to UNIT_BOTTOM of the slide so that a longer fact has room, and not drawn.
UNIT_GAP = 1.5
UNIT_BOTTOM = .8
MARKER, MARKER_GAP = 12, 24


def _icon_before(icon, text):
    """A pictogram right above a text (within its span, a gap under UNIT_GAP of its height) or before it in the line."""
    within = icon[0] >= text[0] - 4 and icon[0] + icon[2] <= text[0] + text[2] + 4
    gap = text[1] - (icon[1] + icon[3])
    if within and 0 <= gap <= UNIT_GAP * icon[3]:
        return gap
    same_line = min(icon[1] + icon[3], text[1] + text[3]) - max(icon[1], text[1]) >= .5 * min(icon[3], text[3])
    gap_x = text[0] - (icon[0] + icon[2])
    return gap_x if same_line and 0 <= gap_x <= max(icon[2], 24) else None


def _unit_set(nodes, width, height):
    """The largest set of equal pictogram units of a slide, as a card set with virtual containers, or None."""
    area = width * height
    texts = [n for n in nodes if n["kind"] == "sp" and n["slots"] and not n["placeholder"] and not n["flags"] and not n["fill"]]
    small = [n for n in nodes if not n["placeholder"] and not n["flags"] and n["box"][2] * n["box"][3] <= area * COMPANION_AREA
             and (n["kind"] == "pic" or n["kind"] == "sp" and n["fill"] and not n["slots"])]
    # The pictogram is the innermost picture: a picture holding another one is its backing (template C 26).
    inner = [i for i in small if i["kind"] == "pic" and not any(o is not i and contains(i["box"], o["box"], 3) and o["box"][2] * o["box"][3] < i["box"][2] * i["box"][3] for o in small)]
    units, used = [], set()
    for text in texts:
        options = [(g, i) for i in inner if i["id"] not in used and (g := _icon_before(i["box"], text["box"])) is not None]
        if not options:
            continue
        icon = min(options, key=lambda o: o[0])[1]
        used.add(icon["id"])
        backing = max((b for b in small if b is not icon and b["id"] not in used and contains(b["box"], icon["box"], 3)
                       and b["box"][2] * b["box"][3] > icon["box"][2] * icon["box"][3]), key=lambda b: b["box"][2] * b["box"][3], default=None)
        if backing is not None:
            used.add(backing["id"])
        members = ([backing] if backing else []) + [icon, text]
        x0, y0 = min(m["box"][0] for m in members), min(m["box"][1] for m in members)
        x1, y1 = max(m["box"][0] + m["box"][2] for m in members), max(m["box"][1] + m["box"][3] for m in members)
        units.append({"members": [m["id"] for m in members], "box": [x0, y0, x1 - x0, y1 - y0], "text": text, "icon": icon})
    # Units of one set have pictograms of one size and texts of one width; the author may make one text taller (template
    # C 27, 28: the third thesis box is 154 px against 130).
    groups = []
    for unit in units:
        for group in groups:
            a, b = group[0]["text"]["box"], unit["text"]["box"]
            if abs(a[2] - b[2]) <= SAME_SIZE * max(a[2], b[2]) and _same_size(group[0]["icon"]["box"], unit["icon"]["box"]):
                group.append(unit)
                break
        else:
            groups.append([unit])
    groups = [g for g in groups if len(g) >= 2]
    if not groups:
        return None
    group = max(groups, key=lambda g: (len(g), g[0]["text"]["box"][2] * g[0]["text"]["box"][3]))
    group.sort(key=lambda u: (round(u["box"][1] / 12), u["box"][0]))
    rows = {}
    for u in group:
        rows.setdefault(round(u["box"][1] / 12), []).append(u)
    counts = [len(r) for r in rows.values()]
    arrangement = "row" if len(rows) == 1 else "column" if max(counts) == 1 else "grid"
    if arrangement == "grid" and len(set(counts[:-1])) > 1:
        return None
    # A row of units grows down to UNIT_BOTTOM of the slide (nothing is drawn there), a column keeps its pitch.
    bottom = max(u["box"][1] + u["box"][3] for u in group)
    if arrangement == "row":
        bottom = max(bottom, height * UNIT_BOTTOM)
    cards, virtual = [], []
    for k, u in enumerate(group):
        box = list(u["box"])
        if arrangement == "row":
            box[3] = bottom - box[1]
        virtual.append({"id": f"unit{k}", "kind": "virtual", "z": -1, "box": box, "flags": {}, "placeholder": None, "fill": None,
                        "geometry": None, "slots": [], "group_text": False, "visible": False, "primitives": []})
        cards.append({"container": f"unit{k}", "box": box, "members": u["members"], "slots": list(u["text"]["slots"])})
    return {"arrangement": arrangement, "rows": len(rows), "cols": max(counts), "cards": cards, "virtual": virtual, "siblings": []}


def _companion(icon, text):
    """An icon next to a text: in the same line with a gap under the icon width, or right above it. A marker (at most MARKER px:
    the 8 px squares of template A) stands up to MARKER_GAP above its text (template A 18: 16 px); left after its text went, it showed
    as a lone dot on an empty panel beside the cube (owner request of 29.09.2026, the list variant on pictures)."""
    same_line = min(icon[1] + icon[3], text[1] + text[3]) - max(icon[1], text[1]) >= .5 * min(icon[3], text[3])
    gap_x = max(text[0] - (icon[0] + icon[2]), icon[0] - (text[0] + text[2]))
    reach = MARKER_GAP if max(icon[2], icon[3]) <= MARKER else icon[3]
    above = min(icon[0] + icon[2], text[0] + text[2]) - max(icon[0], text[0]) > 0 and 0 <= text[1] - (icon[1] + icon[3]) <= reach
    return (same_line and gap_x <= max(icon[2], 24)) or above


def _overlap(a, b):
    return max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])) * max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))


def read_card_samples(package, inventory):
    """Sample slides with a set of equal text cards, and the pictures their preview needs (JSON-compatible)."""
    presentation = parse_xml(package.parts[inventory["presentation_part"]])
    width, height = inventory["width"], inventory["height"]
    samples, assets = [], {}
    for slide in inventory["slides"]:
        found_assets = {}
        nodes = _slide_nodes(package, slide["part"], slide["layout"], presentation, found_assets)
        layout = next((l for l in inventory["layouts"] if l["part"] == slide["layout"]), None)
        background = (layout or {}).get("background_rgb") or "FFFFFF"
        cards = _card_set(nodes, width, height, background)
        kind = "cards"
        if cards is not None:
            _drawn_regions(cards["cards"], nodes, found_assets)
        if cards is None:
            # G3: a slide without cards may hold pictogram units; their containers are virtual nodes of the sample.
            cards = _unit_set(nodes, width, height)
            if cards is None:
                continue
            nodes = nodes + cards.pop("virtual")
            by_id = {n["id"]: n for n in nodes}
            for c in cards["cards"]:
                c["primary"] = _primary(c, by_id, background)
                c["heading"] = _heading(c, by_id, background)
            first = cards["cards"][0]["slots"][cards["cards"][0]["primary"]]
            if METRIC.match(first["sample"] or "0") or first["typography"]["size_pt"] > MAX_PRIMARY_PT:
                continue
            kind = "units"
        in_cards = {n for c in cards["cards"] + cards["siblings"] for n in [c["container"]] + c["members"]}
        roles = {}
        for n in nodes:
            if n["id"] in in_cards:
                roles[n["id"]] = "card"
            elif n["placeholder"]:
                roles[n["id"]] = "placeholder"
            elif n["slots"] or n["group_text"]:
                roles[n["id"]] = "sample-text"   # text of the sample outside the cards is not ours
            elif not n["visible"]:
                roles[n["id"]] = "guide"         # invisible guide rectangles of the author
            else:
                roles[n["id"]] = "decor"
        # An icon paired with sample text outside the cards (an icon and its caption) goes with that text.
        texts = [n for n in nodes if roles[n["id"]] == "sample-text"]
        for n in nodes:
            if roles[n["id"]] == "decor" and n["box"][2] * n["box"][3] <= width * height * COMPANION_AREA and any(_companion(n["box"], t["box"]) for t in texts):
                roles[n["id"]] = "sample-companion"
        samples.append({"slide": slide["number"], "part": slide["part"], "layout": slide["layout"], "kind": kind,
                        "arrangement": cards["arrangement"], "rows": cards["rows"], "cols": cards["cols"], "cards": cards["cards"],
                        "removed_outside_cards": sum(r in ("sample-text", "sample-companion") for r in roles.values()),
                        "siblings": [{k: c[k] for k in ("container", "box", "members")} for c in cards["siblings"]],
                        "nodes": [{**n, "role": roles[n["id"]]} for n in nodes]})
        assets.update(found_assets)
    return samples, assets


# ---- planning -------------------------------------------------------------------------------------------------
# Arrangements of the facts of a slide on a card set (A7 v2, docs/VARIANT_AXIS.md):
#   row     side by side: a row sample, or the first row of a grid;
#   column  one under another where a column sample has them, in a zone of the slide beside the title;
#   stack   one under another across the width of the set: the first column of a grid widened to its first row,
#           or a column sample as wide as a stack;
#   grid    rows and columns: the grid of the sample, or its block of two columns (of three facts the third goes
#           under the first two, across both columns).
ARRANGEMENTS = ("row", "column", "stack", "grid")
# A column of cards narrower than this share of the slide stands in a zone beside the title; a wider one is a stack.
ZONE_WIDTH = .6
# Overlap (px^2) above which a moved card comes under decor of the sample slide that stays in place.
DECOR_OVERLAP = 16
# Gap between text and the bottom of its card that the rendered check asks (CARD_TEXT_MARGIN, apps/generator/quality.js).
CARD_BOTTOM_GAP = 12
# Owner decision 50 (29.09.2026): from three lines on the check asks CARD_THREE_LINES of the size more (qInspectText: bottomGap).
CARD_THREE_LINES = 1.35


def _line_pitch(t, lines):
    """Line height as a multiple of the size. Spacing tighter than single suits the short label a template author
    wrote there, not a sentence that may wrap in another font (journal of stage A), so it becomes single."""
    spacing = t["line_spacing"]
    if "pct" in spacing:
        multiple = 1.2 * spacing["pct"] / 100
        return (multiple, None) if multiple >= 1.2 else (1.2, "line-spacing-below-single")
    return (spacing["pt"] / t["size_pt"] if t["size_pt"] else 1.2), None


def _rows(cards):
    rows = {}
    for c in sorted(cards, key=lambda c: (round(c["box"][1] / 12), c["box"][0])):
        rows.setdefault(round(c["box"][1] / 12), []).append(c)
    return list(rows.values())


def _spread(line, kept, axis):
    """New frames of the kept cards of a row (axis 0) or column (axis 1) over the span of all cards of that line."""
    line = sorted(line, key=lambda c: c["box"][axis])
    starts = [c["box"][axis] for c in line]
    ends = [c["box"][axis] + c["box"][axis + 2] for c in line]
    gaps = [s - e for s, e in zip(starts[1:], ends[:-1])]
    gap = max(0, statistics.median(gaps)) if gaps else 0
    n = len(kept)
    size = (max(ends) - min(starts) - (n - 1) * gap) / n
    frames = {}
    for i, c in enumerate(kept):
        box = list(c["box"])
        box[axis], box[axis + 2] = min(starts) + i * (size + gap), size
        frames[c["container"]] = box
    return frames


def _layout(sample, n, mode, width):
    """Kept cards in reading order and their new frames in an arrangement, or None when the set does not give it."""
    cards, kind, k = sample["cards"], sample["arrangement"], len(sample["cards"])
    rows = _rows(cards)
    siblings = sample.get("siblings", []) if kind in ("row", "column") else []
    def same(kept):
        return {c["container"]: list(c["box"]) for c in kept}
    if mode == "row" and kind in ("row", "grid"):
        line = cards if kind == "row" else rows[0]
        if len(line) < n or (kind == "row" and k - n > MAX_REMOVED):
            return None
        kept = line[:n]
        return kept, (same(kept) if len(line) == n and not siblings else _spread(line + siblings, kept, 0))
    if mode in ("column", "stack") and kind == "column":
        if (mode == "column") != (cards[0]["box"][2] < ZONE_WIDTH * width) or k < n or k - n > MAX_REMOVED:
            return None
        kept = cards[:n]
        return kept, (same(kept) if k == n and not siblings else _spread(cards + siblings, kept, 1))
    x0 = min(c["box"][0] for c in rows[0])
    x1 = max(c["box"][0] + c["box"][2] for c in rows[0])
    if mode == "stack" and kind == "grid":
        line = [r[0] for r in rows]
        if len(line) < n:
            return None
        kept = line[:n]
        frames = same(kept) if len(line) == n else _spread(line, kept, 1)
        for frame in frames.values():
            frame[0], frame[2] = x0, x1 - x0
        return kept, frames
    if mode == "grid" and kind == "grid":
        cols = sample["cols"]
        if n == k or (cols < n <= k and n % cols == 0):
            kept = [c for r in rows for c in r][:n]
            return kept, same(kept)
        if n not in (3, 4) or cols < 3 or len(rows) < 2 or len(rows[1]) < n - 2:
            return None
        first = [r[0] for r in rows]
        heights = same(first[:2]) if len(rows) == 2 else _spread(first, first[:2], 1)
        gap = max(0, statistics.median([b["box"][0] - (a["box"][0] + a["box"][2]) for a, b in zip(rows[0], rows[0][1:])]))
        half = (x1 - x0 - gap) / 2
        top, bottom = heights[first[0]["container"]], heights[first[1]["container"]]
        cells = [(x0, half, top), (x0 + half + gap, half, top)]
        cells += [(x0, x1 - x0, bottom)] if n == 3 else [(x0, half, bottom), (x0 + half + gap, half, bottom)]
        kept = rows[0][:2] + rows[1][:n - 2]
        return kept, {c["container"]: [x, row[1], w, row[3]] for c, (x, w, row) in zip(kept, cells)}
    return None


def _rigid(node):
    """A picture or a group keeps its proportions: stretching it along one axis distorts it (Appendix 1 of the case)."""
    return node["kind"] in ("pic", "grpSp") or node["fill"] == "blipFill"


def _member_frame(box, card, frame, container, rigid):
    """A member keeps its place in the card on each axis: the container takes the frame; a wide member (half the card
    or more) keeps both paddings and takes the whole change of size, so a text slot stays aligned with the marker above
    it; a small or rigid member keeps its size and its offset from the nearer side of the card, or its centre."""
    box = list(box)
    for axis in (0, 1):
        old, new = card[axis + 2], frame[axis + 2]
        offset = box[axis] - card[axis]
        if container:
            box[axis], box[axis + 2] = frame[axis], new
        elif abs(new - old) < .01:
            box[axis] = frame[axis] + offset
        elif box[axis + 2] >= .5 * old and not rigid:
            box[axis], box[axis + 2] = frame[axis] + offset, box[axis + 2] + new - old
        else:
            after = old - offset - box[axis + 2]
            if abs(offset - after) <= .1 * old:
                box[axis] = frame[axis] + (new - box[axis + 2]) / 2
            elif offset <= after:
                box[axis] = frame[axis] + offset
            else:
                box[axis] = frame[axis] + new - (old - offset)
    return box


def _measure(text, t, width, first=True):
    """Size, lines, line pitch and height (paragraph gap included) of a text of typography t in a width, in px.

    first: the text is the first paragraph of its shape, whose gap PowerPoint draws only with spcFirstLastPara."""
    size = t["size_pt"] * 4 / 3
    lines = estimate_lines(text, max(1, width), size)
    pitch, adjustment = _line_pitch(t, lines)
    before = t["space_before"]
    gap = before["pt"] * 4 / 3 if "pt" in before else before["pct"] / 100 * size
    gap_first = not first or t.get("space_first_last", True)
    return {"size": size, "lines": lines, "line_height": pitch, "paragraph_gap": gap, "space_first_last": bool(gap_first),
            "height": lines * size * pitch + (gap if gap_first else 0), "adjustments": [adjustment] if adjustment else []}


def _insets(slot, own, frame):
    """Insets of the text of a slot in a frame of its shape, and the text body insets to write when they differ from the
    sample (None otherwise). Text of the card shape itself whose right inset is under half the left one (template B 5 and
    10: 0 against 11 px) reaches the card edge once a sentence stands there, so its right inset repeats the left one.
    The text rectangle of a rounded shape adds to every side: the layout counts it, the written body insets do not
    (PowerPoint applies it itself)."""
    l, top, r, b = slot["typography"]["insets"]
    written = None
    if own and r < l / 2:
        r = l
        written = [l, top, r, b]
    g = _text_rect(slot, frame)
    return [l + g, top + g, r + g, b + g], written


def _limit(slot, start, here, skip, frame, own, insets):
    """Lowest y the text of a slot may reach. The slot grows down to the card bottom minus the card padding (the left
    offset of the slot in the card, as authors repeat it at the bottom; for text of the card shape itself its bottom
    inset, not less than the bottom gap the rendered check asks) and stops above kept members below the start of its
    text; a member entirely above that start (an icon in the top inset of a card) is no obstacle, a member the slot lies
    on (a heading band) keeps the text on it, and a member across the start makes the slot unusable."""
    left, _, right, bottom = insets
    limit = frame[1] + frame[3] - (max(bottom, CARD_BOTTOM_GAP) if own else max(4, slot[0] - frame[0]))
    for member, box in here.items():
        if member in skip or box[1] + box[3] <= start:
            continue
        if min(box[0] + box[2], slot[0] + slot[2] - right) - max(box[0], slot[0] + left) <= 1:
            continue
        if contains(box, slot, 3):
            limit = min(limit, box[1] + box[3])
        else:
            limit = min(limit, box[1] - 4 if box[1] >= start else start)
    return limit


def _card_texts(card, fact, frame, here, label, margin=False):
    """Text entries of one card, the fact in its primary slot and, when label is given, the label in its heading slot;
    `here` (frames of the kept shapes) is updated for slots that grow. None when the text does not fit.

    An entry carries the element box and insets: text in a shape of its own covers that shape, text of the card shape
    itself covers only its text area below the top inset (an icon may sit in that inset); two paragraphs of one shape
    (a label above its fact) are two elements stacked in that area, and together they cover it."""
    container = card["container"]
    primary = card["slots"][card["primary"]]
    heading = card["slots"][card["heading"]] if label is not None else None
    entries = []
    def entry(slot, text, part, box, insets, role, body, limit=None):
        written = body[1]
        entries.append({**part, "shape": slot["shape"], "paragraph": slot["paragraph"], "fact_id": fact["id"], "text": text,
                        "role": role, "own": slot["shape"] == container, "typography": slot["typography"],
                        "box": box, "insets": insets, "body_insets": written, "limit": limit,
                        "adjustments": part["adjustments"] + (["right-inset-as-left"] if written else [])
                        + (["round-rect-text-area"] if slot["typography"].get("round_adj") else [])})
    label_part = None
    if heading is not None and heading["shape"] != primary["shape"]:
        th = heading["typography"]
        own = heading["shape"] == container
        slot = list(here[heading["shape"]])
        body = _insets(heading, own, slot)
        l, top, r, b = body[0]
        label_part = _measure(label, th, slot[2] - l - r)
        start = slot[1] + top
        limit = _off_drawing(card, frame, slot, body[0], start, _limit(slot, start, here, {container, heading["shape"]}, frame, own, body[0]))
        if start + label_part["height"] + (0 if own else b) > limit + .5:
            return None
        if own:
            entry(heading, label, label_part, [slot[0], start, slot[2], label_part["height"]], [l, 0, r, 0], "label", body)
        else:
            slot[3] = max(slot[3], top + label_part["height"] + b)
            here[heading["shape"]] = slot
            entry(heading, label, label_part, slot, [l, top, r, b], "label", body)
    t = primary["typography"]
    own = primary["shape"] == container
    slot = list(here[primary["shape"]])
    body = _insets(primary, own, slot)
    l, top, r, b = body[0]
    below_label = heading is not None and heading["shape"] == primary["shape"]
    part = _measure(fact["text"], t, slot[2] - l - r, first=not below_label)
    above = 0
    if heading is not None and heading["shape"] == primary["shape"]:
        label_part = _measure(label, heading["typography"], slot[2] - l - r)
        above = label_part["height"]
    start = slot[1] + top
    limit = _off_drawing(card, frame, slot, body[0], start, _limit(slot, start, here, {container, primary["shape"]}, frame, own, body[0]))
    if start + above + part["height"] + (0 if own else b) > limit + .5:
        return None
    if margin:
        # Owner decision 50 (29.09.2026, K3-8/10/11, template B 23): the text of a card shape stands where its anchor puts
        # it (centred there) in its area — the card below the top inset, or its own slot as it grows — and the check of the
        # rendering asks CARD_BOTTOM_GAP between its last line and the bottom of the card, plus CARD_THREE_LINES of the size
        # from three lines on: three centred lines in a card of 129 px left 29 px of the 37 asked (CARD_TEXT_MARGIN).
        height = above + part["height"]
        if own:
            top_edge, bottom_edge = start, frame[1] + frame[3] - b
        else:
            top_edge, bottom_edge = start, slot[1] + max(slot[3], top + height + b) - b
        anchor = t.get("anchor", "t")
        last = top_edge + height if anchor == "t" else bottom_edge if anchor == "b" else (top_edge + bottom_edge + height) / 2
        need = CARD_BOTTOM_GAP + (part["size"] * CARD_THREE_LINES if part["lines"] >= 3 else 0)
        if last > frame[1] + frame[3] - need + .5:
            return None
    if own:
        bottom = frame[1] + frame[3]
    else:
        slot[3] = max(slot[3], top + above + part["height"] + b)
        here[primary["shape"]] = slot
        bottom = slot[1] + slot[3]
    if above:
        # Label and fact are paragraphs of one shape: the label on top, the fact below it.
        if own:
            entry(heading, label, label_part, [slot[0], start, slot[2], above], [l, 0, r, 0], "label", body)
        else:
            entry(heading, label, label_part, [slot[0], slot[1], slot[2], top + above], [l, top, r, 0], "label", body)
        entry(primary, fact["text"], part, [slot[0], start + above, slot[2], bottom - start - above], [l, 0, r, 0 if own else b], "fact", body,
              None if own else limit)
    elif own:
        entry(primary, fact["text"], part, [slot[0], start, slot[2], bottom - start], [l, 0, r, b], "fact", body)
    else:
        # A text shape of its own may grow down to `limit` (its bottom inset included) when its text grows (quality.js).
        entry(primary, fact["text"], part, slot, [l, top, r, b], "fact", body, limit)
    return entries


def readable_cards(sample):
    """Whether every card of a sample holds a fact in a readable slot: the slot _primary chose has a contrast of at least
    READABLE on its own fill, or a background the preview cannot resolve (a picture: contrast unknown, as before). deck 37 3 is
    a set of photo frames labelled "Photo" in white on light grey (1.51:1): the best slot of such a card is still unreadable,
    so the set is not a set of text cards (proposals of 24.09.2026, E1)."""
    return all(c["slots"][c["primary"]].get("contrast") is None or c["slots"][c["primary"]]["contrast"] >= READABLE
               for c in sample["cards"])


def plan_cards(sample, facts, width, height, mode=None):
    """Frames of the kept shapes of a sample slide and the text of every used card, or None when the facts do not fit.

    facts: [{"id", "text", "label"?}], one per card, in the reading order of the cards. mode: an arrangement of
    ARRANGEMENTS; None tries the arrangement of the sample first, then the others. Labels are placed only when every
    fact has one and every kept card has a readable heading slot where it fits; otherwise the cards hold the facts alone.
    """
    if mode is None:
        own = {"row": "row", "column": "column" if sample["cards"][0]["box"][2] < ZONE_WIDTH * width else "stack", "grid": "grid"}[sample["arrangement"]]
        for option in (own,) + tuple(a for a in ARRANGEMENTS if a != own):
            plan = plan_cards(sample, facts, width, height, option)
            if plan is not None:
                return plan
        return None
    n = len(facts)
    if n < 1:
        return None
    layout = _layout(sample, n, mode, width)
    if layout is None:
        return None
    kept, frames = layout
    nodes = {nd["id"]: nd for nd in sample["nodes"]}
    # A picture as the card itself cannot change its size without distortion.
    for card in kept:
        frame = frames[card["container"]]
        if _rigid(nodes[card["container"]]) and any(abs(frame[i] - card["box"][i]) > .02 * card["box"][i] for i in (2, 3)):
            return None
    removed = {m for c in sample["cards"] + sample.get("siblings", []) if c not in kept for m in [c["container"]] + c["members"]}
    # Decor of the sample slide stays in place: a card must not move under it.
    for nd in sample["nodes"]:
        if nd["role"] == "decor" and nd["id"] not in removed:
            for card in kept:
                if _overlap(nd["box"], frames[card["container"]]) > DECOR_OVERLAP >= _overlap(nd["box"], card["box"]):
                    return None
    labelled = all(f.get("label") for f in facts) and all(c.get("heading") is not None for c in kept)

    # Owner decision 50 (29.09.2026, template B 16, rebuild of K3-8): the heading slot of a card stands on a band the preview
    # cannot draw (a gradient in round2SameRect: a ghost); PowerPoint showed a dark label on the blue band, the HTML and the PDF a
    # dark label on the black card (LOW_RENDERED_CONTRAST, BRAND_ASSET_OVERLAP). Labels go only where the preview draws what
    # lies under them; the cards then hold the facts alone.
    def on_ghost(card):
        heading = card["slots"][card["heading"]]
        return any(any(q["type"] == "ghost" for q in nodes[m]["primitives"]) and contains(nodes[m]["box"], heading["box"])
                   for m in [card["container"]] + card["members"])
    labelled = labelled and not any(on_ghost(c) for c in kept)
    # Owner decision 48 (28.09.2026): a large picture that stands, the same file, in every card of the sample (template A 15: a 3D
    # disc in each of the four cards) is an ornament of the template, not an illustration of the text of one card; it is
    # carried over as decor and not asked of question 6 (context.pictures). One that differs from card to card goes, below.
    def large(member):
        return _rigid(nodes[member]) and nodes[member]["box"][2] * nodes[member]["box"][3] > COMPANION_AREA * width * height

    def pictures_of(member):
        return {p["asset"] for p in nodes[member]["primitives"] if p["type"] == "image"}
    ornaments = set.intersection(*[set().union(*[pictures_of(m) for m in c["members"] if large(m)] or [set()]) for c in sample["cards"]]) \
        if len(sample["cards"]) >= 2 else set()
    for labels in ((True, False) if labelled else (False,)):
        placed, texts, clear = {}, [], set()
        for card, fact in zip(kept, facts):
            frame = frames[card["container"]]
            primary = card["slots"][card["primary"]]
            used = {primary["shape"]} | ({card["slots"][card["heading"]]["shape"]} if labels else set())
            # Only the used slots keep text: other text shapes of the card go, other text of the container is cleared.
            drop = {s["shape"] for s in card["slots"]} - used - {card["container"]}
            # A picture of the card larger than a pictogram is an illustration of the sample text (template A 15 and 28: 3D
            # objects of 6.6 and 11.8 % of the slide; pictograms are at most 2.2 %); a fact of the brief is not about it
            # (question 6 of the case answered "no", K3), so it is not carried over.
            drop |= {m for m in card["members"] if large(m) and not (pictures_of(m) and pictures_of(m) <= ornaments)}
            if card["container"] not in used and any(s["shape"] == card["container"] for s in card["slots"]):
                clear.add(card["container"])
            here = {}
            for member in [card["container"]] + card["members"]:
                # Invisible guide rectangles of the author (no fill, line or text) are not carried over.
                if member not in drop and (member == card["container"] or nodes[member]["visible"]):
                    here[member] = _member_frame(nodes[member]["box"], card["box"], frame, member == card["container"], _rigid(nodes[member]))
            # The check of the rendering measures the margins of a text in the smallest shape that holds it: a card shape, not a
            # card picture (quality.js, qParent).
            entries = _card_texts(card, fact, frame, here, fact["label"] if labels else None, margin=not _rigid(nodes[card["container"]]))
            if entries is None:
                break
            placed.update(here)
            texts += entries
        else:
            shapes = []
            for nd in sample["nodes"]:
                if nd["id"] in placed:
                    shapes.append({"shape": nd["id"], "frame": placed[nd["id"]], "clear_text": nd["id"] in clear})
                elif nd["role"] == "decor" and nd["id"] not in removed:
                    shapes.append({"shape": nd["id"], "frame": list(nd["box"]), "clear_text": False})
            respread = any(any(abs(a - b) > .01 for a, b in zip(frames[c["container"]], c["box"])) for c in kept)
            return {"sample": sample["slide"], "part": sample["part"], "layout": sample["layout"], "arrangement": mode,
                    "cards_total": len(sample["cards"]), "cards_used": n, "respread": respread, "labels": labels,
                    "cards": [{"container": card["container"], "fact": fact["id"]} for card, fact in zip(kept, facts)],
                    "shapes": shapes, "texts": texts, "ornaments": sorted(ornaments)}
    return None


def _map_box(box, source, frame):
    sx = frame[2] / source[2] if source[2] else 1
    sy = frame[3] / source[3] if source[3] else 1
    return [frame[0] + (box[0] - source[0]) * sx, frame[1] + (box[1] - source[1]) * sy, box[2] * sx, box[3] * sy]


def card_elements(sample, plan, assets, loaded_fonts, fallback_font, width, height, icons=None):
    """Elements of vsp.document/2 for a planned card slide in the z-order of the sample, and the reasons of an
    incomplete preview. Every element of one source shape carries the same "clone" binding; the exporter writes
    that shape once.

    icons (G3): {card container: {"asset": the sample pictogram, "data", "icon", "meaning", "ink"}}; the picture of that
    asset in that card shows the chosen pictogram instead, and its element says so (the exporter replaces the media)."""
    nodes = {nd["id"]: nd for nd in sample["nodes"]}
    card_of = {m: c["container"] for c in sample["cards"] for m in [c["container"]] + c["members"]}
    texts = {}
    for t in plan["texts"]:
        texts.setdefault(t["shape"], []).append(t)
    elements, incomplete = [], []
    for n, item in enumerate(plan["shapes"]):
        node = nodes[item["shape"]]
        frame = [round(v, 2) for v in item["frame"]]
        # The source box lets the audit compare proportions of the clone with the reference (IMAGE_DISTORTED).
        binding = {"kind": "clone", "source": {"part": sample["part"], "shape": node["id"], "box": [round(v, 2) for v in node["box"]]}, "frame": frame}
        if item["clear_text"]:
            binding["clear_text"] = True
        for m, prim in enumerate(node["primitives"]):
            box = _map_box(prim["box"], node["box"], frame)
            # Clip to the canvas; a primitive entirely outside it is not drawn (PowerPoint clips the same way).
            x0, y0, x1, y1 = max(0, box[0]), max(0, box[1]), min(width, box[0] + box[2]), min(height, box[1] + box[3])
            if x1 - x0 < .5 or y1 - y0 < .5:
                continue
            base = {"id": f"clone{n}-{m}", **dict(zip(("x", "y", "w", "h"), [round(v, 2) for v in (x0, y0, x1 - x0, y1 - y0)])),
                    "fact_ids": [], "template_decoration": True, "locked": True, "binding": binding,
                    "source_style": {"kind": "sample-card", "part": sample["part"], "shape": node["id"], "slide": sample["slide"]}}
            if prim["type"] == "shape":
                elements.append({**base, "type": "shape", "geometry": prim["geometry"], "fill": prim["fill"], "opacity": prim["opacity"],
                                 **({"adj": round(prim["adj"], 5)} if prim.get("adj") is not None else {})})
                if prim.get("approximation"):
                    incomplete.append(prim["approximation"])
            elif prim["type"] == "image":
                asset = assets[prim["asset"]]
                # A small picture (marker, icon, logo) is protected from text; a large one is a surface text may lie on.
                small = (x1 - x0) * (y1 - y0) < width * height * .05
                chosen = (icons or {}).get(card_of.get(node["id"]))
                if chosen and chosen["asset"] == prim["asset"]:
                    elements.append({**base, "type": "image", "mime": "image/png", "data": chosen["data"], "crop": {"l": 0, "t": 0, "r": 0, "b": 0},
                                     "protected_asset": small, "icon": {k: chosen[k] for k in ("icon", "fact", "meaning", "ink", "asset")}})
                else:
                    # A picture of the decor of the sample slide beside the cards (template A 18: the cube) is an ornament of the
                    # template too, not an illustration of a fact: image_auditor called the cube «не относится к теме» on 2 slides
                    # of 9 in the live run of the owner's request of 29.09.2026 (the list variant on pictures).
                    elements.append({**base, "type": "image", "mime": asset["mime"], "data": asset["data"], "crop": prim["crop"], "protected_asset": small,
                                     **({"ornament": True} if prim["asset"] in plan.get("ornaments", ()) or node.get("role") == "decor" else {})})
            else:
                # Small undrawable decor (an EMF icon) is an invisible obstacle, as a logo is; a large one is a surface or a
                # background, and an obstacle of that size would flag every text on it (as in journal item 21 of stage A).
                if (x1 - x0) * (y1 - y0) <= width * height * GHOST_MAX_AREA:
                    elements.append({**base, "type": "shape", "geometry": "rect", "fill": "FFFFFF", "opacity": 0, "ghost": True})
                incomplete.append(prim["reason"])
        for text in texts.get(node["id"], []):
            t = text["typography"]
            family = t["font"] if t["font"] in loaded_fonts else fallback_font
            box = [round(v, 2) for v in text["box"]]
            adjustments = [{"reason": a} for a in text["adjustments"]]
            if family != t["font"]:
                adjustments.append({"reason": "font-not-loaded", "from": t["font"], "to": family})
            sample_text = next((s["sample"] for s in node["slots"] if s["paragraph"] == text["paragraph"]), "")
            label = text["role"] == "label"
            # A label is a heading of its fact written by stage B, not a fact: no fact ids, checked against the brief label.
            extra = {"fact_ids": [], "label_of": text["fact_id"], "locked": True} if label else {"fact_ids": [text["fact_id"]]}
            if not label and text.get("limit") is not None and text["limit"] > box[1] + box[3] + .5:
                extra["grow_bottom"] = round(text["limit"], 2)
            elements.append({"id": text["fact_id"] + ("-label" if label else ""), "type": "text", "text": text["text"],
                             **dict(zip(("x", "y", "w", "h"), box)),
                             "font": family, "font_size": round(text["size"], 2), "color": t["color"], "bold": t["bold"], "align": t["align"],
                             "line_height": round(text["line_height"], 4), **extra,
                             "source_style": {"kind": "sample-card-text", "part": sample["part"], "shape": node["id"], "paragraph": text["paragraph"],
                                              "slide": sample["slide"], "sample_text": sample_text},
                             "reference_box": box, "content_role": {"role": "label" if label else "body", "source": "sample-card"},
                             "style_adjustments": adjustments,
                             "inset": [round(v, 2) for v in text["insets"]], "anchor": t["anchor"], "paragraph_gap": round(text["paragraph_gap"], 2),
                             "space_first_last": text["space_first_last"],
                             "binding": {**binding, "paragraph": text["paragraph"], "size_pt": t["size_pt"], "line_spacing": t["line_spacing"],
                                         "own_text": text["own"], **({"body_insets": [round(v, 2) for v in text["body_insets"]]} if text["body_insets"] else {})}})
    return elements, sorted(set(incomplete))


# ---- Item 31 of the plan (25.09.2026): the preview of mandatory decor carried from the sample slides ----------------------
def _line_preview(node, box, ctx):
    """A horizontal or vertical line (cxnSp) as a thin filled shape, as the preview has no lines; any other line is a ghost."""
    x, y, w, h = box
    ln = node.find("p:spPr/a:ln", NS)
    solid = ln.find("a:solidFill", NS) if ln is not None else None
    ref = node.find("p:style/a:lnRef", NS)
    colour = (ctx.colour(solid[0]) if solid is not None and len(solid) else
              ctx.colour(ref[0]) if ref is not None and len(ref) and ref.get("idx", "0") != "0" else None)
    thickness = max(1.0, int(ln.get("w")) / EMU) if ln is not None and ln.get("w") else 1.0
    if colour is None or (w >= .5 and h >= .5):
        return {"type": "ghost", "box": box, "reason": "unsupported-line"}
    frame = [x - thickness / 2, y, thickness, h] if w < .5 else [x, y - thickness / 2, w, thickness]
    return {"type": "shape", "box": frame, "fill": colour, "geometry": "rect", "opacity": 1}


def _decor_preview(node, box, flags, ctx, package, part, rels, assets, running):
    """What the preview draws for one shape of mandatory decor, in slide coordinates: pictures and filled shapes as A7 draws
    them (_primitives), lines as thin shapes, text with the typography of its first paragraph and the turn of its frame.
    A turned picture or filled shape is an invisible obstacle (ghost): the preview does not turn them."""
    from .reference import visual_box
    kind = _local(node.tag)
    turn = (int(flags["rot"]) / 60000) % 360 if flags.get("rot") else 0
    if kind == "grpSp":
        x = _xfrm(node)
        off, ext, ch_off, ch_ext = (x.find("a:" + t, NS) if x is not None else None for t in ("off", "ext", "chOff", "chExt"))
        if turn or any(n is None for n in (off, ext, ch_off, ch_ext)) or int(ch_ext.get("cx")) == 0 or int(ch_ext.get("cy")) == 0:
            return [{"type": "ghost", "box": visual_box(box, flags.get("rot")), "reason": "turned-or-unframed-group"}]
        sx, sy = int(ext.get("cx")) / int(ch_ext.get("cx")), int(ext.get("cy")) / int(ch_ext.get("cy"))
        out = []
        for child in node:
            if _local(child.tag) not in KINDS:
                continue
            cbox, cflags = _box_flags(child)
            if cbox is None:
                continue
            mapped = [(int(off.get("x")) + (cbox[0] * EMU - int(ch_off.get("x"))) * sx) / EMU,
                      (int(off.get("y")) + (cbox[1] * EMU - int(ch_off.get("y"))) * sy) / EMU, cbox[2] * sx, cbox[3] * sy]
            out += _decor_preview(child, mapped, cflags, ctx, package, part, rels, assets, running)
        return out
    if kind == "cxnSp":
        return [_line_preview(node, visual_box(box, flags.get("rot")), ctx)]
    if turn and kind == "pic":
        return [{"type": "ghost", "box": visual_box(box, flags.get("rot")), "reason": "turned-picture"}]
    out = []
    if kind == "sp" and turn and (_fill(node) or _line_visible(node)):
        out.append({"type": "ghost", "box": visual_box(box, flags.get("rot")), "reason": "turned-shape"})
    elif not (kind == "sp" and turn):
        out += [p for p in _primitives(node, box, ctx, package, part, rels, assets) if p["type"] != "ghost" or p["box"][2] * p["box"][3] > 0]
    paragraphs = node.findall("p:txBody/a:p", NS) if kind == "sp" else []
    lines = ["".join(t.text or "" for t in p.iter(f"{{{A}}}t")).strip() for p in paragraphs]
    if any(lines):
        first = next(i for i, value in enumerate(lines) if value)
        slot = _slot(node, first, paragraphs[first], box, ctx)
        text = "\n".join(_text_of(p) for p in paragraphs).strip("\n")
        joined = " ".join(" ".join(lines).split())
        out.append({"type": "text", "box": visual_box(box, flags.get("rot")), "frame": [round(v, 2) for v in box], "rotation": round(turn, 2),
                    "text": text, "lines": [v for v in lines if v], "typography": slot["typography"],
                    "running_title": bool(running) and joined.casefold() == running.casefold()})
    return out


def brand_decor_preview(package, carrier):
    """Preview of every transferable item of carrier["brand_decor"] (reference.brand_decor) from the shape of its source
    slide; the PPTX carries that shape itself (binding "furniture"). Sets item["preview"] and returns the pictures by SHA-256."""
    decor = carrier.get("brand_decor") or {}
    assets = {}
    if not any(i["transferable"] for i in decor.get("items", [])):
        return assets
    presentation = parse_xml(package.parts[carrier["presentation_part"]])
    layouts = {s["part"]: s["layout"] for s in carrier["slides"]}
    for item in decor["items"]:
        if not item["transferable"]:
            continue
        part, shape = item["source"]["part"], str(item["source"]["shape"])
        ctx = _Context(package, part, layouts[part], presentation)
        rels = {r.id: r for r in package.rels(part)}
        node = next(n for n in ctx.slide.find("p:cSld/p:spTree", NS)
                    if n.find("*/p:cNvPr", NS) is not None and n.find("*/p:cNvPr", NS).get("id") == shape)
        box, flags = _box_flags(node)
        item["preview"] = _decor_preview(node, box, flags, ctx, package, part, rels, assets, item.get("running_title"))
    return assets
