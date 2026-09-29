"""Inventory of what a reference deck offers: masters, layouts, placeholders, usage.

Only standard PresentationML semantics and observed geometry are used (layout
`type`, placeholder `type`/`idx`, inheritance layout -> master, where the sample
slides actually put content). No keywords, file names or brands.

Moved from spikes/template_carrier/inventory.py (21.09.2026). Differences: the
result is JSON-compatible (free_text_colours is a list per layout) and every
layout placeholder carries its inherited `typography`, resolved by the same
rules as the reference patterns (vsp_core.typography).
"""
from __future__ import annotations

import hashlib
import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
from difflib import SequenceMatcher

from .opc import Package, parse_xml
from .patterns import rgb
from .roles import leading_instructions
from .typography import color_context, run_candidates, style_role, theme_fonts

NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
P = "{%s}" % NS["p"]
R = "{%s}" % NS["r"]
# Cloned furniture is re-serialized; registered prefixes keep that XML conventional.
for _prefix, _uri in {**NS, "a14": "http://schemas.microsoft.com/office/drawing/2010/main",
                      "a16": "http://schemas.microsoft.com/office/drawing/2014/main",
                      "p14": "http://schemas.microsoft.com/office/powerpoint/2010/main",
                      "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
                      "asvg": "http://schemas.microsoft.com/office/drawing/2016/SVG/main"}.items():
    ET.register_namespace(_prefix, _uri)
EMU_PER_PX = 9525
TEXT_TYPES = {"body", "obj", "subTitle"}
TITLE_TYPES = {"title", "ctrTitle"}
SERVICE_TYPES = {"dt", "ftr", "sldNum", "hdr"}
# A placeholder without its own geometry falls back to the master placeholder
# of the compatible type.
MASTER_FALLBACK = {"ctrTitle": "title", "subTitle": "body", "obj": "body", "tbl": "body", "chart": "body",
                   "dgm": "body", "media": "body", "clipArt": "body", "pic": "body"}
DEFAULT_CLR_MAP = {"bg1": "lt1", "tx1": "dk1", "bg2": "lt2", "tx2": "dk2"}


def _xfrm_box(xfrm):
    if xfrm is None:
        return None
    off, ext = xfrm.find("a:off", NS), xfrm.find("a:ext", NS)
    if off is None or ext is None:
        return None
    return [int(off.get("x")) / EMU_PER_PX, int(off.get("y")) / EMU_PER_PX,
            int(ext.get("cx")) / EMU_PER_PX, int(ext.get("cy")) / EMU_PER_PX]


def _box(shape):
    return _xfrm_box(shape.find("p:spPr/a:xfrm", NS))


def _is_placeholder(shape):
    return shape.find("p:nvSpPr/p:nvPr/p:ph", NS) is not None


def _placeholders(root):
    result = []
    for shape in root.findall("p:cSld/p:spTree/p:sp", NS):
        ph = shape.find("p:nvSpPr/p:nvPr/p:ph", NS)
        if ph is None:
            continue
        size = shape.find("p:txBody/a:lstStyle/a:lvl1pPr/a:defRPr", NS)
        xfrm = shape.find("p:spPr/a:xfrm", NS)
        body = shape.find("p:txBody/a:bodyPr", NS)
        result.append({
            "type": ph.get("type", "obj"), "idx": ph.get("idx", "0"),
            # The slide must repeat the layout's identity attributes exactly.
            "raw": {k: ph.get(k) for k in ("type", "idx", "orient", "sz") if ph.get(k) is not None},
            "box": _box(shape), "rotated": xfrm is not None and xfrm.get("rot", "0") != "0",
            "vertical": ph.get("orient") == "vert" or (body is not None and body.get("vert") not in (None, "horz")),
            "size_pt": int(size.get("sz")) / 100 if size is not None and size.get("sz") else None,
            "name": shape.find("p:nvSpPr/p:cNvPr", NS).get("name", ""),
        })
    return result


def _static_boxes(root):
    """Top-level non-placeholder shapes, pictures and groups: inherited decor."""
    boxes = []
    tree = root.find("p:cSld/p:spTree", NS)
    for node in tree if tree is not None else []:
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "sp" and not _is_placeholder(node):
            box = _box(node)
        elif tag in ("pic", "cxnSp"):
            box = _box(node)
        elif tag == "grpSp":
            box = _xfrm_box(node.find("p:grpSpPr/a:xfrm", NS))
        elif tag == "graphicFrame":
            box = _xfrm_box(node.find("p:xfrm", NS))
        else:
            continue
        if box:
            boxes.append(box)
    return boxes


def _master_style_size(master, ph_type):
    style = "titleStyle" if ph_type in TITLE_TYPES else "bodyStyle" if ph_type in TEXT_TYPES else "otherStyle"
    node = master.find(f"p:txStyles/p:{style}/a:lvl1pPr/a:defRPr", NS)
    return int(node.get("sz")) / 100 if node is not None and node.get("sz") else None


def _theme_colors(package, master_part, master):
    colors = {}
    theme_parts = package.related(master_part, "theme")
    if theme_parts:
        scheme = parse_xml(package.parts[theme_parts[0]]).find("a:themeElements/a:clrScheme", NS)
        for entry in scheme if scheme is not None else []:
            if len(entry):
                colors[entry.tag.rsplit("}", 1)[-1]] = entry[0].get("val") if entry[0].tag.endswith("srgbClr") else entry[0].get("lastClr")
    mapping = dict(DEFAULT_CLR_MAP)
    clr_map = master.find("p:clrMap", NS)
    if clr_map is not None:
        mapping.update(clr_map.attrib)
    return {"scheme": colors, "map": mapping}


def resolve_scheme(theme, name):
    """RGB of a scheme colour name as seen on slides of this master."""
    return theme["scheme"].get(theme["map"].get(name, name))


def _card_styles(package, slides, width, height):
    """Most frequent filled rectangle/rounded rectangle style on the sample slides."""
    found = Counter()
    for slide in slides:
        root = parse_xml(package.parts[slide["part"]])
        for shape in root.iter(P + "sp"):
            geometry = shape.find("p:spPr/a:prstGeom", NS)
            fill = shape.find("p:spPr/a:solidFill", NS)
            box = _box(shape)
            if geometry is None or fill is None or not len(fill) or box is None or _is_placeholder(shape):
                continue
            if geometry.get("prst") not in ("rect", "roundRect") or not 0.03 <= box[2] * box[3] / (width * height) <= 0.5:
                continue
            if box[2] >= width * 0.9 or box[3] >= height * 0.9:
                continue  # a band across the slide is furniture, not a card
            colour = fill[0]
            kind = colour.tag.rsplit("}", 1)[-1]
            if kind not in ("srgbClr", "schemeClr"):
                continue
            adjust = geometry.find("a:avLst/a:gd", NS)
            transforms = tuple((t.tag.rsplit("}", 1)[-1], t.get("val")) for t in colour)
            found[(geometry.get("prst"), kind, colour.get("val"), transforms,
                   adjust.get("fmla") if adjust is not None else None)] += 1
    return [{"prst": k[0], "color_kind": k[1], "color": k[2], "transforms": list(k[3]), "adjust": k[4], "count": n}
            for k, n in found.most_common(3)]


