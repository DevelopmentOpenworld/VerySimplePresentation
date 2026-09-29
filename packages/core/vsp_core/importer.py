"""Read a bounded, untrusted PPTX. No extraction to caller-controlled paths.

This is style sampling, not a fidelity-preserving whole-deck importer.
Every resolved text observation retains its slide/part/shape provenance.
"""
import base64
from collections import Counter
import hashlib
import io
import posixpath
import statistics
import struct
import xml.etree.ElementTree as ET
import zipfile
from .fonts import decode_eot

NS = {"p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
EMU = 9525  # CSS pixel at 96 DPI


def digest(data):
    return hashlib.sha256(data).hexdigest()


def xml(data):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("DTD и сущности XML не поддерживаются")
    return ET.fromstring(data)


def relationships(z, part):
    name = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if name not in z.namelist():
        return {}
    return {r.get("Id"): {"type": r.get("Type", "").rsplit("/", 1)[-1],
             "part": posixpath.normpath(posixpath.join(posixpath.dirname(part), r.get("Target", "")))}
            for r in xml(z.read(name)) if r.get("TargetMode") != "External"}


def bbox(shape):
    transform = shape.find("p:spPr/a:xfrm", NS)
    if transform is None or transform.get("rot", "0") != "0":
        return None
    off, ext = transform.find("a:off", NS), transform.find("a:ext", NS)
    if off is None or ext is None:
        return None
    return [round(int(off.get("x"))/EMU, 2), round(int(off.get("y"))/EMU, 2),
            round(int(ext.get("cx"))/EMU, 2), round(int(ext.get("cy"))/EMU, 2)]


def placeholder(shape):
    ph = shape.find("p:nvSpPr/p:nvPr/p:ph", NS)
    return (ph.get("type", "body"), ph.get("idx", "0")) if ph is not None else None


# Role of a placeholder for the typeface of its text; other types (ftr, sldNum, dt...) are roles of their own.
FACE_ROLES = {"title": "title", "ctrTitle": "cover", "subTitle": "subtitle", "body": "body", "obj": "body"}


def pictures(tree, transform=(1,1,0,0), cluster=None, object_type='pic'):
    """Resolve translation/scale of nested image groups; rotated groups are skipped."""
    if tree is None:
        return
    sx,sy,dx,dy=transform
    object_types=(object_type,) if isinstance(object_type,str) else object_type
    for child in tree:
        if child.tag.rsplit('}',1)[-1] in object_types:
            box=bbox(child)
            if box:
                x,y,w,h=box
                yield child,[x*sx+dx,y*sy+dy,w*sx,h*sy],cluster
            elif object_type=='sp' and placeholder(child):
                yield child,None,cluster
        elif child.tag.endswith('}grpSp'):
            t=child.find('p:grpSpPr/a:xfrm',NS)
            if t is None or any(t.get(k,'0') not in ('0','false') for k in ('rot','flipH','flipV')):
                continue
            off,ext,co,ce=(t.find('a:'+tag,NS) for tag in ('off','ext','chOff','chExt'))
            if any(n is None for n in (off,ext,co,ce)) or int(ce.get('cx'))==0 or int(ce.get('cy'))==0:
                continue
            ax,ay=int(ext.get('cx'))/int(ce.get('cx')),int(ext.get('cy'))/int(ce.get('cy'))
            bx=(int(off.get('x'))-ax*int(co.get('x')))/EMU
            by=(int(off.get('y'))-ay*int(co.get('y')))/EMU
            group_id=child.find('p:nvGrpSpPr/p:cNvPr',NS).get('id')
            yield from pictures(child,(sx*ax,sy*ay,dx+sx*bx,dy+sy*by),cluster or group_id,object_type)


def matching(root, key, by_type=False):
    if root is None or key is None:
        return None
    for shape in root.findall("p:cSld/p:spTree/p:sp", NS):
        candidate = placeholder(shape)
        if candidate and (candidate[0] == key[0] if by_type else candidate[1] == key[1]):
            return shape
    return None


def read_style(data):
    if len(data) > 40 * 1024**2:
        raise ValueError("Образец превышает 40 МБ")
    z = zipfile.ZipFile(io.BytesIO(data))
    if len(z.infolist()) > 8000 or sum(i.file_size for i in z.infolist()) > 180 * 1024**2:
        raise ValueError("Слишком большой распакованный PPTX")
    presentation = xml(z.read("ppt/presentation.xml"))
    size = presentation.find("p:sldSz", NS)
    width, height = int(size.get("cx"))/EMU, int(size.get("cy"))/EMU
    if not (400 <= width <= 3000 and 250 <= height <= 2000):
        raise ValueError("Неподдерживаемый размер слайда")
    pr = relationships(z, "ppt/presentation.xml")
    parts = [pr[n.get("{"+NS["r"]+"}id")]["part"] for n in presentation.findall("p:sldIdLst/p:sldId", NS)]
    if not parts or len(parts) > 150:
        raise ValueError("Нужно от 1 до 150 слайдов образца")
    from .opc import Package
    from .reference import read_reference
    package=Package(data);carrier=read_reference(package)
    # Session 16 (26.09.2026): slides of instructions at the start of the sample give no composition and no decor
    # (reference.read_inventory); the first slide after them is the sample of the cover. Their text is set in the styles of
    # the template, so the typography below still reads them (deck 38: the only slide with text is its instruction slide, and
    # without it the typeface fell from Open Sans to the Arial of the theme).
    skip=set(carrier['instruction_slides'])
    cache = {}
    def root(part):
        if part not in cache:
            cache[part] = xml(z.read(part))
        return cache[part]
    def related(part, kind):
        return next((r["part"] for r in relationships(z, part).values() if r["type"] == kind), None)
    fonts, colors, sizes, title_sizes, margins = Counter(), Counter(), Counter(), [], []
    observations, picture_candidates, unsupported = [], {}, Counter()
    # M7, cause 7 (holdout-next 25.09.2026): the typeface the text of the sample slides is set in, by role of its frame
    # (runs), for the typeface of text in layout placeholders (composer.placeholder_face).
    sample_faces = {}
    theme_parts = Counter()
    for number, part in enumerate(parts, 1):
        slide = root(part)
        layout_part = related(part, "slideLayout")
        master_part = related(layout_part, "slideMaster") if layout_part else None
        theme_part = related(master_part, "theme") if master_part else None
        theme = root(theme_part) if theme_part else None
        palette = {}
        theme_fonts = {}
        if theme is not None:
            theme_parts[theme_part] += 1
            for item in theme.findall("a:themeElements/a:clrScheme/*", NS):
                if len(item):
                    palette[item.tag.split("}")[-1]] = item[0].get("val") if item[0].tag.endswith("srgbClr") else item[0].get("lastClr")
            for role in ("major", "minor"):
                latin = theme.find(f"a:themeElements/a:fontScheme/a:{role}Font/a:latin", NS)
                theme_fonts[role] = latin.get("typeface") if latin is not None else "Arial"
        layout = root(layout_part) if layout_part else None
        master = root(master_part) if master_part else None
        for tag in ("grpSp", "graphicFrame", "cxnSp", "oleObj"):
            unsupported[tag] += len(slide.findall(".//p:"+tag, NS))
        for shape in slide.findall("p:cSld/p:spTree/p:sp", NS):
            text = "".join(n.text or "" for n in shape.findall(".//a:t", NS))
            if not text.strip():
                continue
            ph = placeholder(shape)
            inherited = [(shape, part), (matching(layout, ph), layout_part), (matching(master, ph, True), master_part)]
            rect = next((bbox(s) for s, _ in inherited if s is not None and bbox(s)), None)
            if rect is None:
                unsupported["unresolved_geometry"] += 1
                continue
            for para in shape.findall("p:txBody/a:p", NS):
                level_node = para.find("a:pPr", NS)
                level = int(level_node.get("lvl", "0")) + 1 if level_node is not None else 1
                for run in para.findall("a:r", NS):
                    t = run.find("a:t", NS)
                    if t is None or not t.text:
                        continue
                    candidates = [(run.find("a:rPr", NS), part), (para.find("a:pPr/a:defRPr", NS), part)]
                    for owner, owner_part in inherited:
                        if owner is not None:
                            candidates.extend([(owner.find(f"p:txBody/a:lstStyle/a:lvl{level}pPr/a:defRPr", NS), owner_part),
                                               (owner.find("p:txBody/a:p/a:pPr/a:defRPr", NS), owner_part)])
                    role = "titleStyle" if ph and ph[0] in ("title", "ctrTitle") else "bodyStyle" if ph else "otherStyle"
                    if master is not None:
                        candidates.append((master.find(f"p:txStyles/p:{role}/a:lvl{level}pPr/a:defRPr", NS), master_part))
                    candidates.append((presentation.find(f"p:defaultTextStyle/a:lvl{level}pPr/a:defRPr", NS), "ppt/presentation.xml"))
                    font, point, color, sources = None, None, None, {}
                    for prop, owner in candidates:
                        if prop is None:
                            continue
                        latin = prop.find("a:latin", NS)
                        if font is None and latin is not None:
                            font = latin.get("typeface")
                            sources["font"] = owner
                        if point is None and prop.get("sz"):
                            point = int(prop.get("sz"))/100
                            sources["size"] = owner
                        fill = prop.find("a:solidFill", NS)
                        if color is None and fill is not None and len(fill):
                            node = fill[0]
                            value = node.get("val")
                            color = value if node.tag.endswith("srgbClr") else palette.get({"tx1":"dk1", "bg1":"lt1"}.get(value, value))
                            if len(node):
                                unsupported["color_transform"] += 1
                    if font and font.startswith("+"):
                        font = theme_fonts.get("major" if font.startswith("+mj") else "minor")
                    if not font:
                        font = theme_fonts.get("minor", "Arial")
                        sources["font"] = theme_part or "fallback"
                    point = point or 12
                    fonts[font] += len(t.text)
                    sample_faces.setdefault(FACE_ROLES.get(ph[0], ph[0]) if ph else "free", Counter())[font] += 1
                    sizes[point] += len(t.text)
                    if color:
                        colors[color] += len(t.text)
                    if ph and ph[0] in ("title", "ctrTitle") and 15 <= point <= 50:
                        title_sizes.append(point*4/3)
                        margins.append(rect[0])
                    observations.append({"slide": number, "part": part, "shape": shape.find("p:nvSpPr/p:cNvPr", NS).get("id"),
                                         "font": font, "size_pt": point, "color": color, "box": rect, "inherited_from": sources})
        # Conservative repeated header image heuristic, never claim logo recognition.
        for owner, owner_part in ((slide, part), (layout, layout_part), (master, master_part)):
            if owner is None:
                continue
            rr = relationships(z, owner_part)
            owner_pictures=list(pictures(owner.find("p:cSld/p:spTree", NS)))
            for pic, box, cluster in owner_pictures:
                blip = pic.find("p:blipFill/a:blip", NS)
                if box is None or blip is None or box[3] <= 0:
                    continue
                crop = pic.find("p:blipFill/a:srcRect", NS)
                if crop is not None and any(int(v) for v in crop.attrib.values()):
                    continue
                x, y, w, h = box
                rel = rr.get(blip.get("{"+NS["r"]+"}embed"))
                if rel and rel["part"].lower().endswith((".png", ".jpg", ".jpeg")) and (y < height*.18 or y > height*.88) and w/h > 1.8 and w*h < width*height*.06:
                    key = rel["part"]
                    members=[]
                    for sibling,sibling_box,sibling_cluster in owner_pictures:
                        if sibling is pic or (cluster is not None and sibling_cluster==cluster):
                            sb=sibling.find('p:blipFill/a:blip',NS)
                            sr=rr.get(sb.get('{'+NS['r']+'}embed')) if sb is not None else None
                            sc=sibling.find('p:blipFill/a:srcRect',NS)
                            if sr and sr['part'].lower().endswith(('.png','.jpg','.jpeg')) and (sc is None or not any(int(v) for v in sc.attrib.values())):
                                members.append({'part':sr['part'],'box':sibling_box})
                    c = picture_candidates.setdefault(key, {"slides": set(), "part": key, "box": box, "owner": owner_part,"members":members})
                    c["slides"].add(number)
    selected_font = fonts.most_common(1)[0][0] if fonts else "Arial"
    dominant_theme = theme_parts.most_common(1)[0][0] if theme_parts else None
    if not fonts and dominant_theme:
        default_face=root(dominant_theme).find('a:themeElements/a:fontScheme/a:minorFont/a:latin',NS)
        if default_face is not None and default_face.get('typeface'):selected_font=default_face.get('typeface')
    palette = {}
    if dominant_theme:
        for item in root(dominant_theme).findall("a:themeElements/a:clrScheme/*", NS):
            if len(item):
                palette[item.tag.split("}")[-1]] = item[0].get("val") if item[0].tag.endswith("srgbClr") else item[0].get("lastClr")
    chromatic = [(c, n) for c, n in colors.items() if len(c) == 6 and max(bytes.fromhex(c))-min(bytes.fromhex(c)) > 55]
    accent = max(chromatic, key=lambda x:x[1])[0] if chromatic else palette.get("accent1", "0077FF")
    diagnostics = [{"level":"warning", "code":"STYLE_SAMPLE_ONLY", "message":"Извлечены ограниченные образцы оформления; результат требует визуальной проверки. Произвольные группы, графики, схемы и сложные заливки не восстанавливаются полностью."}]
    if unsupported:
        diagnostics.append({"level":"warning", "code":"UNSUPPORTED_SOURCE_OBJECTS", "counts":dict(unsupported), "message":"Сложные объекты образца учитываются в диагностике, но не переносятся в модель."})
    embedded = []
    for entry in presentation.findall("p:embeddedFontLst/p:embeddedFont", NS):
        family = entry.find("p:font", NS).get("typeface")
        if family != selected_font:
            continue
        for weight in ("regular", "bold"):
            item = entry.find("p:"+weight, NS)
            rel = pr.get(item.get("{"+NS["r"]+"}id")) if item is not None else None
            if not rel:
                continue
            raw = z.read(rel["part"])
            sfnt = decode_eot(raw, family, weight)
            if sfnt:
                embedded.append({"family":family, "weight":weight, "source_part":rel["part"], "sha256":digest(sfnt),
                                 "data":base64.b64encode(sfnt).decode(), "eot":base64.b64encode(raw).decode()})
    if not embedded:
        diagnostics.append({"level":"warning", "code":"FONT_AVAILABILITY", "message":f"{selected_font}: пригодный встроенный шрифт не извлечен. Браузер обязан показать подстановку."})
    asset = None
    if picture_candidates:
        c = max(picture_candidates.values(), key=lambda c:len(c["slides"]))
        if len(c["slides"]) >= 2:
            raw = z.read(c["part"])
            asset = {**c, "slides":sorted(c["slides"]), "mime":"image/png" if c["part"].endswith(".png") else "image/jpeg",
                     "data":base64.b64encode(raw).decode(), "sha256":digest(raw)}
            # A repeated cover/section asset is not necessarily global furniture.
            # Preserve the evidence, but don't stamp a minority element on every
            # generated slide before its structural role is understood.
            asset['coverage_ratio'] = len(c['slides']) / len(parts)
            asset['placement_scope'] = 'global' if asset['coverage_ratio'] >= .5 else 'reference_only'
            for member in asset['members']:
                raw=z.read(member['part'])
                member.update({'mime':'image/png' if member['part'].endswith('.png') else 'image/jpeg','data':base64.b64encode(raw).decode(),'sha256':digest(raw)})
            diagnostics.append({"level":"warning", "code":"HEADER_IMAGE_HEURISTIC", "message":"Повторяющееся изображение у края слайда выбрано по геометрии. Его роль и контраст требуют просмотра."})
            if asset['placement_scope'] == 'reference_only':
                diagnostics.append({'level':'warning','code':'PARTIAL_HEADER_ASSET',
                    'source_part':asset['part'],'source_slides':asset['slides'],'coverage_ratio':asset['coverage_ratio'],
                    'message':'Изображение встречается меньше чем на половине слайдов. Оно сохранено как ресурс образца, но не переносится на каждый новый слайд без определения его роли.'})
    # Session 16 (M7 of holdout-next3, cause 3): the decor nodes of the card samples (cloned in place) take part in the
    # catalogue rule of read_patterns; the card samples themselves do not depend on the compositions.
    from .sample_clone import read_card_samples
    card_samples,card_assets=read_card_samples(package,carrier)
    card_decor={s['slide']:[{'box':n['box'],'id':n['id'],
                             'fill':'picture' if n['kind']=='pic' else next((p.get('fill') for p in n['primitives'] if p['type']=='shape' and p.get('fill')),None) or n['kind']}
                            for n in s['nodes'] if n['role']=='decor'] for s in card_samples}
    from .patterns import read_patterns
    reference=read_patterns(z,presentation,parts,width,height,selected_font,skip,card_decor)
    diagnostics.append({'level':'warning','code':'BOUNDED_REFERENCE_PATTERNS',
        'patterns':len(reference['patterns']),'unsupported':reference['unsupported'],
        'message':'Извлечены геометрические композиции, цветовые роли и декор мастеров/макетов. Семантическое назначение, произвольные группы/диаграммы и сложные заливки пока не восстанавливаются полностью.'})
    # Item 31 (25.09.2026): the mandatory decor of the sample slides, grouped by the tone of every slide (its title colour).
    from .reference import brand_decor,slide_tone
    tones={p['source_part']:slide_tone(p['title'].get('color')) for p in reference['patterns'] if p.get('source_kind')=='slide'}
    carrier['brand_decor']=brand_decor(package,carrier['slides'],carrier['layouts'],carrier['width'],carrier['height'],tones)
    # A7: sample slides with sets of equal text cards, cloned as whole units by the composer.
    # A card sample of a catalogue slide gives no cards (and asks no pictograms of icons.choose).
    carrier['card_samples'],carrier['card_assets']=[s for s in card_samples if s['slide'] not in reference.get('catalogue_slides',())],card_assets
    # Item 31 (25.09.2026): the preview of the mandatory decor of the sample slides that copies of their shapes carry.
    from .sample_clone import brand_decor_preview
    carrier['brand_decor']['assets']=brand_decor_preview(package,carrier)
    # G3: pictograms of the sample slides and the places for them in the cards (icons.choose picks them for a brief).
    from .icons import read_icons,card_icon_slots
    carrier['icons']=read_icons(package,carrier)
    card_icon_slots(carrier['card_samples'],carrier['icons'])
    return {"schema":"vsp.style/2", "source_sha256":digest(data), "slide_count":len(parts), "width":width, "height":height,
            "font":selected_font, "font_candidates":dict(fonts), "title_size":round(statistics.median(title_sizes), 2) if title_sizes else 32,
            "body_size":max(19, min(24, sizes.most_common(1)[0][0]*4/3)) if sizes else 20,
            "margin":max(width*.035, min(width*.075, statistics.median(margins))) if margins else width*.05,
            "accent":accent, "ink":"151515", "background":"FFFFFF", "palette":palette, "theme_part":dominant_theme,
            "fonts":embedded, "header_asset":asset, "observations":observations, "diagnostics":diagnostics,'reference':reference,
            'carrier':carrier, 'sample_faces':{role:dict(faces) for role, faces in sample_faces.items()}}
