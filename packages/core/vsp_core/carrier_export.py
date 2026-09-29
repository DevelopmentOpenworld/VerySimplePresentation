"""Export on the reference package itself (A3-A4, docs/CARRIER_PRODUCT_2026-09-21.md).

bind_carrier turns a reference-patterns-v1 document into vsp.document/2: every
slide names its reference layout, every element says how it reaches the file.
export_carrier_pptx keeps masters, layouts, themes and embedded fonts of the
reference byte for byte, removes its sample slides and puts our slides on its
own layouts. Native elements use the serializer of exporter.py. Ported from
spikes/template_carrier/carrier.py (_clear_slides, add_slide, finalize, validate).
"""
import base64
import copy
import re
import xml.etree.ElementTree as ET
from collections import Counter
from xml.sax.saxutils import escape, quoteattr, unescape

from . import charts
from .exporter import element_xml, xfrm, color
from .font_assets import wrap_eot
from .importer import EMU, digest
from .opc import OFFICE_REL, Package, Rel, parse_xml
from .reference import FOOTER_SEGMENTS, NS, classify_layout, close_to_title

P, A, R = NS["p"], NS["a"], NS["r"]
ROOT = f'xmlns:a="{A}" xmlns:r="{R}" xmlns:p="{P}"'
SLIDE_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
LAYOUT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"
# The output is always a presentation: a .potx (template.main+xml) or .ppsx (slideshow.main+xml) sample keeps its own
# type in [Content_Types].xml, and PowerPoint refuses a .pptx with it (blind test of 23.09, analysis/style-experiments/
# 20260923-blind-office, journal 2).
PRESENTATION_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
SECTION_EXT = "{521415D9-36F7-43E2-AB2F-B90AF26B5E84}"
# Own literal, not exporter.TABLE_URI: the self-check must catch a wrong serializer constant.
GRAPHIC_URIS = {"http://schemas.openxmlformats.org/drawingml/2006/table", "http://schemas.openxmlformats.org/drawingml/2006/chart"}
ALIGN = {"left":"l", "center":"ctr", "right":"r"}
NOTES_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"
NOTES_MASTER_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.notesMaster+xml"
THEME_TYPE = "application/vnd.openxmlformats-officedocument.theme+xml"
GROUP = ('<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/>'
         '<a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>')
# A plain notes master for a sample without one (5 of the 33 development samples): the picture of the slide above, the
# notes below, on the notes page of presentation.xml (notesSz, 6858000 x 9144000 EMU by default).
NOTES_MASTER_XML = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<p:notesMaster {ROOT}><p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg><p:spTree>{GROUP}'
    '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Slide Image Placeholder 1"/><p:cNvSpPr><a:spLocks noGrp="1" noRot="1" noChangeAspect="1"/></p:cNvSpPr>'
    '<p:nvPr><p:ph type="sldImg" idx="2"/></p:nvPr></p:nvSpPr><p:spPr><a:xfrm><a:off x="685800" y="1143000"/><a:ext cx="5486400" cy="3086100"/></a:xfrm>'
    '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln w="12700"><a:solidFill><a:prstClr val="black"/></a:solidFill></a:ln></p:spPr></p:sp>'
    '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Notes Placeholder 2"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>'
    '<p:spPr><a:xfrm><a:off x="685800" y="4400550"/><a:ext cx="5486400" cy="3600450"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
    '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="ru-RU"/></a:p></p:txBody></p:sp></p:spTree></p:cSld>'
    '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" '
    'accent6="accent6" hlink="hlink" folHlink="folHlink"/><p:notesStyle><a:lvl1pPr marL="0" algn="l" rtl="0"><a:defRPr sz="1200" kern="1200">'
    '<a:solidFill><a:schemeClr val="tx1"/></a:solidFill><a:latin typeface="+mn-lt"/><a:ea typeface="+mn-ea"/><a:cs typeface="+mn-cs"/></a:defRPr>'
    '</a:lvl1pPr></p:notesStyle></p:notesMaster>')


# ---- A3: bindings --------------------------------------------------------------
def bind_carrier(doc, style):
    if doc.get("composition_engine") != "reference-patterns-v1":
        raise ValueError("Экспорт на носителе требует композиции образца")
    carrier = style["carrier"]
    layouts = {l["part"]:l for l in carrier["layouts"]}
    patterns = {p["id"]:p for p in style["reference"]["patterns"]}
    sample_shapes = {s["part"]:s.get("shapes", {}) for s in carrier["slides"]}
    doc = copy.deepcopy(doc)
    doc["schema"] = "vsp.document/2"
    doc["export"] = {"mode":"carrier", "reference_sha256":style["source_sha256"]}
    for slide in doc["slides"]:
        ref = slide["reference"]
        layout, pattern = layouts.get(ref["layout_part"]), patterns.get(ref["pattern"])
        # C4a: a fallback slide on a layout without its own composition has no pattern; the layout draws everything.
        if pattern is None and ref.get("pattern") is None and ref.get("family") == "carrier-fallback":
            pattern = {}
        if layout is None or pattern is None:
            raise ValueError("Макет или композиция слайда не найдены в образце: "+slide["id"])
        master = layout["master"]
        owners = {layout["part"]:"layout", master:"master"}
        theme = carrier["masters"][master].get("theme_part")
        if theme:owners[theme] = "theme"
        slide["layout"] = {"part":layout["part"], "master":master, "name":layout["name"],
            "kind":classify_layout(layout, carrier["width"], carrier["height"])["kind"],
            "show_master_shapes":pattern.get("show_master_shapes", True), "color_map_override":copy.deepcopy(pattern.get("color_map_override"))}
        origin = ref.get("background_origin")
        slide["background_binding"] = "inherited" if origin in (layout["part"], master, "fallback") else "native"
        # Placeholder frames of a layout are not drawn on a slide until the slide repeats them.
        placeholder_surfaces = {str(s["source"].get("shape")) for s in pattern.get("surfaces", []) if s.get("placeholder")}
        for e in slide["elements"]:
            # A binding the composer has already set (text in a layout placeholder, A5b) stays.
            if "binding" in e:
                continue
            source = e.get("source_style") or {}
            decor = e.get("template_decoration") and e.get("locked") and e["type"] in ("image", "shape")
            owner = owners.get(source.get("part")) if e.get("template_decoration") and e.get("locked") else None
            if owner and e["id"].startswith("surface") and str(source.get("shape")) in placeholder_surfaces:
                owner = None
            if owner:
                e["binding"] = {"kind":"inherited", "owner":owner}
                continue
            # Decor of the sample slide itself is cloned with its XML when every relationship of the shape
            # is a picture; a hyperlink to a removed sample slide would dangle, so that stays native.
            # A card that holds sample text or a placeholder would bring that text along
            # (analysis/style-experiments/20260921-a5-composer/01-furniture-literal-rule.txt), so it stays native too.
            shape = sample_shapes.get(source.get("part"), {}).get(str(source.get("shape"))) if source.get("shape") else None
            if (decor and source.get("part") == ref.get("source_part") and shape is not None and not shape["text"] and not shape["placeholder"]
                    and all(k == "image" for k in shape["relationships"])):
                e["binding"] = {"kind":"furniture", "source":{"part":source["part"], "shape":str(source["shape"])}}
            else:
                e["binding"] = {"kind":"native"}
    return doc