def _first_run_overrides(shape):
    """Direct formatting of the first non-empty run: what the author set by hand."""
    for paragraph in shape.findall("p:txBody/a:p", NS):
        for run in paragraph.findall("a:r", NS):
            text = run.find("a:t", NS)
            if text is None or not (text.text or "").strip():
                continue
            found = {}
            props, para_props = run.find("a:rPr", NS), paragraph.find("a:pPr", NS)
            if props is not None:
                if props.get("b") is not None:
                    found["bold"] = props.get("b") in ("1", "true")
                if props.get("sz"):
                    found["size_pt"] = int(props.get("sz")) / 100
                fill = props.find("a:solidFill", NS)
                if fill is not None and len(fill) and fill[0].tag.rsplit("}", 1)[-1] in ("srgbClr", "schemeClr"):
                    found["color"] = (fill[0].tag.rsplit("}", 1)[-1], fill[0].get("val"),
                                      tuple((c.tag.rsplit("}", 1)[-1], c.get("val")) for c in fill[0]))
                latin = props.find("a:latin", NS)
                if latin is not None and latin.get("typeface") and not latin.get("typeface").startswith("+"):
                    found["font"] = latin.get("typeface")
            if para_props is not None and para_props.get("algn"):
                found["align"] = para_props.get("algn")
            return found
    return None


def _background_rgb(root, theme):
    """Solid background declared by a slide-like part, or None (picture, gradient, inherited)."""
    background = root.find("p:cSld/p:bg", NS)
    if background is None:
        return None
    node = background.find("p:bgPr/a:solidFill", NS)
    node = node if node is not None else background.find("p:bgRef", NS)
    if node is None or not len(node):
        return None
    colour = node[0]
    kind = colour.tag.rsplit("}", 1)[-1]
    if len(colour):  # tints and shades would need colour arithmetic; unknown is safer than wrong
        return None
    return colour.get("val") if kind == "srgbClr" else resolve_scheme(theme, colour.get("val")) if kind == "schemeClr" else None


def _free_text_colours(package, slides):
    """Explicit colours of free text, per layout: what is readable on that layout's background."""
    by_layout: dict[str, Counter] = {}
    for slide in slides:
        root = parse_xml(package.parts[slide["part"]])
        counter = by_layout.setdefault(slide["layout"], Counter())
        for shape in root.iter(P + "sp"):
            if _is_placeholder(shape):
                continue
            # Text inside a filled shape is coloured for that shape, not for the slide background.
            if shape.find("p:spPr/a:solidFill", NS) is not None or shape.find("p:spPr/a:gradFill", NS) is not None:
                continue
            for run in shape.findall("p:txBody/a:p/a:r", NS):
                text, fill = run.find("a:t", NS), run.find("a:rPr/a:solidFill", NS)
                if text is None or fill is None or not len(fill):
                    continue
                kind = fill[0].tag.rsplit("}", 1)[-1]
                if kind in ("srgbClr", "schemeClr"):
                    key = (kind, fill[0].get("val"), tuple((c.tag.rsplit("}", 1)[-1], c.get("val")) for c in fill[0]))
                    counter[key] += len(text.text or "")
    # JSON has no tuple keys: a list per layout, most characters first, then by colour.
    return {layout: [{"color": [k[0], k[1], [list(t) for t in k[2]]], "chars": n}
                     for k, n in sorted(counter.items(), key=lambda i: (-i[1], i[0][0], i[0][1] or "", tuple((a, b or "") for a, b in i[0][2])))]
            for layout, counter in by_layout.items()}


def _free_text_size(package, slides):
    """Median explicit size of free text runs that carry a sentence, in points."""
    sizes = []
    for slide in slides:
        root = parse_xml(package.parts[slide["part"]])
        for shape in root.iter(P + "sp"):
            if _is_placeholder(shape):
                continue
            for run in shape.findall("p:txBody/a:p/a:r", NS):
                text, props = run.find("a:t", NS), run.find("a:rPr", NS)
                if text is not None and len((text.text or "").strip()) >= 40 and props is not None and props.get("sz"):
                    sizes.append(int(props.get("sz")) / 100)
    sizes.sort()
    return sizes[len(sizes) // 2] if len(sizes) >= 3 else None


def _observed_styles(package, slides):
    """Hand formatting that the reference applies consistently on top of its masters.

    A property is adopted only when at least 60 % of the sampled shapes of a role
    set it directly and at least 60 % of those agree on the value.
    """
    samples = {"title": [], "body": [], "free": []}
    for slide in slides:
        root = parse_xml(package.parts[slide["part"]])
        for shape in root.iter(P + "sp"):
            found = _first_run_overrides(shape)
            if found is None:
                continue
            ph = shape.find("p:nvSpPr/p:nvPr/p:ph", NS)
            kind = ph.get("type", "obj") if ph is not None else None
            role = "free" if ph is None else "title" if kind in TITLE_TYPES else "body" if kind in TEXT_TYPES else None
            if role in ("title", "body"):
                # Authors also move and fill placeholders by hand (a title turned into a colour band).
                box = _box(shape)
                if box and shape.find("p:spPr/a:xfrm", NS).get("rot", "0") == "0":
                    found["box"] = tuple(round(v / 4) * 4 for v in box)
                fill = shape.find("p:spPr/a:solidFill", NS)
                if fill is not None and len(fill) and fill[0].tag.rsplit("}", 1)[-1] in ("srgbClr", "schemeClr"):
                    found["shape_fill"] = (fill[0].tag.rsplit("}", 1)[-1], fill[0].get("val"),
                                           tuple((c.tag.rsplit("}", 1)[-1], c.get("val")) for c in fill[0]))
            if role:
                samples[role].append(found)
    observed = {}
    for role, items in samples.items():
        adopted = {}
        for prop in ("color", "bold", "align", "font", "size_pt", "box", "shape_fill"):
            values = Counter(i[prop] for i in items if prop in i)
            total = sum(values.values())
            if items and total >= max(2, 0.6 * len(items)):
                value, count = values.most_common(1)[0]
                if count >= 0.6 * total:
                    adopted[prop] = value
        observed[role] = {"samples": len(items), "adopted": adopted}
    # Body and free-text sizes depend on the content of a particular slide.
    for role in ("body", "free"):
        observed[role]["adopted"].pop("size_pt", None)
    # A body moved by hand follows the pictures of one particular slide.
    observed["body"]["adopted"].pop("box", None)
    return observed


def _furniture(package, slides, width, height):
    """Decor the author repeats by hand on the slides themselves (bands, logos).

    Many real decks keep their identity there and not in the layouts. An element
    qualifies when it has no text, and the same picture or the same filled
    rectangle sits at the same place on at least half of the slides.
    """
    seen: dict[tuple, dict] = {}
    for slide in slides:
        root = parse_xml(package.parts[slide["part"]])
        rels = {r.id: r for r in package.rels(slide["part"])}
        tree = root.find("p:cSld/p:spTree", NS)
        for order, node in enumerate(tree):
            tag = node.tag.rsplit("}", 1)[-1]
            if tag not in ("sp", "pic") or (tag == "sp" and _is_placeholder(node)):
                continue
            if any((text.text or "").strip() for text in node.iter("{%s}t" % NS["a"])):
                continue
            box = _box(node)
            if not box or box[2] <= 0 or box[3] <= 0:
                continue
            rounded = tuple(round(v / 4) * 4 for v in box)
            fill_key = None
            if tag == "pic":
                blip = node.find("p:blipFill/a:blip", NS)
                rel = rels.get(blip.get(R + "embed")) if blip is not None else None
                if rel is None or rel.external:
                    continue
                signature = ("pic", package.resolve(slide["part"], rel.target), rounded)
            else:
                geometry, fill = node.find("p:spPr/a:prstGeom", NS), node.find("p:spPr/a:solidFill", NS)
                if geometry is None or fill is None or not len(fill):
                    continue
                fill_key = (fill[0].tag.rsplit("}", 1)[-1], fill[0].get("val"),
                            tuple((c.tag.rsplit("}", 1)[-1], c.get("val")) for c in fill[0]))
                signature = ("sp", geometry.get("prst"), fill_key, rounded)
            item = seen.setdefault(signature, {
                "xml": ET.tostring(node, encoding="unicode"), "box": list(box), "kind": tag, "fill": fill_key, "order": order,
                "rels": {i: (rels[i].type, package.resolve(slide["part"], rels[i].target) if not rels[i].external else rels[i].target,
                             rels[i].external) for i in {v for n in node.iter() for k, v in n.attrib.items() if k.startswith(R)} if i in rels},
                "slides": set()})
            item["slides"].add(slide["number"])
    total = len(slides)
    area = width * height
    opening = slides[0]["number"] if slides else 1
    result = []
    for item in seen.values():
        x, y, w, h = item["box"]
        repeated = len(item["slides"]) >= max(2, 0.5 * total)
        edge = x <= 2 or y <= 2 or x + w >= width - 2 or y + h >= height - 2
        # An opening slide may carry its own edge band that no other slide repeats.
        cover_only = opening in item["slides"] and item["kind"] == "sp" and edge and w * h <= area * 0.3
        if w * h <= area * 0.6 and (repeated or cover_only):
            result.append({**item, "slides": sorted(item["slides"]), "repeated": repeated,
                           "on_cover": opening in item["slides"]})
    return sorted(result, key=lambda i: i["order"])


# ---- Item 31 of the plan (25.09.2026): mandatory decor of the sample slides ---------------------------------------------
# What the non-cover sample slides show again and again at one place is the look of the template, whether the slide holds it
# or its layout or master draws it: pictures, filled shapes, lines, groups, and text too (the band of deck 25 names the
# university; the header of deck 21 repeats the name of the presentation). A derived slide carries it: by standing on a layout
# that draws it, or by a copy of the slide shape. Measurement and reasons: analysis/style-experiments/20260925-brand-decor.
# A slide shape is carried only from the edge band of the slide (BRAND_EDGE of its width or height): text repeated in the
# middle of the sample slides is an instruction of the template author (deck 29: two of three slides), not decor.
BRAND_EDGE = 0.15
# Text of a template that asks to be replaced is no decor ("Optional Date", "Подзаголовок", "Щелкните, чтобы…").
SAMPLE_TEXT = re.compile(r"^\W*(optional|date|дата|subtitle|подзаголовок|title|заголовок|text|текст|name|имя|click|щелкните|щёлкните|"
                         r"нажмите|введите|вставьте|insert|add|добавьте|lorem)\b", re.I)
_QUARTER = 5400000


def visual_box(box, rot):
    """What a shape turned by 90 or 270 degrees draws: its extent swapped around its centre."""
    x, y, w, h = box
    if round((int(rot or 0) % 21600000) / _QUARTER) % 2 == 1:
        return [x + w / 2 - h / 2, y + h / 2 - w / 2, h, w]
    return list(box)


def _plain_text(node):
    return " ".join(" ".join((t.text or "") for t in node.iter("{%s}t" % NS["a"])).split())


def _visible_fill(sppr, style):
    """Kind of the visible fill of a shape without text: its own fill, a fill of the shape style, None for none."""
    for child in sppr if sppr is not None else []:
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "noFill":
            return None
        if tag in ("solidFill", "gradFill", "blipFill", "pattFill"):
            return child
    ref = style.find("a:fillRef", NS) if style is not None else None
    return ref if ref is not None and ref.get("idx", "0") != "0" else None


def _visible_line(sppr, style):
    ln = sppr.find("a:ln", NS) if sppr is not None else None
    if ln is not None and ln.find("a:noFill", NS) is not None:
        return None
    if ln is not None and ln.find("a:solidFill", NS) is not None:
        return ln
    ref = style.find("a:lnRef", NS) if style is not None else None
    return ref if ref is not None and ref.get("idx", "0") != "0" else None


def _key(node):
    """Comparable form of an XML fragment: tag, attributes and children, without relationship ids."""
    if node is None:
        return None
    return (node.tag.rsplit("}", 1)[-1], tuple(sorted((k, v) for k, v in node.attrib.items() if not k.startswith(R))),
            tuple(_key(c) for c in node))


class _DecorReader:
    """Signatures of the top-level shapes of slides, layouts and masters as decor."""

    def __init__(self, package, width, height, cover_title):
        self.package, self.width, self.height, self.cover_title = package, width, height, cover_title
        self.digests = {}

    def media(self, part, rels, rid):
        rel = rels.get(rid) if rid else None
        if rel is None or rel.external:
            return None
        target = self.package.resolve(part, rel.target)
        if target not in self.digests:
            data = self.package.parts.get(target)
            self.digests[target] = hashlib.sha256(data).hexdigest() if data is not None else None
        return self.digests[target]

    def signature(self, node, part, rels, number, on_slide):
        """(signature, visual box, kind, running title) of a shape shown as decor, or None: a placeholder (a slide placeholder
        holds content; PowerPoint draws no placeholder of a layout or master on a slide), a hidden, empty or invisible shape,
        a picture of 60 % of the slide or more (a background). Text equal to the slide number is a page number: the product
        places it itself (C2), so its kind is "page"."""
        tag = node.tag.rsplit("}", 1)[-1]
        nv = node.find("*/p:cNvPr", NS)
        if nv is None or nv.get("hidden") in ("1", "true"):
            return None
        area = self.width * self.height
        if tag == "grpSp":
            xfrm = node.find("p:grpSpPr/a:xfrm", NS)
            box = _xfrm_box(xfrm)
            if not box or box[2] * box[3] <= 0:
                return None
            children = [self.signature(c, part, rels, number, on_slide) for c in node
                        if c.tag.rsplit("}", 1)[-1] in ("sp", "pic", "cxnSp", "grpSp")]
            children = [c for c in children if c]
            if not children:
                return None
            vbox = visual_box(box, xfrm.get("rot"))
            running = any(c[3] for c in children)
            return ("group", _rounded(vbox), tuple(sorted(repr(c[0]) for c in children))), vbox, "group", running
        xfrm = node.find("p:spPr/a:xfrm", NS) if tag in ("sp", "pic", "cxnSp") else None
        box = _xfrm_box(xfrm)
        if not box:
            return None
        vbox = visual_box(box, xfrm.get("rot"))
        if tag == "pic":
            blip = node.find("p:blipFill/a:blip", NS)
            digest = self.media(part, rels, blip.get(R + "embed") if blip is not None else None)
            if box[2] < 1 or box[3] < 1 or vbox[2] * vbox[3] >= area * 0.6 or digest is None:
                return None
            return ("picture", digest, _rounded(vbox)), vbox, "picture", False
        sppr, style = node.find("p:spPr", NS), node.find("p:style", NS)
        if tag == "cxnSp":
            line = _visible_line(sppr, style)
            return (("line", _rounded(vbox), _key(line)), vbox, "line", False) if line is not None else None
        ph = node.find("p:nvSpPr/p:nvPr/p:ph", NS)
        text = _plain_text(node)
        if ph is not None:
            return (("page", _rounded(vbox)), vbox, "page", False) if on_slide and ph.get("type") == "sldNum" else None
        if text:
            if text == str(number) or any(f.get("type") == "slidenum" for f in node.iter("{%s}fld" % NS["a"])):
                return ("page", _rounded(vbox)), vbox, "page", False
            if self.cover_title and text.casefold() == self.cover_title.casefold():
                return ("running-title", _rounded(vbox)), vbox, "text", True
            return ("text", _rounded(vbox), text, xfrm.get("rot", "0")), vbox, "text", False
        fill, line = _visible_fill(sppr, style), _visible_line(sppr, style)
        if (fill is None and line is None) or vbox[2] * vbox[3] >= area * 0.6:
            return None
        if fill is not None and fill.tag.endswith("}blipFill"):
            blip = fill.find("a:blip", NS)
            fill_key = ("blip", self.media(part, rels, blip.get(R + "embed") if blip is not None else None))
        else:
            fill_key = _key(fill)
        geometry = node.find("p:spPr/a:prstGeom", NS)
        return (("shape", geometry.get("prst") if geometry is not None else "custom", fill_key, _key(line), _rounded(vbox)),
                vbox, "shape", False)

    def items(self, part, number=0):
        """{signature: (visual box, kind, shape id, running title)} of the top-level shapes of a part."""
        root = parse_xml(self.package.parts[part])
        rels = {r.id: r for r in self.package.rels(part)}
        tree = root.find("p:cSld/p:spTree", NS)
        on_slide = part.startswith("ppt/slides/")
        found = {}
        for node in tree if tree is not None else []:
            if node.tag.rsplit("}", 1)[-1] not in ("sp", "pic", "cxnSp", "grpSp"):
                continue
            s = self.signature(node, part, rels, number, on_slide)
            if s and s[0] not in found:
                found[s[0]] = (s[1], s[2], node.find("*/p:cNvPr", NS).get("id"), s[3])
        return found


def _rounded(box):
    return tuple(round(v / 4) * 4 for v in box)


def _cover_title(package, part):
    """Text of the title of the first slide: its title placeholder, otherwise its largest text."""
    root = parse_xml(package.parts[part])
    best = None
    for sp in root.iter(P + "sp"):
        text = _plain_text(sp)
        if not text:
            continue
        ph = sp.find("p:nvSpPr/p:nvPr/p:ph", NS)
        if ph is not None and ph.get("type") in TITLE_TYPES:
            return text
        size = max((int(r.get("sz")) for r in sp.iter("{%s}rPr" % NS["a"]) if r.get("sz")), default=0)
        if best is None or size > best[0]:
            best = (size, text)
    return best[1] if best else None


def _peripheral(box, width, height):
    x, y, w, h = box
    return (x + w <= width * BRAND_EDGE + 1 or x >= width * (1 - BRAND_EDGE) - 1
            or y + h <= height * BRAND_EDGE + 1 or y >= height * (1 - BRAND_EDGE) - 1)


# Separators of the segments of a footer line ("deck 26 Public | Let's build… 2020 | <title of the talk> | May 15, 2020").
FOOTER_SEGMENTS = re.compile(r"(\s*[|•·]\s*)")


def close_to_title(text, title):
    """Text that names the presentation: at least 0.8 similar to its cover title (deck 26: "…with deck 26’s developer tools" in the
    footer, "…with deck 26 developer tools" on the cover), case and runs of spaces aside."""
    a, b = " ".join((text or "").split()).casefold(), " ".join((title or "").split()).casefold()
    return bool(a and b) and SequenceMatcher(None, a, b).ratio() >= 0.8


def _running_footers(package, part, cover_title, width, height):
    """Shapes a layout or master draws itself in the edge band of the slide (no placeholder) whose text has a segment naming
    the sample presentation (close_to_title): [{"shape": id, "text": text}]."""
    if not cover_title:
        return []
    tree = parse_xml(package.parts[part]).find("p:cSld/p:spTree", NS)
    found = []
    for node in tree if tree is not None else []:
        if node.tag.rsplit("}", 1)[-1] != "sp" or node.find("p:nvSpPr/p:nvPr/p:ph", NS) is not None:
            continue
        box, text = _xfrm_box(node.find("p:spPr/a:xfrm", NS)), _plain_text(node)
        if box and text and _peripheral(box, width, height) and any(close_to_title(s, cover_title) for s in FOOTER_SEGMENTS.split(text)[::2]):
            found.append({"shape": node.find("p:nvSpPr/p:cNvPr", NS).get("id"), "text": text[:200]})
    return found


def _group_texts(tree):
    """(shape, box in px on the slide) of the text shapes inside the groups of a shape tree; rotated or flipped groups are
    left out."""
    found = []

    def walk(node, transform):
        for child in node:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "grpSp":
                xfrm = child.find("p:grpSpPr/a:xfrm", NS)
                if xfrm is None or any(xfrm.get(k, "0") not in ("0", "false") for k in ("rot", "flipH", "flipV")):
                    continue
                off, ext, ch_off, ch_ext = (xfrm.find("a:" + t, NS) for t in ("off", "ext", "chOff", "chExt"))
                if any(n is None for n in (off, ext, ch_off, ch_ext)) or not int(ch_ext.get("cx")) or not int(ch_ext.get("cy")):
                    continue
                g = [int(off.get("x")), int(off.get("y")), int(ext.get("cx")), int(ext.get("cy"))]
                c = [int(ch_off.get("x")), int(ch_off.get("y")), int(ch_ext.get("cx")), int(ch_ext.get("cy"))]

                def inner(b, g=g, c=c, outer=transform):
                    moved = [g[0] + (b[0] - c[0]) * g[2] / c[2], g[1] + (b[1] - c[1]) * g[3] / c[3], b[2] * g[2] / c[2], b[3] * g[3] / c[3]]
                    return outer(moved) if outer else moved
                walk(child, inner)
            elif tag == "sp" and transform is not None:
                xfrm = child.find("p:spPr/a:xfrm", NS)
                off, ext = (xfrm.find("a:off", NS), xfrm.find("a:ext", NS)) if xfrm is not None else (None, None)
                if off is not None and ext is not None:
                    box = transform([int(off.get("x")), int(off.get("y")), int(ext.get("cx")), int(ext.get("cy"))])
                    found.append((child, [v / EMU_PER_PX for v in box]))
    walk(tree if tree is not None else [], None)
    return found


def slide_tone(colour):
    """Tone of a slide from the colour of its title: a light title (relative luminance above 0.5) stands on a dark slide."""
    if not isinstance(colour, str) or len(colour) != 6:
        return "light"
    channels = [int(colour[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
    return "dark" if .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2] > .5 else "light"


def brand_decor(package, slides, layouts, width, height, tones=None):
    """Mandatory decor of the sample slides (vsp.brand-decor/1).

    items: what at least max(2, half) of the non-cover sample slides of one tone show at one place (tones: the slides take
    the tone of their title colour, `tones` by slide part, "light" otherwise: deck 21 has a dark header on its light slides and
    a white one on its dark slides), with where it comes from (levels: slide, layout, master). An item only the slides hold
    is carried by a copy of its shape (transferable) when it lies in the edge band of the slide and is no page number; items
    a layout or master draws are carried by the choice of the layout. layouts: the items every layout draws itself (with its
    master when it shows it); layout_tones: the tone of every layout by the colour of its title placeholder. blocked: per
    layout, the transferable items that would cover its title or text frames, with the items that overlap them (a logo on
    a band goes with the band). slides: the items every sample slide shows itself (on the slide, not by its layout);
    occurrences: the id of the shape of a transferable item on each of those slides, so a slide built on that sample carries
    its own shape whole.
    """
    tones = tones or {}
    cover_title = _cover_title(package, slides[0]["part"]) if slides else None
    reader = _DecorReader(package, width, height, cover_title)
    by_part = {l["part"]: l for l in layouts}
    # Evidence for content slides: sample slides after the first that do not stand on a layout of a cover (deck 03 repeats
    # its intro slide with "Optional Date" on two cover layouts).
    rest = [s for s in slides[1:] if s["layout"] not in by_part or classify_layout(by_part[s["layout"]], width, height)["kind"] != "cover"]
    shows = {part: parse_xml(package.parts[part]).get("showMasterSp", "1") not in ("0", "false") for part in by_part}
    owned = {}

    def owner(part, number=0):
        key = (part, number)
        if key not in owned:
            owned[key] = reader.items(part, number)
        return owned[key]

    def drawn_by(layout_part):
        found = dict(owner(layout_part))
        if shows.get(layout_part, True) and by_part[layout_part]["master"] in package.parts:
            for k, v in owner(by_part[layout_part]["master"]).items():
                found.setdefault(k, v)
        return found

    layout_tones = {}
    for part, layout in by_part.items():
        title = classify_layout(layout, width, height)["title"]
        layout_tones[part] = slide_tone(((title or {}).get("typography") or {}).get("color"))
    groups = {}
    for slide in rest:
        groups.setdefault(tones.get(slide["part"]) or layout_tones.get(slide["layout"], "light"), []).append(slide)
    levels, info, occurrences, mandatory = {}, {}, {}, {}
    for tone, members in sorted(groups.items()):
        threshold = max(2, math.ceil(len(members) / 2))
        counts = Counter()
        for slide in members:
            root = parse_xml(package.parts[slide["part"]])
            own = owner(slide["part"], slide["number"])
            shown = {k: ("slide", v) for k, v in own.items()}
            for k, v in own.items():
                occurrences.setdefault(k, {})[slide["part"]] = v[2]
            if slide["layout"] in by_part:
                for k, v in owner(slide["layout"]).items():
                    shown.setdefault(k, ("layout", v))
                if root.get("showMasterSp", "1") not in ("0", "false") and shows.get(slide["layout"], True):
                    for k, v in owner(by_part[slide["layout"]]["master"]).items():
                        shown.setdefault(k, ("master", v))
            for k, (level, v) in shown.items():
                counts[k] += 1
                levels.setdefault(k, set()).add(level)
                info.setdefault(k, v)
        # A tone of a few special slides says nothing about content slides (template A: its dark slides are a title variant and
        # two "Спасибо!" slides, 3 of 53); a tone counts from a fifth of the non-cover slides.
        if len(members) >= max(2, 0.2 * len(rest)):
            for k, c in counts.items():
                if c >= threshold and info[k][1] != "page":
                    mandatory.setdefault(k, set()).add(tone)
    items, ids = [], {}
    for k in sorted(mandatory, key=lambda k: (info[k][0][1], info[k][0][0], repr(k))):
        box, kind, _, running = info[k]
        slide_only = levels[k] == {"slide"}
        transferable = slide_only and _peripheral(box, width, height) and not (kind == "text" and not running and SAMPLE_TEXT.match(k[2] if k[0] == "text" else ""))
        if slide_only and not transferable:
            continue
        ids[k] = f"brand{len(items) + 1}"
        first = next(s for s in rest if s["part"] in occurrences.get(k, {})) if transferable else None
        items.append({"id": ids[k], "kind": kind, "box": [round(v, 2) for v in box], "tones": sorted(mandatory[k]),
                      "levels": sorted(levels[k]), "transferable": transferable, "running_title": cover_title if running else None,
                      "source": {"part": first["part"], "shape": occurrences[k][first["part"]], "slide": first["number"]} if first else None,
                      "occurrences": dict(sorted(occurrences.get(k, {}).items())) if transferable else {}})
    # What every layout draws of the items, where it draws it. A variant of an item counts as the item (_shifted): deck 04
    # draws the accent under the title at 143 px on its layouts for one-line titles and at 182 px on "Bullet Slide - 2 Line
    # Title"; deck 05 draws its accent in the colour of its gold design on one layout and of its white design on another.
    drawn = {}
    for part in by_part:
        here = drawn_by(part)
        found = {ids[k]: [round(v, 2) for v in info[k][0]] for k in here if k in ids}
        for k, item_id in ids.items():
            if item_id in found or k[0] not in ("line", "shape", "picture"):
                continue
            for other, value in here.items():
                if _shifted(k, other, height):
                    found[item_id] = [round(v, 2) for v in value[0]]
                    break
        drawn[part] = dict(sorted(found.items(), key=lambda i: int(i[0][5:])))
    # A layout the sample slides that show an item stand on takes it by definition (deck 18: the band lies over the lowest
    # 17 px of the body frame of "Title and Content" on all seven slides); on another layout a text or title frame over a
    # quarter of the item leaves no place for it, nor for the items overlapping it (the logo of deck 18 lies on its band).
    homes = {}
    for slide in rest:
        for k in owner(slide["part"], slide["number"]):
            if k in ids:
                homes.setdefault(ids[k], set()).add(slide["layout"])
    moving = [i for i in items if i["transferable"]]
    linked = {i["id"]: {j["id"] for j in moving if _overlap_area(i["box"], j["box"]) > 0} for i in moving}
    blocked = {}
    for part, layout in by_part.items():
        content = [p["box"] for p in layout["placeholders"] if p["box"] and p["type"] not in SERVICE_TYPES]
        hits = {i["id"] for i in moving if i["id"] not in drawn[part] and part not in homes.get(i["id"], ())
                and any(_overlap_area(i["box"], b) > 0.25 * i["box"][2] * i["box"][3] for b in content)}
        while True:
            grown = hits | {j for i in hits for j in linked[i]}
            if grown == hits:
                break
            hits = grown
        if hits:
            blocked[part] = sorted(hits, key=lambda i: int(i[5:]))
    carried = {}
    for slide in rest:
        found = [ids[k] for k in owner(slide["part"], slide["number"]) if k in ids]
        if found:
            carried[slide["part"]] = sorted(found, key=lambda i: int(i[5:]))
    # Item 35, H3 (holdout 25.09.2026, deck 08): text a layout draws itself — no placeholder, outside the edge band of the
    # slide, no item of the mandatory decor — is text of its sample slide ("LEVEL 3 WILL:" of the layouts of the level
    # slides; the labels "CHALLENGE:", "SOLUTION:" of the form layouts of deck 15), and a derived slide on that layout shows it as
    # its own. On a layout of a cover it is the slogan of the brand (deck 01, deck 06); the composer asks only content slides.
    # H5 (deck 33): a slide number the layout, or the master it shows, draws itself — a text box with the number field, not
    # a placeholder — is on every slide of that layout; the product sets no number of its own there.
    sample_texts, pages = {}, {}
    for part in by_part:
        texts = [{"text": k[2][:120], "box": [round(v, 2) for v in value[0]], "shape": value[2]} for k, value in owner(part).items()
                 if k[0] == "text" and k not in ids and not _peripheral(value[0], width, height)]
        if texts:
            sample_texts[part] = texts
        numbers = sorted([round(v, 2) for v in value[0]] for value in drawn_by(part).values() if value[1] == "page")
        if numbers:
            pages[part] = numbers
    # Item 37, cause 6 (holdout-next 25.09.2026, deck 26): a line a layout or master draws itself in the edge band that
    # names the sample presentation shows the title, event and date of another talk on every derived slide ("deck 26 Public |
    # Let's build… 2020 | Curing Amnesia… | May 15, 2020 | <number>" on all eight content layouts). With its slide number
    # field the reader above takes it for a page number, H3 leaves the edge band out; the exporter rewrites it
    # (carrier_export.rewrite_running_footers).
    footers = {}
    for part in sorted(set(by_part) | {l["master"] for l in by_part.values() if l.get("master") in package.parts}):
        shapes = _running_footers(package, part, cover_title, width, height)
        if shapes:
            footers[part] = shapes
    # Session 16, D1 (holdout-next3, «Газпром»; analysis/style-experiments/20260926-hn3-m7/process/research-gazprom.md): a
    # layout without a title placeholder draws the title of its sample slide as a text box of its own in the title zone
    # ("Описание инновационной продукции (2/2)"), under the title of every slide on it. That box is text of the sample even
    # in the edge band (layout 12: its bottom at 77 px of 540), and on such a layout so are the texts inside its groups (the
    # footnote "1 В соответствии с приказом Минэнерго…"). Page numbers, running footers and the mandatory decor stay out.
    def zone(box):
        return box[1] < height * 0.22 and box[1] + box[3] <= height * 0.25

    def own_text(node, box, known):
        sid = node.find("p:nvSpPr/p:cNvPr", NS).get("id")
        text = _plain_text(node)
        if not text or not box or sid in known or node.find("p:nvSpPr/p:nvPr/p:ph", NS) is not None:
            return None
        if any((f.get("type") or "").startswith(("slidenum", "datetime")) for f in node.iter("{%s}fld" % NS["a"])):
            return None
        if any(all(abs(u - v) <= 6 for u, v in zip(i["box"], box)) for i in items):
            return None
        return {"text": text[:120], "box": [round(v, 2) for v in box], "shape": sid}

    for part, layout in by_part.items():
        if any(p["type"] in TITLE_TYPES for p in layout["placeholders"]):
            continue
        known = {t["shape"] for t in sample_texts.get(part, [])} | {f["shape"] for f in footers.get(part, [])}
        tree = parse_xml(package.parts[part]).find("p:cSld/p:spTree", NS)
        heads = [t for node in (tree if tree is not None else []) if node.tag.rsplit("}", 1)[-1] == "sp"
                 for box in [_box(node)] if box and zone(box) and box[2] >= width * 0.28 for t in [own_text(node, box, known)] if t]
        if heads or any(zone(t["box"]) for t in sample_texts.get(part, [])):
            known |= {t["shape"] for t in heads}
            heads += [t for node, box in _group_texts(tree) for t in [own_text(node, box, known)] if t]
        if heads:
            sample_texts.setdefault(part, []).extend(heads)
    return {"schema": "vsp.brand-decor/1", "slides_considered": len(rest),
            "groups": {tone: len(members) for tone, members in sorted(groups.items())}, "cover_title": cover_title,
            "items": items, "layouts": drawn, "layout_tones": layout_tones, "blocked": blocked, "slides": carried,
            "sample_texts": sample_texts, "pages": pages, **({"running_footers": footers} if footers else {})}


def _shifted(a, b, height):
    """Two decor signatures that stand for the same decor on different layouts: a line or filled shape moved vertically by
    at most a tenth of the slide (deck 04: the accent under one-line and two-line titles), a filled shape of another colour,
    a picture of another colour at the same place (deck 05: the accent of its gold and of its white design)."""
    near = lambda p, q: all(abs(u - v) <= 4 for u, v in zip(p, q))   # rounded to 4 px: neighbours may round apart
    if a[0] != b[0] or a == b:
        return False
    if a[0] == "picture":
        return near(a[2], b[2])
    if a[0] not in ("line", "shape"):
        return False
    box_a, box_b = (a[1], b[1]) if a[0] == "line" else (a[4], b[4])
    if a[0] == "shape" and a[1] == b[1] and near(box_a, box_b):
        return True
    rest_a, rest_b = (a[2:], b[2:]) if a[0] == "line" else (a[1:4], b[1:4])
    return (rest_a == rest_b and box_a[0] == box_b[0] and box_a[2:] == box_b[2:] and box_a != box_b
            and abs(box_a[1] - box_b[1]) <= height * 0.1)


def _overlap_area(a, b):
    return max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])) * max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))