# ---- A4: the package -------------------------------------------------------------
def package_counts(package):
    names = package.parts
    return {"masters":sum(bool(re.fullmatch(r"ppt/slideMasters/[^/]+\.xml", n)) for n in names),
            "layouts":sum(bool(re.fullmatch(r"ppt/slideLayouts/[^/]+\.xml", n)) for n in names),
            "themes":sum(bool(re.fullmatch(r"ppt/theme/[^/]+\.xml", n)) for n in names),
            "fonts":sum(n.startswith("ppt/fonts/") for n in names),
            "media":dict(Counter(n.rsplit(".", 1)[-1].lower() for n in names if n.startswith("ppt/media/")))}


def slide_parts(package, presentation_part):
    root = parse_xml(package.parts[presentation_part])
    rels = {r.id:r for r in package.rels(presentation_part)}
    found = []
    for node in root.findall("p:sldIdLst/p:sldId", NS):
        rel = rels.get(node.get(f"{{{R}}}id"))
        if rel is not None and not rel.external:found.append(package.resolve(presentation_part, rel.target))
    return found


def source_shape(package, part, shape_id):
    """Top-level shape of a sample slide with its relationships, as reference._furniture keeps it."""
    if part not in package.parts:
        raise ValueError(f"Исходный слайд декора не найден: {part}")
    tree = parse_xml(package.parts[part]).find("p:cSld/p:spTree", NS)
    rels = {r.id:r for r in package.rels(part)}
    for node in tree if tree is not None else []:
        nv = node.find("*/p:cNvPr", NS)
        if nv is not None and nv.get("id") == str(shape_id):
            used = {v for n in node.iter() for k, v in n.attrib.items() if k.startswith(f"{{{R}}}")}
            return {"xml":ET.tostring(node, encoding="unicode"),
                    "rels":{i:(rels[i].type, rels[i].target if rels[i].external else package.resolve(part, rels[i].target), rels[i].external) for i in used if i in rels}}
    raise ValueError(f"Фигура декора {shape_id} не найдена в {part}")


# ---- A7: clones of sample slide shapes ---------------------------------------------------------------
def _set_frame(node, frame):
    """Top-level a:xfrm of a cloned shape: new offset and extent; a group keeps chOff/chExt, so its children scale."""
    tag = node.tag.rsplit("}", 1)[-1]
    path = {"grpSp": "p:grpSpPr/a:xfrm", "graphicFrame": "p:xfrm"}.get(tag, "p:spPr/a:xfrm")
    x = node.find(path, NS)
    if x is None:
        raise ValueError("У клонируемой фигуры нет a:xfrm")
    x.find("a:off", NS).attrib.update({"x": str(round(frame[0]*EMU)), "y": str(round(frame[1]*EMU))})
    x.find("a:ext", NS).attrib.update({"cx": str(round(frame[2]*EMU)), "cy": str(round(frame[3]*EMU))})


def _clone_text(node, texts):
    """Text of a cloned shape: its elements in the order of their source paragraphs (a label above its fact), every line
    a copy of the source paragraph of its element with one run that keeps the properties of that paragraph's first run;
    sz and line spacing are written only when the element changed them."""
    body = node.find("p:txBody", NS)
    paragraphs = body.findall("a:p", NS)
    written = []
    for e in sorted(texts, key=lambda e: e["binding"]["paragraph"]):
        binding = e["binding"]
        template = paragraphs[binding["paragraph"]]
        run = template.find("a:r", NS)
        rpr = run.find("a:rPr", NS) if run is not None else None
        if rpr is None:
            end = template.find("a:endParaRPr", NS)
            rpr = ET.Element(f"{{{A}}}rPr", dict(end.attrib) if end is not None else {})
            for child in (end if end is not None else []):
                rpr.append(copy.deepcopy(child))
        rpr = copy.deepcopy(rpr)
        if abs(e["font_size"]*.75 - binding["size_pt"]) > .01:
            rpr.set("sz", str(round(e["font_size"]*75)))
        _override_run(rpr, binding.get("overrides") or {})
        spacing = binding.get("line_spacing") or {"pct": 100.0}
        # Points are the same height at every size in PowerPoint: the multiple they give at the size of the element, not at
        # the own size (a clone set smaller than its sample kept the pitch of the sample in PowerPoint, not the one measured).
        expected = 1.2*spacing["pct"]/100 if "pct" in spacing else spacing["pt"]*4/3/e["font_size"]
        for line in e["text"].split("\n"):
            p = copy.deepcopy(template)
            for child in list(p):
                if child.tag.rsplit("}", 1)[-1] not in ("pPr", "endParaRPr"):
                    p.remove(child)
            if abs(e["line_height"] - expected) > .01:
                ppr = p.find("a:pPr", NS)
                if ppr is None:
                    ppr = ET.Element(f"{{{A}}}pPr"); p.insert(0, ppr)
                for old in ppr.findall("a:lnSpc", NS):
                    ppr.remove(old)
                spc = ET.Element(f"{{{A}}}lnSpc"); ET.SubElement(spc, f"{{{A}}}spcPct", {"val": str(round(e["line_height"]/1.2*100000))})
                ppr.insert(0, spc)
            r = ET.Element(f"{{{A}}}r"); r.append(copy.deepcopy(rpr)); ET.SubElement(r, f"{{{A}}}t").text = line
            at = 1 if p.find("a:pPr", NS) is not None else 0
            p.insert(at, r)
            written.append(p)
    for old in paragraphs:
        body.remove(old)
    for p in written:
        body.append(p)