def _placeholder_shapes(root):
    """Placeholder shapes in the order of _placeholders(root)."""
    return [s for s in root.findall("p:cSld/p:spTree/p:sp", NS) if s.find("p:nvSpPr/p:nvPr/p:ph", NS) is not None]


def _first(nodes, attribute):
    return next((n.get(attribute) for n in nodes if n is not None and n.get(attribute) is not None), None)


def _spacing(levels, tag, default):
    """a:lnSpc / a:spcBef of the first paragraph level that sets it: {"pct": percent} or {"pt": points}."""
    for level in levels:
        node = level.find(f"a:{tag}", NS) if level is not None else None
        if node is None:
            continue
        pct, pts = node.find("a:spcPct", NS), node.find("a:spcPts", NS)
        if pct is not None and pct.get("val") is not None:
            return {"pct": int(pct.get("val")) / 1000}
        if pts is not None and pts.get("val") is not None:
            return {"pt": int(pts.get("val")) / 100}
    return dict(default)


def _placeholder_fill(layout_shape, master_shape, theme, master, layout):
    """Solid fill of a layout placeholder itself (its spPr, else that of its master placeholder), as RGB, or None.

    Item 37, cause 8 (holdout-next 25.09.2026, Rosseti): PowerPoint draws it under the text of a slide placeholder that
    sets no fill of its own (the white plate between the date and the region of the cover); the preview does not."""
    palette, mapping = color_context(theme, master, (layout,))
    for shape in (layout_shape, master_shape):
        sppr = shape.find("p:spPr", NS) if shape is not None else None
        for child in sppr if sppr is not None else []:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "solidFill":
                return rgb(child[0], palette, mapping) if len(child) else None
            if tag in ("noFill", "gradFill", "blipFill", "pattFill", "grpFill"):
                return None
    return None


def placeholder_typography(ph_type, layout_shape, master_shape, master, presentation, theme, layout=None, fallback="Arial"):
    """Typography a new slide inherits in a layout placeholder.

    Chain: layout placeholder -> master placeholder of the same type -> master
    text styles of the role -> presentation defaults -> theme.
    """
    ph = (ph_type, "0")
    palette, mapping = color_context(theme, master, (layout,))
    fonts = theme_fonts(theme, fallback)
    props, _ = run_candidates([layout_shape, master_shape], master, presentation, ph)
    latin = next((n.find("a:latin", NS) for n in props if n.find("a:latin", NS) is not None), None)
    face = latin.get("typeface") if latin is not None else None
    if face and face.startswith("+"):
        face = fonts.get("major" if face.startswith("+mj") else "minor", fallback)
    font = face or fonts.get("minor", fallback)
    size = next((int(n.get("sz")) / 100 for n in props if n.get("sz")), 18.0)
    source = next((n.find("a:solidFill", NS)[0] for n in props
                   if n.find("a:solidFill", NS) is not None and len(n.find("a:solidFill", NS))), None)
    color = rgb(source, palette, mapping) if source is not None else None
    color = color or palette.get(mapping.get("tx1", "tx1")) or "000000"
    color_ref = ([source.tag.rsplit("}", 1)[-1], source.get("val"), [[t.tag.rsplit("}", 1)[-1], t.get("val")] for t in source]]
                 if source is not None else None)
    bold = next((n.get("b") in ("1", "true") for n in props if n.get("b") is not None), False)
    role = style_role(ph)
    levels = [s.find("p:txBody/a:lstStyle/a:lvl1pPr", NS) if s is not None else None for s in (layout_shape, master_shape)]
    levels.append(master.find(f"p:txStyles/p:{role}/a:lvl1pPr", NS) if master is not None else None)
    levels.append(presentation.find("p:defaultTextStyle/a:lvl1pPr", NS) if presentation is not None else None)
    bullet, bullet_char = "none", None
    for level in levels:
        if level is None:
            continue
        if level.find("a:buNone", NS) is not None:
            break
        char = level.find("a:buChar", NS)
        if char is not None:
            bullet, bullet_char = "char", char.get("char")
            break
        if level.find("a:buAutoNum", NS) is not None:
            bullet = "autonum"
            break
    line_spacing = _spacing(levels, "lnSpc", {"pct": 100.0})
    space_before = _spacing(levels, "spcBef", {"pt": 0.0})
    bodies = [s.find("p:txBody/a:bodyPr", NS) if s is not None else None for s in (layout_shape, master_shape)]
    insets = [int(_first(bodies, k) or default) for k, default in (("lIns", 91440), ("tIns", 45720), ("rIns", 91440), ("bIns", 45720))]
    autofit = "none"
    for body in bodies:
        if body is None:
            continue
        found = next((v for k, v in (("normAutofit", "norm"), ("spAutoFit", "shape"), ("noAutofit", "none"))
                      if body.find(f"a:{k}", NS) is not None), None)
        if found:
            autofit = found
            break
    # PowerPoint applies the space before the first paragraph of a frame only with bodyPr spcFirstLastPara="1" (Google
    # Slides writes it; Office templates do not): analysis/style-experiments/20260923-quality-tails, journal item 1.
    space_first_last = _first(bodies, "spcFirstLastPara") in ("1", "true")
    # Item 37, cause 9 (holdout-next 25.09.2026, deck 27, deck 26): capitals the chain sets (cap="all" or "small"); PowerPoint
    # draws the text in capitals, which are wider than the letters the preview measured.
    caps = next((n.get("cap") for n in props if n.get("cap") is not None), None)
    return {"font": font, "size_pt": size, "color": color, "color_ref": color_ref, "bold": bold,
            "align": _first(levels, "algn") or "l", "bullet": bullet, "bullet_char": bullet_char,
            "margin_left_emu": int(_first(levels, "marL") or 0), "indent_emu": int(_first(levels, "indent") or 0),
            "line_spacing": line_spacing, "space_before": space_before, "space_first_last": space_first_last,
            "anchor": _first(bodies, "anchor") or "t", "insets_emu": insets, "autofit": autofit,
            **({"caps": caps} if caps in ("all", "small") else {})}