def _override_run(rpr, overrides):
    """C6: a colour or typeface a fixer chose for cloned text replaces the one of the source run."""
    if "color" in overrides:
        for child in list(rpr):
            if child.tag.rsplit("}", 1)[-1] in ("noFill", "solidFill", "gradFill", "blipFill", "pattFill", "grpFill"):
                rpr.remove(child)
        fill = ET.Element(f"{{{A}}}solidFill"); ET.SubElement(fill, f"{{{A}}}srgbClr", {"val": overrides["color"].upper()})
        rpr.insert(1 if rpr.find("a:ln", NS) is not None else 0, fill)
    if "font" in overrides:
        for tag in ("latin", "ea", "cs"):
            node = rpr.find(f"a:{tag}", NS)
            if node is None:
                node = ET.SubElement(rpr, f"{{{A}}}{tag}")
            node.attrib.clear(); node.set("typeface", overrides["font"])


def _clear_text(node):
    """Text of a cloned container whose own slot is not used: one empty paragraph with the source paragraph properties."""
    body = node.find("p:txBody", NS)
    if body is None:
        return
    paragraphs = body.findall("a:p", NS)
    for old in paragraphs[1:]:
        body.remove(old)
    if paragraphs:
        for child in list(paragraphs[0]):
            if child.tag.rsplit("}", 1)[-1] not in ("pPr", "endParaRPr"):
                paragraphs[0].remove(child)


def clone_xml(item, texts, binding, rel):
    """A cloned top-level shape: new frame, facts (and their labels) in its text slots, relationships re-created on the
    new slide. A hyperlink to another slide of the reference would dangle once the sample slides are gone, so it is removed."""
    node = ET.fromstring(item["xml"])
    # A separate text shape follows its elements (capacity repairs and the editor may move or resize them): its frame is
    # their union; text inside a card shape covers only the text area of that shape, so the shape keeps the frame of the binding.
    frame = binding["frame"]
    if texts and not texts[0]["binding"].get("own_text"):
        x0, y0 = min(e["x"] for e in texts), min(e["y"] for e in texts)
        frame = [x0, y0, max(e["x"] + e["w"] for e in texts) - x0, max(e["y"] + e["h"] for e in texts) - y0]
    _set_frame(node, frame)
    # A7 v2: insets the planner changed (a right inset repeating the left one) are written to the text body.
    override = next((e["binding"]["body_insets"] for e in texts if e["binding"].get("body_insets")), None)
    body = node.find("p:txBody/a:bodyPr", NS)
    if override and body is not None:
        for key, value in zip(("lIns", "tIns", "rIns", "bIns"), override):
            body.set(key, str(round(value*EMU)))
    if texts:
        _clone_text(node, texts)
    elif binding.get("clear_text"):
        _clear_text(node)
    mapping = {}
    for old, (rel_type, target, external) in sorted(item["rels"].items()):
        if rel_type.endswith("/slide") and not external:
            continue
        mapping[old] = rel(rel_type, target, external)
    for parent in list(node.iter()):
        for child in list(parent):
            rid = child.get(f"{{{R}}}id")
            if child.tag in (f"{{{A}}}hlinkClick", f"{{{A}}}hlinkHover") and rid and rid not in mapping:
                parent.remove(child)
    for n in node.iter():
        for key, value in list(n.attrib.items()):
            if key.startswith(f"{{{R}}}") and value in mapping:
                n.set(key, mapping[value])
    return node


def replace_text(node, change):
    """Item 31 (25.09.2026): the text shape of copied decor whose whole text is change["from"] (a running title: the name of
    the sample presentation) takes change["to"] as one paragraph with the properties of its first run, at change["size_pt"]
    when the composer needed a smaller size for one line. Returns whether a shape was found."""
    for sp in node.iter(f"{{{P}}}sp"):
        body = sp.find("p:txBody", NS)
        if body is None:
            continue
        paragraphs = body.findall("a:p", NS)
        words = lambda p: "".join(t.text or "" for t in p.iter(f"{{{A}}}t"))
        if " ".join(" ".join(words(p) for p in paragraphs).split()) != change["from"]:
            continue
        first = next(p for p in paragraphs if words(p).strip())
        run = copy.deepcopy(next(r for r in first.findall("a:r", NS) if (r.findtext("a:t", default="", namespaces=NS) or "").strip()))
        run.find("a:t", NS).text = change["to"]
        if change.get("size_pt"):
            rpr = run.find("a:rPr", NS)
            if rpr is None:
                rpr = ET.Element(f"{{{A}}}rPr"); run.insert(0, rpr)
            rpr.set("sz", str(round(change["size_pt"] * 100)))
        paragraph = copy.deepcopy(first)
        for child in list(paragraph):
            if child.tag.rsplit("}", 1)[-1] not in ("pPr", "endParaRPr"):
                paragraph.remove(child)
        paragraph.insert(1 if paragraph.find("a:pPr", NS) is not None else 0, run)
        for p in paragraphs:
            body.remove(p)
        body.append(paragraph)
        return True
    return False


def clear_slides(package, presentation_part):
    text = package.parts[presentation_part].decode("utf-8")
    prefix = re.search(r"<(\w+):presentation\b", text)
    if prefix is None or prefix.group(1) != "p":
        raise ValueError("Нестандартный префикс пространства имён PresentationML")
    text = re.sub(r"<p:sldIdLst>.*?</p:sldIdLst>|<p:sldIdLst\s*/>", "<p:sldIdLst>@@SLIDES@@</p:sldIdLst>", text, flags=re.S)
    if "@@SLIDES@@" not in text:
        raise ValueError("В образце нет списка слайдов")
    # Sections and custom shows point at slide ids that no longer exist.
    text = re.sub(r'<p:ext uri="%s">.*?</p:ext>' % re.escape(SECTION_EXT), "", text, flags=re.S)
    text = re.sub(r"<p:custShowLst>.*?</p:custShowLst>", "", text, flags=re.S)
    text = re.sub(r"<p:extLst>\s*</p:extLst>", "", text)
    for slide in slide_parts(package, presentation_part):
        package.parts.pop(slide, None)
        package.parts.pop(package.rels_name(slide), None)
        package.overrides.pop(slide, None)
    # Collaboration metadata is keyed by the removed slide ids.
    package.set_rels(presentation_part, [r for r in package.rels(presentation_part) if r.kind not in ("slide", "changesInfo", "revisionInfo")])
    for other in list(package.parts):
        if other.endswith(".rels") or not other.startswith("ppt/") or other.startswith("ppt/slides/"):
            continue
        rels = package.rels(other)
        if any(r.kind == "slide" for r in rels):
            package.set_rels(other, [r for r in rels if r.kind != "slide"])
            if other.endswith("viewProps.xml"):
                view = package.parts[other].decode("utf-8")
                package.parts[other] = re.sub(r"<p:sldLst>.*?</p:sldLst>", "", view, flags=re.S).encode("utf-8")
    # The thumbnail shows the removed reference cover.
    package.set_rels("", [r for r in package.rels("") if r.kind != "thumbnail"])
    return text


def placeholder_body(e, t):
    """a:bodyPr and a:pPr of placeholder text: only what differs from the inherited typography t.

    An absent optional text property of the element means what the preview draws for it
    (renderer.js textLayout): zero insets, anchor "t", no marker, zero paragraph gap.
    """
    px = lambda emu: emu / EMU
    insets = e.get("inset") or [0, 0, 0, 0]
    inherited = t.get("insets_emu") or [91440, 45720, 91440, 45720]
    body = "".join(f' {k}="{round(v*EMU)}"' for k, v, base in zip(("lIns", "tIns", "rIns", "bIns"), insets, inherited) if abs(v-px(base)) > .5)
    anchor = e.get("anchor", "t")
    if anchor != t.get("anchor", "t"):
        body += f' anchor="{anchor}"'
    # The gap before the first paragraph as the preview draws it: PowerPoint needs spcFirstLastPara for it.
    first = bool(e.get("space_first_last", True))
    if e.get("paragraph_gap") and first != bool(t.get("space_first_last")):
        body += f' spcFirstLastPara="{int(first)}"'
    bullet = e.get("bullet")
    attrs = ""
    margin, indent = (bullet["indent"], -bullet["hanging"]) if bullet else (0, 0)
    if abs(margin-px(t.get("margin_left_emu", 0))) > .5:
        attrs += f' marL="{round(margin*EMU)}"'
    if abs(indent-px(t.get("indent_emu", 0))) > .5:
        attrs += f' indent="{round(indent*EMU)}"'
    align = ALIGN.get(e.get("align"), "l")
    if align != t.get("align", "l"):
        attrs += f' algn="{align}"'
    children = ""
    spacing = t.get("line_spacing") or {"pct": 100.0}
    expected = 1.2*spacing["pct"]/100 if "pct" in spacing else spacing["pt"]*4/3/e["font_size"]
    # 1e-9: 1.18 against 1.2 is 0.020000000000000018 in binary floating point.
    if abs(e["line_height"]-expected) > .02+1e-9:
        children += f'<a:lnSpc><a:spcPct val="{round(e["line_height"]/1.2*100000)}"/></a:lnSpc>'
    before = t.get("space_before") or {"pt": 0.0}
    expected = before["pt"]*4/3 if "pt" in before else before["pct"]/100*e["font_size"]
    gap = e.get("paragraph_gap", 0)
    if abs(gap-expected) > .5:
        children += f'<a:spcBef><a:spcPts val="{round(gap*75)}"/></a:spcBef>'
    if bullet is None and t.get("bullet") in ("char", "autonum"):
        children += "<a:buNone/>"
    elif bullet is not None and (t.get("bullet") != "char" or t.get("bullet_char") != bullet["char"]):
        children += f"<a:buChar char={quoteattr(bullet['char'])}/>"
    ppr = f"<a:pPr{attrs}>{children}</a:pPr>" if children else (f"<a:pPr{attrs}/>" if attrs else "")
    return f"<a:bodyPr{body}/>", ppr


def placeholder_xml(e, i, binding):
    """Text in a layout placeholder: explicit geometry, and in a:bodyPr, a:pPr, a:rPr only what differs from the inherited typography."""
    t = binding.get("typography") or {}
    size = "" if t.get("size_pt") is not None and abs(e["font_size"]-t["size_pt"]*4/3) <= .25 else f' sz="{round(e["font_size"]*75)}"'
    bold = "" if bool(e.get("bold")) == bool(t.get("bold")) else f' b="{int(bool(e.get("bold")))}"'
    fill = "" if str(e.get("color", "")).upper() == str(t.get("color", "")).upper() else f'<a:solidFill><a:srgbClr val="{color(e["color"])}"/></a:solidFill>'
    font = "" if e.get("font") == t.get("font") else ''.join(f'<a:{k} typeface={quoteattr(e["font"])}/>' for k in ("latin", "ea", "cs"))
    body, ppr = placeholder_body(e, t)
    identity = "".join(f" {k}={quoteattr(str(v))}" for k, v in binding["raw"].items())
    paragraphs = "".join(f'<a:p>{ppr}<a:r><a:rPr lang="ru-RU"{size}{bold}>{fill}{font}</a:rPr><a:t>{escape(line)}</a:t></a:r></a:p>' for line in e["text"].split("\n"))
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{i}" name={quoteattr(e["id"])}/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph{identity}/></p:nvPr></p:nvSpPr>'
            f'<p:spPr>{xfrm(e)}</p:spPr><p:txBody>{body}<a:lstStyle/>{paragraphs}</p:txBody></p:sp>')


def slide_xml(slide, shapes):
    layout = slide["layout"]
    hidden = "" if layout.get("show_master_shapes", True) else ' showMasterSp="0"'
    background = (f'<p:bg><p:bgPr><a:solidFill><a:srgbClr val="{color(slide["background"])}"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>'
                  if slide["background_binding"] == "native" else "")
    override = layout.get("color_map_override")
    mapping = ("<a:overrideClrMapping" + "".join(f" {k}={quoteattr(str(v))}" for k, v in override.items()) + "/>") if override else "<a:masterClrMapping/>"
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<p:sld {ROOT}{hidden}><p:cSld>{background}<p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
            f'{"".join(shapes)}</p:spTree></p:cSld><p:clrMapOvr>{mapping}</p:clrMapOvr></p:sld>')


def notes_xml(text, language):
    """A notes slide: the picture of the slide and the text a speaker says, paragraph by paragraph (owner decision 21, 1a)."""
    lang = "en-US" if language == "en" else "ru-RU"
    body = "".join(f'<a:p><a:r><a:rPr lang="{lang}" dirty="0"/><a:t>{escape(line.strip())}</a:t></a:r></a:p>'
                   for line in text.split("\n") if line.strip()) or f'<a:p><a:endParaRPr lang="{lang}"/></a:p>'
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<p:notes {ROOT}><p:cSld><p:spTree>{GROUP}'
            '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Slide Image Placeholder 1"/><p:cNvSpPr><a:spLocks noGrp="1" noRot="1" noChangeAspect="1"/></p:cNvSpPr>'
            '<p:nvPr><p:ph type="sldImg"/></p:nvPr></p:nvSpPr><p:spPr/></p:sp>'
            '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Notes Placeholder 2"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>'
            f'<p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/>{body}</p:txBody></p:sp></p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:notes>')