def read_inventory(package: Package) -> dict:
    presentation_part = package.related("", "officeDocument")[0]
    presentation = parse_xml(package.parts[presentation_part])
    size = presentation.find("p:sldSz", NS)
    width, height = int(size.get("cx")) / EMU_PER_PX, int(size.get("cy")) / EMU_PER_PX

    rel_by_id = {r.id: r for r in package.rels(presentation_part)}
    slide_parts = []
    for node in presentation.findall("p:sldIdLst/p:sldId", NS):
        rel = rel_by_id.get(node.get(f"{{{NS['r']}}}id"))
        if rel is not None:
            slide_parts.append(package.resolve(presentation_part, rel.target))

    # Session 16 (26.09.2026, holdout-next3: deck 20, deck 28): slides of instructions for the author at the start of the
    # sample (roles.leading_instructions) are no evidence of the composition: the slides of the carrier (cover, brand decor,
    # cards and pictograms of the samples, scenes) and the furniture leave them out, and the first slide after them is the
    # opening slide (the sample of the cover). The author set them on layouts of the template in its styles, so the use of
    # the layouts and the observed styles still count them (deck 38: its only text; deck 28: without the use of its instruction
    # slide the white "Content Slide" tied with the black cover layout, and slides of key numbers went black).
    instructions = leading_instructions([[_plain_text(p) for p in parse_xml(package.parts[part]).iter("{%s}p" % NS["a"])]
                                         for part in slide_parts])
    usage = Counter()
    slides = []
    content_boxes: dict[str, list] = {}
    for number, part in enumerate(slide_parts, 1):
        root = parse_xml(package.parts[part])
        layout = (package.related(part, "slideLayout") or [None])[0]
        usage[layout] += 1
        tree = root.find("p:cSld/p:spTree", NS)
        # Where the author of the reference really put content on this layout.
        for box in _static_boxes(root):
            if box[2] * box[3] < width * height * 0.6:
                content_boxes.setdefault(layout, []).append(box)
        # Top-level shapes by id: what a clone of the shape would carry (A5b, furniture).
        rels = {r.id: r for r in package.rels(part)}
        shapes = {}
        for node in tree if tree is not None else []:
            nv = node.find("*/p:cNvPr", NS)
            if nv is None:
                continue
            used = {v for n in node.iter() for k, v in n.attrib.items() if k.startswith(R)}
            shapes[nv.get("id")] = {"relationships": sorted(rels[i].kind if i in rels else "missing" for i in used),
                                    "text": any((t.text or "").strip() for t in node.iter("{%s}t" % NS["a"])),
                                    "placeholder": node.find(".//p:nvPr/p:ph", NS) is not None}
        slides.append({
            "number": number, "part": part, "layout": layout,
            "placeholders": len(root.findall(".//p:nvPr/p:ph", NS)),
            "text_boxes": sum(1 for s in tree.iter(P + "sp") if not _is_placeholder(s) and s.find("p:txBody//a:t", NS) is not None),
            "pictures": len(list(tree.iter(P + "pic"))),
            "graphic_frames": len(list(tree.iter(P + "graphicFrame"))),
            "shapes": shapes,
        })

    samples = [s for s in slides if s["number"] not in instructions]
    layouts = []
    masters = {}
    for master_part in package.related(presentation_part, "slideMaster"):
        master = parse_xml(package.parts[master_part])
        master_phs = {p["type"]: p for p in _placeholders(master)}
        master_shapes = {p["type"]: s for p, s in zip(_placeholders(master), _placeholder_shapes(master))}
        theme_parts = package.related(master_part, "theme")
        theme_root = parse_xml(package.parts[theme_parts[0]]) if theme_parts else None
        masters[master_part] = {"theme": _theme_colors(package, master_part, master), "decor": _static_boxes(master),
                                "theme_part": theme_parts[0] if theme_parts else None,
                                "body_size_pt": _master_style_size(master, "body")}
        for layout_part in package.related(master_part, "slideLayout"):
            root = parse_xml(package.parts[layout_part])
            c_sld = root.find("p:cSld", NS)
            placeholders = _placeholders(root)
            for ph, shape in zip(placeholders, _placeholder_shapes(root)):
                inherited = master_phs.get(ph["type"]) or master_phs.get(MASTER_FALLBACK.get(ph["type"], ""))
                if ph["box"] is None and inherited:
                    ph["box"] = inherited["box"]
                ph["size_pt"] = (ph["size_pt"] or (inherited or {}).get("size_pt")
                                 or _master_style_size(master, ph["type"]) or 18.0)
                master_shape = master_shapes.get(ph["type"])
                if master_shape is None:
                    master_shape = master_shapes.get(MASTER_FALLBACK.get(ph["type"], ""))
                ph["typography"] = placeholder_typography(ph["type"], shape, master_shape, master, presentation, theme_root, root)
                fill = _placeholder_fill(shape, master_shape, theme_root, master, root)
                if fill:
                    ph["fill"] = fill
            shows_master = root.get("showMasterSp", "1") not in ("0", "false")
            theme = masters[master_part]["theme"]
            background = _background_rgb(root, theme) or _background_rgb(master, theme)
            layouts.append({
                "part": layout_part, "master": master_part,
                "name": c_sld.get("name", "") if c_sld is not None else "",
                "type": root.get("type", "cust"), "used_by_slides": usage.get(layout_part, 0),
                "placeholders": placeholders, "background_rgb": background,
                # Owner decision 46 (28.09.2026): whether the layout declares a background of its own; one that does not
                # shows the background of its master (composer.bare_pattern).
                "own_background": root.find("p:cSld/p:bg", NS) is not None,
                "decor": _static_boxes(root) + (masters[master_part]["decor"] if shows_master else []),
                "sample_content_boxes": content_boxes.get(layout_part, []),
                "image_formats": dict(Counter(package.resolve(layout_part, r.target).rsplit(".", 1)[-1].lower()
                                              for r in package.rels(layout_part) if r.kind == "image" and not r.external)),
            })

    return {"presentation_part": presentation_part, "width": width, "height": height,
            "slide_parts": slide_parts, "slides": samples, "layouts": layouts, "masters": masters,
            "instruction_slides": instructions, "opening_slide": samples[0]["number"] if samples else 1,
            "first_slide_layout": slides[0]["layout"] if slides else None,
            "card_styles": _card_styles(package, slides, width, height),
            "observed_styles": _observed_styles(package, slides),
            "free_text_size_pt": _free_text_size(package, slides),
            "free_text_colours": _free_text_colours(package, slides),
            "furniture": _furniture(package, samples, width, height),
            "media_formats": dict(Counter(p.rsplit(".", 1)[-1].lower() for p in package.parts if p.startswith("ppt/media/")))}