def notes_master(package, presentation_part, presentation_text):
    """The notes master of the reference, or a plain one on a copy of the theme of its first slide master: PowerPoint
    needs a notes master for notes slides. The notes slides of the removed sample slides are dropped first."""
    for old in [p for p in package.parts if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", p)]:
        package.parts.pop(old)
        package.parts.pop(package.rels_name(old), None)
        package.overrides.pop(old, None)
    found = package.related(presentation_part, "notesMaster")
    if found:
        return found[0], presentation_text
    theme = package.related(package.related(presentation_part, "slideMaster")[0], "theme")[0]
    number = 1
    while f"ppt/theme/theme{number}.xml" in package.parts:
        number += 1
    theme_part, part = f"ppt/theme/theme{number}.xml", "ppt/notesMasters/notesMaster1.xml"
    package.put(theme_part, package.parts[theme], THEME_TYPE)
    package.put(part, NOTES_MASTER_XML, NOTES_MASTER_TYPE)
    package.set_rels(part, [Rel("rId1", f"{OFFICE_REL}/theme", package.relative(part, theme_part))])
    rel_id = package.next_rel_id(presentation_part)
    package.set_rels(presentation_part, package.rels(presentation_part)+[Rel(rel_id, f"{OFFICE_REL}/notesMaster", package.relative(presentation_part, part))])
    if "</p:sldMasterIdLst>" not in presentation_text:
        raise ValueError("В образце нет списка мастеров слайдов")
    presentation_text = presentation_text.replace("</p:sldMasterIdLst>", f'</p:sldMasterIdLst><p:notesMasterIdLst><p:notesMasterId r:id="{rel_id}"/></p:notesMasterIdLst>', 1)
    return part, presentation_text


# Item 37, cause 6: segments of a footer line of the sample presentation that carry its date or event ("May 15, 2020",
# "Let's build… 2020"); a legal line keeps its year ("© 2025 deck 15 SE").
DATED = re.compile(r"\b(?:19|20)\d{2}\b|\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b")
LEGAL = re.compile(r"©|\(c\)|copyright|all rights|все права", re.I)


def running_footer_text(text, cover_title, title):
    """A footer line of the sample presentation for the deck (item 37, cause 6): the segment naming the sample (its cover
    title) becomes the title of the deck, the dated segments of that talk go with their separator, the others stay."""
    pieces = FOOTER_SEGMENTS.split(text)
    out = []
    for i in range(0, len(pieces), 2):
        segment, separator = pieces[i], pieces[i + 1] if i + 1 < len(pieces) else ""
        core = segment.strip()
        if core and close_to_title(core, cover_title):
            segment = segment.replace(core, title)
        elif core and DATED.search(core) and not LEGAL.search(core):
            continue
        out.append(segment + separator)
    return "".join(out)


def rewrite_running_footers(data, shapes, cover_title, title):
    """The text runs of the shapes `shapes` (ids) of a layout or master part rewritten by running_footer_text, by narrow text
    surgery (opc: no parse/serialize round trip); fields (the slide number) and everything else stay byte for byte."""
    text = data.decode("utf-8")

    def shape(match):
        sp = match.group(0)
        found = re.search(r'<p:cNvPr\b[^>]*?\bid="(\d+)"', sp)
        if not found or found.group(1) not in shapes:
            return sp

        def run(m):
            value = unescape(m.group(2), {"&quot;": '"', "&apos;": "'"})
            return m.group(1) + escape(running_footer_text(value, cover_title, title)) + m.group(3)
        return re.sub(r"(<a:r>(?:(?!</a:r>).)*?<a:t(?:\s[^>]*)?>)(.*?)(</a:t>)", run, sp, flags=re.S)
    return re.sub(r"<p:sp\b[^>]*>.*?</p:sp>", shape, text, flags=re.S).encode("utf-8")


PANOSE = re.compile(rb' panose="([0-9A-Fa-f]{20})"')
FONT_PARTS = re.compile(r"ppt/(?:theme/theme|slideMasters/slideMaster|slideLayouts/slideLayout)\d+\.xml")


def repair_panose(package):
    """Holdout-next2 26.09.2026 (19th Congress of Serbian geologists): the theme fonts of the template (Aptos) carry
    panose="02110004020202020204", decimal digits written as hex; its second byte 0x11 is outside PANOSE (every byte is at
    most 15), and PowerPoint 2013, not having the font, set the whole deck in Courier. Measured on a copy of the deck: without
    the attribute a sans face is substituted. Such an attribute is removed from the fonts of themes, masters and layouts; a
    valid one and every other byte stay as they are (deck 16 and deck 03 of the development decks carry the same broken value)."""
    for name in [n for n in package.parts if FONT_PARTS.fullmatch(n)]:
        data = package.parts[name]
        fixed = PANOSE.sub(lambda m: b"" if any(v > 15 for v in bytes.fromhex(m.group(1).decode())) else m.group(0), data)
        if fixed != data:
            package.parts[name] = fixed


FONT_NODE = re.compile(rb'<a:(?:latin|ea|cs)\b[^>]*>')
SLIDE_PARTS = re.compile(r"ppt/slides/slide\d+\.xml")


def repair_font_class(package):
    """Session 15 (26.09.2026, holdout-next2 SIGGRAPH Asia 2025): the title of the cover inherits `Montserrat Black` with
    pitchFamily="2" charset="77" and no panose — "family does not matter"; PowerPoint 2013 without the font set it with serifs.
    Measured on copies of the deck (process/03-font-probe-*.jpg of 20260926-hn2-rest): without pitchFamily, or with the class
    Swiss, a sans face is substituted; without charset alone, a script face. deck 03 of the development decks: its 36 titles in
    Montserrat ExtraBold went the same way. pitchFamily whose family bits are 0 is removed from a font the deck does not embed
    and whose panose names no Latin text face; a stated class (Roman, Swiss, Modern…) and every other attribute stay. Themes,
    masters, layouts and the sample slides (their nodes travel with the clones)."""
    presentation = package.parts.get("ppt/presentation.xml", b"")
    embedded = set(re.findall(rb'<p:embeddedFont>\s*<p:font typeface="([^"]*)"', presentation))

    def fixed(m):
        node = m.group(0)
        family = re.search(rb'\spitchFamily="(\d+)"', node)
        face = re.search(rb'\stypeface="([^"]*)"', node)
        panose = re.search(rb'\spanose="([0-9A-Fa-f]{20})"', node)
        if not family or not face or face.group(1) in embedded or int(family.group(1)) >> 4:
            return node
        if panose and panose.group(1)[:2] == b"02" and all(v <= 15 for v in bytes.fromhex(panose.group(1).decode())):
            return node
        return node.replace(family.group(0), b"")
    for name in [n for n in package.parts if FONT_PARTS.fullmatch(n) or SLIDE_PARTS.fullmatch(n)]:
        data = package.parts[name]
        new = FONT_NODE.sub(fixed, data)
        if new != data:
            package.parts[name] = new


FONT_REL = f"{OFFICE_REL}/font"
EMBEDDED_ORIGINS = ("licensed-asset", "catalog")


def _font_key(name):
    return re.sub(r"[^0-9a-zа-яё]", "", (name or "").lower())


def embed_fonts(package, presentation_part, doc):
    """Owner decision 46 (28.09.2026), part 2 — PPTX = PDF: the open static faces that drew the deck (preview, HTML, PDF, the
    fit check) under the name of a typeface of the template are embedded in the PPTX as PowerPoint embeds fonts
    (ppt/fonts/*.fntdata in EOT, font_assets.wrap_eot; <p:embeddedFontLst>), so PowerPoint sets the lines in the font the PDF
    shows. These are the pinned assets (assets/fonts, OFL) and faces of the catalog of the server whose own family is that
    typeface. Not embedded: typefaces the template embeds itself (they stay as the template has them), metric substitutes
    (the PPTX names Calibri or Arial, which the viewer has, with the same widths), faces of the content package (the licence
    is the user's), variable faces (PowerPoint embeds static TrueType only) and faces whose embedding is restricted; the
    last two are reported (the viewer sees a substitute). Returns (embedded, not_embedded) for export.json."""
    text = package.parts[presentation_part].decode("utf-8")
    present = {_font_key(unescape(v)) for v in re.findall(r'<p:embeddedFont>\s*<p:font typeface="([^"]*)"', text)}
    used = {_font_key(doc["style"].get("font"))} | {_font_key(e.get("font")) for s in doc["slides"] for e in s["elements"] if e.get("font")}
    faces, embedded, skipped = {}, [], []
    for f in doc["style"]["fonts"]:
        family, origin = f.get("family"), f.get("origin")
        if _font_key(family) not in used or _font_key(family) in present or origin not in EMBEDDED_ORIGINS or f.get("weight") not in ("regular", "bold"):
            continue
        if origin == "catalog" and _font_key(f.get("served_by")) != _font_key(family):
            skipped.append({"family": family, "weight": f["weight"], "reason": "other-family", "served_by": f.get("served_by")})
            continue
        try:
            eot = wrap_eot(base64.b64decode(f["data"], validate=True))
        except ValueError as exc:
            reason = "variable" if "Variable" in str(exc) else "restricted" if "restricted" in str(exc) else "not-truetype"
            skipped.append({"family": family, "weight": f["weight"], "reason": reason, "served_by": f.get("served_by")})
            continue
        faces.setdefault(family, {}).setdefault(f["weight"], (eot, f))
    if not faces:
        return embedded, skipped
    package.defaults.setdefault("fntdata", "application/x-fontdata")
    nodes = []
    for n, (family, weights) in enumerate(sorted(faces.items())):
        entries = []
        for weight in ("regular", "bold"):
            if weight not in weights:
                continue
            eot, f = weights[weight]
            part = f"ppt/fonts/vsp-font{n}-{weight}.fntdata"
            package.put(part, eot)
            rid = package.next_rel_id(presentation_part)
            package.set_rels(presentation_part, package.rels(presentation_part) + [Rel(rid, FONT_REL, package.relative(presentation_part, part))])
            entries.append(f'<p:{weight} r:id="{rid}"/>')
            embedded.append({"family": family, "weight": weight, "origin": f.get("origin"), "sha256": f.get("sha256"), "part": part, "bytes": len(eot)})
        nodes.append(f'<p:embeddedFont><p:font typeface={quoteattr(family)}/>{"".join(entries)}</p:embeddedFont>')
    if "</p:embeddedFontLst>" in text:
        text = text.replace("</p:embeddedFontLst>", "".join(nodes) + "</p:embeddedFontLst>", 1)
    else:
        # Schema order of p:presentation: ... sldSz, notesSz, smartTags, embeddedFontLst, custShowLst ...
        anchor = re.search(r'<p:smartTags\b[^>]*/>', text) or re.search(r'<p:notesSz\b[^>]*/>', text)
        if anchor is None:
            raise ValueError("Не найдено место для списка встроенных шрифтов в presentation.xml")
        at = anchor.end()
        text = text[:at] + "<p:embeddedFontLst>" + "".join(nodes) + "</p:embeddedFontLst>" + text[at:]
    opening = re.search(r"<p:presentation\b[^>]*>", text).group(0)
    fixed = re.sub(r'\sembedTrueTypeFonts="[^"]*"', "", opening).replace("<p:presentation", '<p:presentation embedTrueTypeFonts="1"', 1)
    package.parts[presentation_part] = text.replace(opening, fixed, 1).encode("utf-8")
    return embedded, skipped


def export_carrier_pptx(doc, reference):
    if digest(reference) != doc.get("export", {}).get("reference_sha256"):
        raise ValueError("Образец не совпадает с документом")
    package = Package(reference)
    repair_panose(package)
    repair_font_class(package)
    source_counts = package_counts(package)
    presentation_part = package.related("", "officeDocument")[0]
    package.overrides[presentation_part] = PRESENTATION_TYPE
    # Furniture is cloned from sample slides, so it is read before they are removed.
    furniture, clones = {}, {}
    for slide in doc["slides"]:
        for e in slide["elements"]:
            b = e.get("binding", {})
            if b.get("kind") == "furniture":
                furniture[(slide["id"], e["id"])] = source_shape(package, b["source"]["part"], b["source"]["shape"])
            if b.get("kind") == "clone":
                key = (b["source"]["part"], str(b["source"]["shape"]))
                if key not in clones:
                    clones[key] = source_shape(package, *key)
    presentation_text = clear_slides(package, presentation_part)
    # Text of every slide (owner decision 21, 1a): notes slides on the notes master of the reference.
    notes = (doc.get("speaker_notes") or {}).get("slides") or {}
    language = (doc.get("speaker_notes") or {}).get("language", "ru")
    master_part = None
    if notes:
        master_part, presentation_text = notes_master(package, presentation_part, presentation_text)
    added, written_notes = [], 0
    for number, slide in enumerate(doc["slides"], 1):
        layout_part = slide["layout"]["part"]
        if layout_part not in package.parts or package.content_type(layout_part) != LAYOUT_TYPE:
            raise ValueError(f"Макет слайда не найден в образце: {layout_part}")
        part = f"ppt/slides/slide{number}.xml"
        rels = [Rel("rId1", f"{OFFICE_REL}/slideLayout", package.relative(part, layout_part))]
        media, shapes, next_id = {}, [], 2
        def rel(rel_type, target, external=False):
            rid = f"rId{len(rels)+1}"
            rels.append(Rel(rid, rel_type, target if external else package.relative(part, target), external))
            return rid
        # A7: every source shape is written once, at its first element; its text element supplies the facts.
        clone_texts = {}
        for e in slide["elements"]:
            if e["type"] == "text" and e["binding"].get("kind") == "clone":
                clone_texts.setdefault((e["binding"]["source"]["part"], str(e["binding"]["source"]["shape"])), []).append(e)
        written = set()
        for e in slide["elements"]:
            b = e["binding"]
            if b["kind"] == "inherited":
                continue
            if b["kind"] == "clone":
                key = (b["source"]["part"], str(b["source"]["shape"]))
                if key in written:
                    continue
                written.add(key)
                texts = clone_texts.get(key, [])
                item, replaced = clones[key], set()
                # G3: a pictogram chosen for the card replaces the picture of the sample one: a new media part, and the
                # picture loses the crop and the SVG version of the old one (srcRect, extLst), which PowerPoint would draw.
                chosen = {x["icon"]["asset"]: x for x in slide["elements"] if x["type"] == "image" and x.get("icon")
                          and x["binding"].get("kind") == "clone" and (x["binding"]["source"]["part"], str(x["binding"]["source"]["shape"])) == key}
                if chosen:
                    rels_of = {}
                    for old, (rel_type, target, external) in item["rels"].items():
                        source = digest(package.parts[target]) if not external and target in package.parts else None
                        if rel_type.endswith("/image") and source in chosen:
                            data = base64.b64decode(chosen[source]["data"], validate=True)
                            target = f"ppt/media/vsp-{digest(data)[:12]}.png"
                            package.put(target, data)
                            if "png" not in package.defaults:package.defaults["png"] = "image/png"
                            replaced.add(package.relative(part, target))
                        rels_of[old] = (rel_type, target, external)
                    item = {**item, "rels": rels_of}
                node = clone_xml(item, texts, b, rel)
                for fill in node.iter(f"{{{P}}}blipFill"):
                    blip = fill.find("a:blip", NS)
                    target = next((r.target for r in rels if blip is not None and r.id == blip.get(f"{{{R}}}embed")), None)
                    if target in replaced:
                        for child in [c for c in fill if c.tag == f"{{{A}}}srcRect"] + [c for c in blip if c.tag == f"{{{A}}}extLst"]:
                            (fill if child.tag.endswith("srcRect") else blip).remove(child)
                for n in node.iter(f"{{{P}}}cNvPr"):
                    n.set("id", str(next_id)); next_id += 1
                shapes.append(ET.tostring(node, encoding="unicode"))
                continue
            if b["kind"] == "furniture":
                # Item 31: an item of mandatory decor copied from a sample slide has several preview elements; its shape is
                # written once, with the running title replaced by the title of the brief.
                key = ("furniture", b["source"]["part"], str(b["source"]["shape"]))
                if key in written:
                    continue
                written.add(key)
                item = furniture[(slide["id"], e["id"])]
                node = ET.fromstring(item["xml"])
                # Holdout-next2 26.09.2026 (Toastmasters, "Subhead VS. Subhead"): a filled card of a sample slide that the
                # variant moved (placement.reflow_grid: the two halves stacked) kept its old frame in the PPTX, while the text
                # went to the new one, and white text ran onto the white half. A single unrotated shape takes the frame of its
                # element; items of the mandatory decor (several preview elements for one shape) keep theirs.
                frame = node.find("p:spPr/a:xfrm", NS) if node.tag == f"{{{P}}}sp" and not e.get("brand_item") else None
                if frame is not None and not frame.get("rot") and frame.find("a:off", NS) is not None and frame.find("a:ext", NS) is not None:
                    fx, fy = (int(frame.find("a:off", NS).get(k)) / EMU for k in ("x", "y"))
                    fr, fb = fx + int(frame.find("a:ext", NS).get("cx")) / EMU, fy + int(frame.find("a:ext", NS).get("cy")) / EMU
                    # A shape that bleeds off the slide has the clipped box in the model (patterns.py); that is no move.
                    seen = (max(0, fx), max(0, fy), min(doc["width"], fr) - max(0, fx), min(doc["height"], fb) - max(0, fy))
                    if max(abs(a - v) for a, v in zip(seen, (e["x"], e["y"], e["w"], e["h"]))) > .5:
                        _set_frame(node, (e["x"], e["y"], e["w"], e["h"]))
                for change in b.get("replace_text", []):
                    replace_text(node, change)
                mapping = {old:rel(rel_type, target, external) for old, (rel_type, target, external) in sorted(item["rels"].items())}
                for n in node.iter():
                    for key, value in list(n.attrib.items()):
                        if key.startswith(f"{{{R}}}") and value in mapping:n.set(key, mapping[value])
                    if n.tag == f"{{{P}}}cNvPr":
                        n.set("id", str(next_id)); next_id += 1
                shapes.append(ET.tostring(node, encoding="unicode"))
                continue
            if b["kind"] == "placeholder":
                shapes.append(placeholder_xml(e, next_id, b))
            elif e["type"] == "chart":
                # G1: a native chart with its workbook; the colours are scheme colours of the theme of this slide.
                shapes.append(charts.frame_xml(e, next_id, charts.write_chart(package, e, rel)))
            else:
                rid = None
                if e["type"] == "image":
                    data = base64.b64decode(e["data"], validate=True)
                    ext = "png" if e["mime"] == "image/png" else "jpg"
                    target = f"ppt/media/vsp-{digest(data)[:12]}.{ext}"
                    if target in package.parts and package.parts[target] != data:
                        raise ValueError(f"Конфликт имени изображения: {target}")
                    package.put(target, data)
                    if ext not in package.defaults:package.defaults[ext] = "image/png" if ext == "png" else "image/jpeg"
                    rid = media.get(target) or rel(f"{OFFICE_REL}/image", target)
                    media[target] = rid
                shapes.append(element_xml(e, next_id, rid))
            next_id += 1
        package.put(part, slide_xml(slide, shapes), SLIDE_TYPE)
        text = (notes.get(slide["id"]) or {}).get("text")
        if text:
            notes_part = f"ppt/notesSlides/notesSlide{number}.xml"
            package.put(notes_part, notes_xml(text, language), NOTES_TYPE)
            package.set_rels(notes_part, [Rel("rId1", f"{OFFICE_REL}/notesMaster", package.relative(notes_part, master_part)),
                                          Rel("rId2", f"{OFFICE_REL}/slide", package.relative(notes_part, part))])
            rels.append(Rel(f"rId{len(rels)+1}", f"{OFFICE_REL}/notesSlide", package.relative(part, notes_part)))
            written_notes += 1
        package.set_rels(part, rels)
        rel_id = package.next_rel_id(presentation_part)
        package.set_rels(presentation_part, package.rels(presentation_part)+[Rel(rel_id, f"{OFFICE_REL}/slide", package.relative(presentation_part, part))])
        added.append((part, rel_id))
    entries = "".join(f'<p:sldId id="{256+n}" r:id="{rel_id}"/>' for n, (_, rel_id) in enumerate(added))
    package.parts[presentation_part] = presentation_text.replace("@@SLIDES@@", entries).encode("utf-8")
    fonts_embedded, fonts_not_embedded = embed_fonts(package, presentation_part, doc)
    app = "docProps/app.xml"
    if app in package.parts:
        text = package.parts[app].decode("utf-8")
        text = re.sub(r"<Slides>\d+</Slides>", f"<Slides>{len(added)}</Slides>", text)
        text = re.sub(r"<Notes>\d+</Notes>", f"<Notes>{written_notes}</Notes>", text)
        text = re.sub(r"<HiddenSlides>\d+</HiddenSlides>", "<HiddenSlides>0</HiddenSlides>", text)
        text = re.sub(r"<HeadingPairs>.*?</HeadingPairs>|<TitlesOfParts>.*?</TitlesOfParts>", "", text, flags=re.S)
        package.parts[app] = text.encode("utf-8")
    # Item 37, cause 6: footer lines of layouts and masters that name the sample presentation name the deck.
    footers = (doc.get("template") or {}).get("running_footers") or {}
    rewritten = 0
    for part, shapes in footers.get("parts", {}).items():
        if part in package.parts:
            package.parts[part] = rewrite_running_footers(package.parts[part], set(shapes), footers.get("cover_title"), doc["brief"]["title"])
            rewritten += 1
    removed = package.collect_garbage()
    data = package.save()
    issues = self_check(data, doc)
    if issues:
        raise ValueError("Самопроверка пакета не пройдена: "+"; ".join(issues[:10]))
    return data, {"mode":"carrier", "reference_sha256":doc["export"]["reference_sha256"], "slides":len(added), "notes_slides":written_notes, "removed_parts":len(removed),
                  "source_package":source_counts, "output_package":package_counts(Package(data)), "bytes":len(data), "self_check":[],
                  "fonts_embedded":fonts_embedded, "fonts_not_embedded":fonts_not_embedded,
                  **({"running_footers":rewritten} if rewritten else {})}


# ---- self-check -------------------------------------------------------------------
def validate(data):
    """Structural checks of an output package, as in the spike. An empty list means no findings."""
    issues = []
    package = Package(data)
    for part in list(package.parts):
        if part.endswith(".rels"):
            continue
        if package.content_type(part) is None:
            issues.append(f"нет типа содержимого: {part}")
        for rel in package.rels(part):
            if not rel.external and package.resolve(part, rel.target) not in package.parts:
                issues.append(f"связь в никуда: {part} -> {rel.target}")
    presentation_part = package.related("", "officeDocument")[0]
    if package.content_type(presentation_part) != PRESENTATION_TYPE:
        issues.append(f"тип основной части не презентация: {package.content_type(presentation_part)}")
    root = parse_xml(package.parts[presentation_part])
    rels = {r.id:r for r in package.rels(presentation_part)}
    ids = [n.get("id") for n in root.findall("p:sldIdLst/p:sldId", NS)]
    if len(ids) != len(set(ids)):
        issues.append("повтор идентификатора слайда")
    for node in root.findall("p:sldIdLst/p:sldId", NS):
        rel = rels.get(node.get(f"{{{R}}}id"))
        if rel is None:
            issues.append("слайд без связи")
            continue
        slide = package.resolve(presentation_part, rel.target)
        slide_root = parse_xml(package.parts[slide])
        if len(package.related(slide, "slideLayout")) != 1:
            issues.append(f"у слайда не один макет: {slide}")
        # Every slide placeholder must have a partner in its layout, otherwise nothing is inherited.
        for layout_part in package.related(slide, "slideLayout"):
            layout_root = parse_xml(package.parts[layout_part])
            partners = {(ph.get("type", "obj"), ph.get("idx", "0")) for ph in layout_root.iter(f"{{{P}}}ph")}
            for ph in slide_root.iter(f"{{{P}}}ph"):
                key = (ph.get("type", "obj"), ph.get("idx", "0"))
                by_type = key[0] in ("title", "ctrTitle") and any(t in ("title", "ctrTitle") for t, _ in partners)
                if key not in partners and not by_type:
                    issues.append(f"плейсхолдер {key} без пары в макете: {slide}")
        shape_ids = [n.get("id") for n in slide_root.iter(f"{{{P}}}cNvPr")]
        if len(shape_ids) != len(set(shape_ids)):
            issues.append(f"повтор id фигуры: {slide}")
        known = {r.id for r in package.rels(slide)}
        for other in slide_root.iter():
            for key, value in other.attrib.items():
                if key.startswith(f"{{{R}}}") and value not in known:
                    issues.append(f"неразрешённая ссылка {value}: {slide}")
    stale = [p for p in package.parts if re.fullmatch(r"ppt/slides/slide\d+\.xml", p)
             and p not in {package.resolve(presentation_part, r.target) for r in rels.values()}]
    issues += [f"осиротевший слайд: {p}" for p in stale]
    return issues


def self_check(data, doc):
    """validate() plus: graphicData URIs from the white list only, and each slide on the layout the document names."""
    issues = validate(data)
    package = Package(data)
    presentation_part = package.related("", "officeDocument")[0]
    slides = slide_parts(package, presentation_part)
    if len(slides) != len(doc["slides"]):
        issues.append(f"слайдов {len(slides)} вместо {len(doc['slides'])}")
    for part, slide in zip(slides, doc["slides"]):
        for node in parse_xml(package.parts[part]).iter(f"{{{A}}}graphicData"):
            if node.get("uri") not in GRAPHIC_URIS:
                issues.append(f"a:graphicData/@uri вне белого списка: {node.get('uri')}: {part}")
        if package.related(part, "slideLayout") != [slide["layout"]["part"]]:
            issues.append(f"макет слайда не совпадает с документом: {part}")
    return issues