read_reference = read_inventory


def _decor_inside(layout: dict, box, area: float) -> int:
    """Inherited decor that occupies the area where new content would go."""
    bx, by, bw, bh = box
    count = 0
    for x, y, w, h in layout["decor"]:
        overlap = max(0, min(bx + bw, x + w) - max(bx, x)) * max(0, min(by + bh, y + h) - max(by, y))
        # Full-bleed backgrounds are not obstacles; a logo or a line at the edge barely overlaps.
        if w * h < area * 0.6 and overlap > max(area * 0.01, w * h * 0.5):
            count += 1
    return count


def classify_layout(layout: dict, width: float, height: float) -> dict:
    """Describe what a layout can carry, from placeholder semantics and geometry."""
    area = width * height
    usable = [p for p in layout["placeholders"] if p["box"] and not p["rotated"] and not p["vertical"]]
    titles = [p for p in usable if p["type"] in TITLE_TYPES]
    texts = [p for p in usable if p["type"] in TEXT_TYPES and p["box"][2] * p["box"][3] >= area * 0.06]
    other = [p for p in usable if p["type"] not in TITLE_TYPES | TEXT_TYPES | SERVICE_TYPES]
    texts.sort(key=lambda p: (round(p["box"][1] / 40), p["box"][0]))
    title = min(titles, key=lambda p: p["box"][1]) if titles else None
    is_cover = layout["type"] == "title" or any(p["type"] == "ctrTitle" for p in titles)
    # Item 31 (25.09.2026): a turned footer, date or slide number carries no content (deck 25: the footer placeholder of the band
    # of its sample layout); turned title or text placeholders stay outside the contract of the renderer.
    has_vertical = any((p["vertical"] or p["rotated"]) and p["type"] not in SERVICE_TYPES for p in layout["placeholders"])
    kind = "unsupported"
    if title and not has_vertical:
        if is_cover:
            kind = "cover"
        elif layout["type"] == "secHead":
            kind = "section"
        elif other:
            kind = "unsupported"
        elif not texts:
            kind = "title_only"
        elif len(texts) == 1:
            kind = "content_1"
        elif len(texts) == 2 and abs(texts[0]["box"][1] - texts[1]["box"][1]) < height * 0.08:
            kind = "content_2"
        else:
            kind = "content_n"
    small_text = [p for p in usable if p["type"] in TEXT_TYPES and p not in texts]
    score = 0
    if title:
        score += 2 if title["box"][1] + title["box"][3] / 2 < height * 0.35 else -2
    if texts:
        body = max(texts, key=lambda p: p["box"][2] * p["box"][3])
        share = body["box"][2] * body["box"][3] / area
        score += 2 if share >= 0.3 else 1 if share >= 0.15 else 0
        score += 1 if title and body["box"][1] >= title["box"][1] else -1
        score -= min(3, _decor_inside(layout, body["box"], area))
        if body["size_pt"] < 14:
            score -= 3  # contents lists, contact cards and captions, not a body for ordinary text
    elif title:
        free = [width * 0.05, title["box"][1] + title["box"][3], width * 0.9, height * 0.88 - (title["box"][1] + title["box"][3])]
        score -= min(4, 2 * _decor_inside(layout, free, area))
    score -= min(3, len(small_text) if kind != "cover" else 0)
    score += 1 if layout["type"] in ("obj", "tx", "twoObj", "twoColTx", "titleOnly", "title") else 0
    return {"kind": kind, "title": title, "texts": texts, "small_texts": small_text, "plausibility": score,
            "service": {p["type"]: p for p in usable if p["type"] in SERVICE_TYPES},
            "non_text_placeholders": [p["type"] for p in other]}
