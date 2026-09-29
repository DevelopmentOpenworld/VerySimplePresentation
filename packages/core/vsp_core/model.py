"""Versioned, renderer-independent document. Coordinates are CSS pixels."""
import copy
import math
from collections import Counter


def validate_brief(brief, config):
    if brief.get("schema") != "vsp.brief/1":
        raise ValueError("Ожидается бриф vsp.brief/1")
    for field in ("title", "subtitle", "disclosure"):
        if not isinstance(brief.get(field), str) or not brief[field].strip() or len(brief[field]) > 180:
            raise ValueError(f"Неверное поле брифа: {field}")
    sections = brief.get("sections", [])
    if not 1 <= len(sections) < config["max_slides"]:
        raise ValueError("Неподдерживаемое число разделов")
    source_ids = {s["id"] for s in brief.get("sources", [])}
    ids, section_ids = set(), set()
    for section in sections:
        if section["id"] in section_ids:
            raise ValueError("Повторяется ID раздела")
        section_ids.add(section["id"])
        if section.get("kind") not in ("text", "table") or not 1 <= len(section["title"]) <= config["title_max_chars"]:
            raise ValueError("Неподдерживаемый раздел")
        facts = section.get("facts", [])
        if not 1 <= len(facts) <= config["max_facts_per_section"]:
            raise ValueError("В разделе требуется от 1 до 4 фактов")
        if section["kind"] == "table" and not 2 <= len(section.get("columns", [])) <= 4:
            raise ValueError("В таблице требуется от 2 до 4 столбцов")
        for fact in facts:
            if fact["id"] in ids or fact["source"] not in source_ids:
                raise ValueError("Повтор ID факта или неизвестный источник")
            ids.add(fact["id"])
            if section["kind"] == "text":
                if not isinstance(fact.get("text"), str) or not 1 <= len(fact["text"]) <= config["body_max_chars"]:
                    raise ValueError("Текст факта пуст или слишком длинный")
            elif len(fact.get("cells", [])) != len(section["columns"]) or any(not isinstance(c,str) or len(c)>100 for c in fact["cells"]):
                raise ValueError("Некорректная строка таблицы")
        # G2: key numbers are verbatim pieces of the facts; a process is 3 or 4 facts in order (charts.check_metrics).
        if "metrics" in section or "process" in section:
            from .charts import check_metrics, check_process
            violations = (check_metrics(section["metrics"], section) if "metrics" in section else []) + (check_process(section["process"], section) if "process" in section else [])
            if violations:
                raise ValueError("Неверные ключевые числа или шаги раздела "+section["id"]+": "+", ".join(v["reason"] for v in violations))
        # G1: a chart of a text section shows numbers of its facts only (charts.check_chart).
        if "chart" in section:
            from .charts import check_chart
            violations = check_chart(section["chart"], section)
            if violations:
                raise ValueError("Неверный график раздела "+section["id"]+": "+", ".join(v["reason"] for v in violations))


def build_documents(brief, style, config):
    validate_brief(brief, config)
    if config.get('composition_mode') == 'reference_patterns':
        from .composer import build_reference_documents
        from .title_places import place_titles
        # Session 16: titles into the band and onto the panel of the scene of the template (title_places), before the check.
        return place_titles(bind_export(build_reference_documents(brief, style, config), style, config), style)
    documents = []
    w, h, m = style["width"], style["height"], style["margin"]
    title_size = min(42, max(30, style["title_size"]))
    for variant in config["variants"]:
        doc = {"schema":"vsp.document/1", "revision":0, "variant":variant, "width":w, "height":h,
               "style":{k:copy.deepcopy(style[k]) for k in ("font", "accent", "ink", "fonts", "header_asset")},
               "brief":copy.deepcopy(brief), "slides":[]}
        def new_slide(sid, role):
            slide = {"id":sid, "role":role, "background":"FFFFFF", "elements":[]}
            doc["slides"].append(slide)
            return slide
        def text(slide, eid, value, x, y, width, height, size=None, color=None, bold=False, facts=None):
            slide["elements"].append({"id":eid, "type":"text", "text":value, "x":round(x,2), "y":round(y,2),
                "w":round(width,2), "h":round(height,2), "font":style["font"], "font_size":size or style["body_size"],
                "color":color or style["ink"], "bold":bold, "line_height":config["line_height"], "fact_ids":facts or []})
        def furniture(slide, index):
            asset = style.get("header_asset")
            if asset and asset.get('placement_scope', 'global') != 'global':
                asset = None
            footer_x = max(m,asset["box"][0]+asset["box"][2]+20) if asset and asset["box"][1]>h*.88 else m
            text(slide, "disclosure", brief["disclosure"], footer_x, h-34, w-footer_x-m-45, 20, 12, "697583")
            text(slide, "page", str(index), w-m-30, h-34, 30, 20, 12, "697583")
            if asset:
                for n,member in enumerate(asset.get('members',[asset])):
                    ax, ay, aw, ah = member["box"]
                    slide["elements"].append({"id":f"brand{n}", "type":"image", "x":ax, "y":ay, "w":aw, "h":ah,
                                               "mime":member["mime"], "data":member["data"], "fact_ids":[]})
        cover = new_slide("cover", "cover")
        if variant == "sequence":
            text(cover,"title",brief["title"],m,h*.30,w*.78,h*.22,title_size*1.4,style["accent"],True)
            text(cover,"subtitle",brief["subtitle"],m,h*.59,w*.8,60,24)
        elif variant == "split":
            text(cover,"title",brief["title"],m,h*.25,w*.43,h*.4,title_size*1.25,style["accent"],True)
            text(cover,"subtitle",brief["subtitle"],w*.55,h*.37,w*.37,h*.23,26)
        else:
            text(cover,"subtitle",brief["subtitle"],m,h*.26,w*.8,50,23)
            text(cover,"title",brief["title"],m,h*.43,w*.82,h*.3,title_size*1.5,style["accent"],True)
        furniture(cover,1)
        for index, section in enumerate(brief["sections"],2):
            slide = new_slide(section["id"], section["kind"])
            if variant == "split":
                text(slide,"title",section["title"],m,h*.25,w*.31,h*.42,title_size,style["accent"],True)
                x, y, cw, ch = w*.41, h*.23, w*.54, h*.60
            else:
                text(slide,"title",section["title"],m,h*.15,w-2*m,h*.15,title_size,style["accent"],True)
                x,y,cw,ch = m,h*.38,w-2*m,h*.43
            if section["kind"] == "text":
                facts = section["facts"]
                for n,f in enumerate(facts):
                    if variant == "columns":
                        gap = w*.025
                        fw = (cw-gap*(len(facts)-1))/len(facts)
                        fx,fy,fh = x+n*(fw+gap), y, ch
                    else:
                        fx,fy,fw,fh = x,y+n*ch/len(facts),cw,ch/len(facts)-10
                    text(slide,f["id"],f["text"],fx,fy,fw,fh,facts=[f["id"]])
            else:
                table_h = min(ch-40, (len(section["facts"])+1)*h*.13)
                slide["elements"].append({"id":"table", "type":"table", "x":x,"y":y,"w":cw,"h":table_h,
                    "rows":[section["columns"]]+[f["cells"] for f in section["facts"]],
                    "fact_ids":[f["id"] for f in section["facts"]], "font":style["font"], "font_size":min(20,style["body_size"]),
                    "color":style["ink"], "accent":style["accent"], "column_widths":[.50]+[.50/(len(section["columns"])-1)]*(len(section["columns"])-1)})
                if section.get("note"):
                    text(slide,"note",section["note"],x,y+table_h+18,cw,40,14,"697583")
            furniture(slide,index)
        documents.append(doc)
    return bind_export(documents, style, config)


def bind_export(documents, style, config):
    # Without export_mode, or with "standalone", documents stay vsp.document/1.
    if config.get("export_mode") != "carrier":
        return documents
    from .carrier_export import bind_carrier
    from .audit import template_tokens
    # C2: what the reference allows (typefaces, size scale, colours, layouts) travels with the document for the audit.
    tokens = template_tokens(style)
    return [{**bind_carrier(doc, style), "template": copy.deepcopy(tokens)} for doc in documents]


BINDING_KINDS = ("native", "inherited", "furniture", "placeholder", "clone")
INHERITED_OWNERS = ("layout", "master", "theme")
# Optional text properties of vsp.document/2, placeholder and clone bindings only (A5, A7, docs/CARRIER_PRODUCT_2026-09-21.md).
# paragraph_gap is the space before every paragraph, as a:spcBef in PowerPoint (A5b); before the first one only when
# space_first_last is not false (bodyPr spcFirstLastPara; absent means true, as native text writes it).
# caps: capitals of the placeholder (item 37, cause 9), which PowerPoint takes from the layout and the preview draws.
TEXT_LAYOUT = ("inset", "anchor", "bullet", "paragraph_gap", "space_first_last", "caps")


def check_text_layout(e):
    """Raises ValueError on an invalid optional text property; returns the names present."""
    present = [k for k in TEXT_LAYOUT if k in e]
    def number(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
    valid = True
    if "inset" in e:
        inset = e["inset"]
        valid &= isinstance(inset, list) and len(inset) == 4 and all(number(v) for v in inset) and inset[0]+inset[2] < e["w"] and inset[1]+inset[3] < e["h"]
    if "anchor" in e:
        valid &= e["anchor"] in ("t", "ctr", "b")
    if "bullet" in e:
        bullet = e["bullet"]
        valid &= (isinstance(bullet, dict) and set(bullet) == {"char", "indent", "hanging"} and isinstance(bullet["char"], str)
                  and 1 <= len(bullet["char"]) <= 2 and number(bullet["indent"]) and number(bullet["hanging"]))
    if "paragraph_gap" in e:
        valid &= number(e["paragraph_gap"])
    if "space_first_last" in e:
        valid &= isinstance(e["space_first_last"], bool)
    if "caps" in e:
        valid &= e["caps"] in ("all", "small")
    if not valid:
        raise ValueError("Некорректное оформление текста")
    return present


def binding_errors(doc):
    """vsp.document/2: how every element reaches the reference carrier (docs/CARRIER_PRODUCT_2026-09-21.md)."""
    errors = []
    for slide in doc["slides"]:
        def invalid(reason, element=None):
            errors.append({"slide":slide["id"], **({"element":element} if element is not None else {}), "code":"BINDING_INVALID", "reason":reason})
        layout = slide.get("layout")
        if not isinstance(layout, dict) or not isinstance(layout.get("part"), str) or not layout["part"]:
            invalid("slide-without-layout-part")
        if slide.get("background_binding") not in ("inherited", "native"):
            invalid("background-binding")
        for e in slide["elements"]:
            binding = e.get("binding")
            if not isinstance(binding, dict):
                invalid("missing", e["id"]); continue
            kind = binding.get("kind")
            if kind not in BINDING_KINDS:
                invalid("unknown-kind", e["id"]); continue
            if kind in ("inherited", "furniture"):
                # Item 31 (25.09.2026): text of mandatory decor copied from a sample slide (decor) is furniture as well.
                decor_text = kind == "furniture" and e["type"] == "text" and e.get("decor") is True
                if not (e.get("locked") and e.get("template_decoration") and (e["type"] in ("image", "shape") or decor_text)):
                    invalid(kind+"-requires-locked-template-decoration-image-or-shape", e["id"])
                if kind == "inherited" and binding.get("owner") not in INHERITED_OWNERS:
                    invalid("inherited-owner", e["id"])
                if kind == "furniture":
                    source = binding.get("source")
                    if not isinstance(source, dict) or not str(source.get("part", "")).startswith("ppt/slides/") or not source.get("shape"):
                        invalid("furniture-source-not-a-sample-slide", e["id"])
            if kind == "placeholder" and e["type"] != "text":
                invalid("placeholder-requires-text", e["id"])
            if e["type"] == "chart" and kind != "native":
                invalid("chart-requires-native", e["id"])
            if kind == "clone":
                # A7: a shape of a sample slide cloned with a new frame; its decor stays locked, its text holds facts.
                source, frame = binding.get("source"), binding.get("frame")
                if not isinstance(source, dict) or not str(source.get("part", "")).startswith("ppt/slides/") or not source.get("shape"):
                    invalid("clone-source-not-a-sample-slide", e["id"])
                if not (isinstance(frame, list) and len(frame) == 4 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in frame) and frame[2] > 0 and frame[3] > 0):
                    invalid("clone-frame", e["id"])
                # A7 v2: or the label of one fact (a heading of the card), with no fact of its own.
                if e["type"] == "text" and not e.get("fact_ids") and not e.get("label_of"):
                    invalid("clone-text-requires-facts", e["id"])
                if e.get("label_of") and (e["type"] != "text" or e.get("fact_ids")):
                    invalid("label-with-facts", e["id"])
                if e["type"] in ("image", "shape") and not (e.get("locked") and e.get("template_decoration")):
                    invalid("clone-decor-requires-locked-template-decoration", e["id"])
                if e["type"] == "table":
                    invalid("clone-table", e["id"])
            # The native text box serializer does not write these, so the preview would differ from the PPTX;
            # it writes the paragraph gap (G1: facts beside a chart).
            # Item 31: text of copied decor is written as its source shape, which the preview follows.
            if kind not in ("placeholder", "clone") and not (kind == "furniture" and e.get("decor")) and any(k in e for k in TEXT_LAYOUT if k != "paragraph_gap"):
                invalid("text-layout-requires-placeholder", e["id"])
    return errors


def audit_document(doc):
    errors, warnings = [], []
    if doc.get("schema") not in ("vsp.document/1", "vsp.document/2"):
        raise ValueError("Неизвестная схема документа")
    if not (400 <= doc["width"] <= 3000 and 250 <= doc["height"] <= 2000):
        raise ValueError("Неверный размер документа")
    expected = {f["id"]:f for s in doc["brief"]["sections"] for f in s["facts"]}
    found = Counter()
    expected_slides = {"cover"} | {s["id"] for s in doc["brief"]["sections"]}
    if {s["id"] for s in doc["slides"]} != expected_slides or len(doc["slides"]) != len(expected_slides):
        errors.append({"code":"SLIDE_SET", "message":"Потерян или повторен слайд"})
    for slide in doc["slides"]:
        section=next((s for s in doc['brief']['sections'] if s['id']==slide['id']),None)
        by_id={e['id']:e for e in slide['elements']}
        contract=slide.get('style_contract')
        if contract:
            from .style_compatibility import decoration_signature
            if slide['background']!=contract['background']:errors.append({'slide':slide['id'],'code':'STYLE_BACKGROUND_CHANGED'})
            for eid,signature in contract['decorations'].items():
                if eid not in by_id or decoration_signature(by_id[eid])!=signature:
                    errors.append({'slide':slide['id'],'element':eid,'code':'STYLE_DECORATION_CHANGED'})
        if by_id.get('disclosure',{}).get('text')!=doc['brief']['disclosure']:
            errors.append({'slide':slide['id'],'code':'DISCLOSURE_CHANGED'})
        if section and section['kind']=='table':
            if by_id.get('table',{}).get('rows',[[]])[0]!=section['columns']:
                errors.append({'slide':slide['id'],'code':'COLUMN_HEADERS_CHANGED'})
            if section.get('note') and by_id.get('note',{}).get('text')!=section['note']:
                errors.append({'slide':slide['id'],'code':'SOURCE_NOTE_CHANGED'})
        seen_ids = set()
        for e in slide["elements"]:
            location = {"slide":slide["id"],"element":e["id"]}
            if e["id"] in seen_ids:
                errors.append({**location,"code":"DUPLICATE_ID"})
            seen_ids.add(e["id"])
            if e["type"] not in ("text","table","image","shape","chart"):
                raise ValueError("Неподдерживаемый тип объекта")
            box = [e[k] for k in ("x","y","w","h")]
            if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in box) or e["w"]<=0 or e["h"]<=0:
                raise ValueError("Некорректная геометрия")
            # Owner decision 46 (28.09.2026): decor of the template itself that bleeds off the slide (36 of 36 OUT_OF_BOUNDS of
            # QS3 and the pitch, REP-168) is the template's design, not an error of the deck.
            if (e["x"]<0 or e["y"]<0 or e["x"]+e["w"]>doc["width"]+.1 or e["y"]+e["h"]>doc["height"]+.1) and not (e.get("template_decoration") and e.get("locked")):
                errors.append({**location,"code":"OUT_OF_BOUNDS"})
            if e["type"] in ("text","table","chart") and not 6 <= e["font_size"] <= 160:
                raise ValueError("Некорректный размер шрифта")
            if e["type"] == "chart":
                # G1: the chart is the chart of the brief section of its slide, citing its facts; it does not hold facts itself.
                if not (section and section.get("chart") and e.get("chart") == section["chart"] and e.get("data_of") == section["chart"]["fact_ids"]
                        and not e.get("fact_ids")):
                    errors.append({**location,"code":"CHART_DATA_CHANGED"})
                colours = e.get("series_colors", [])
                needed = len(e.get("chart", {}).get("categories" if e.get("chart", {}).get("kind") == "pie" else "series", []))
                if len(colours) < needed or any(not isinstance(c, str) or len(c) != 6 for c in colours + [e.get("color", ""), e.get("grid_color", "")] + ([e["background"]] if e.get("background") is not None else [])):
                    raise ValueError("Некорректные цвета диаграммы")
            if e['type']=='shape' and not 0<=e.get('opacity',1)<=1:raise ValueError('Некорректная прозрачность')
            # A6b: an invisible obstacle only stands for locked decor of the reference that the layout or master draws,
            # A7: or that a cloned shape of a sample slide draws, item 37 (cause 3): or a copy of a shape of a sample slide
            # (furniture: the EMF logo of deck 19).
            if e.get('ghost') and not (doc['schema']=='vsp.document/2' and e['type']=='shape' and e.get('locked') and e.get('template_decoration')
                                       and isinstance(e.get('binding'),dict) and e['binding'].get('kind') in ('inherited','clone','furniture')):
                errors.append({**location,'code':'BINDING_INVALID','reason':'ghost-requires-locked-template-decoration-bound-inherited'})
            if check_text_layout(e) and doc["schema"] != "vsp.document/2":
                raise ValueError("Некорректное оформление текста: text-layout-requires-placeholder")
            if e["type"] == "table":
                rows = e["rows"]
                if not 2<=len(rows)<=20 or not 2<=len(rows[0])<=6 or any(len(r)!=len(rows[0]) for r in rows):
                    raise ValueError("Некорректная таблица")
                widths=e["column_widths"]
                if len(widths)!=len(rows[0]) or any(not math.isfinite(v) or v<=0 for v in widths) or abs(sum(widths)-1)>.001:
                    raise ValueError("Некорректная ширина столбцов")
            fids = e.get("fact_ids",[])
            # Several facts in one text element are its paragraphs, in the order of fact_ids.
            joined = len(fids) > 1 and e["type"] == "text"
            together = joined and all(isinstance(expected.get(fid,{}).get("text"),str) for fid in fids) and e.get("text")=="\n".join(expected[fid]["text"] for fid in fids)
            for fid in fids:
                found[fid] += 1
                f = expected.get(fid)
                correct = together if joined else bool(f) and (e.get("text")==f.get("text") if e["type"]=="text" else f.get("cells") in e.get("rows",[]))
                if not correct:
                    errors.append({**location,"code":"FACT_CHANGED", "fact":fid})
            # G2: a key number is the one the brief gives for its fact, a verbatim piece of that fact; it holds no fact itself.
            if e.get("metric_of") is not None:
                wanted = {m["fact_id"]: m["value"] for m in (section or {}).get("metrics", [])}
                if e["metric_of"] not in wanted or e.get("text") != wanted[e["metric_of"]] or e.get("fact_ids"):
                    errors.append({**location,"code":"METRIC_CHANGED","fact":e["metric_of"]})
            # A7 v2: a label of a fact is the label stage B wrote for it (numbers only from the fact, at most 4 words).
            if e.get("label_of") is not None:
                f = expected.get(e["label_of"])
                if not f or not f.get("label") or e.get("text") != f["label"]:
                    errors.append({**location,"code":"LABEL_CHANGED","fact":e["label_of"]})
            # Item 31: text of mandatory decor is a copy of the sample slide, its colour and size are the author's.
            if e["type"] == "text" and not e.get("decor"):
                if doc.get('composition_engine')=='reference-patterns-v1':
                    from .patterns import contrast, contains, blend
                    background=slide['background'];raster=False
                    for surface in slide['elements']:
                        if surface is e:break
                        if surface['type']=='shape' and contains([surface[k] for k in ('x','y','w','h')],box):
                            background=blend(surface['fill'],background,surface.get('opacity',1))
                            if surface.get('opacity',1)>=1:raster=False
                        # C4b, 3: on the carrier text on a background picture of the reference keeps the reference colour;
                        # the rendered check decides its contrast, a solid-colour comparison would be a false alarm.
                        elif (doc['schema']=='vsp.document/2' and surface['type']=='image' and surface.get('template_decoration')
                              and not surface.get('protected_asset') and contains([surface[k] for k in ('x','y','w','h')],box)):
                            raster=True
                    if contrast(e['color'],background)<3 and not raster:
                        # Holdout 25.09 (H2b): a pair of colours of the template itself the composer kept is its own colour (C3).
                        if any(a.get('reason')=='template-colour-kept' for a in e.get('style_adjustments',[])):
                            warnings.append({**location,'code':'LOW_SOLID_CONTRAST','background':background,'severity':'warning'})
                        else:
                            errors.append({**location,'code':'LOW_SOLID_CONTRAST','background':background})
                # Conservative warning only. Browser measures actual loaded-font metrics.
                rough_lines = sum(max(1,math.ceil(len(line)*e["font_size"]*.51/e["w"])) for line in e["text"].split("\n"))
                if rough_lines*e["font_size"]*e.get("line_height",1.18)>e["h"]+2:
                    warnings.append({**location,"code":"TEXT_FIT_ESTIMATE"})
        for i,a in enumerate(slide["elements"]):
            for b in slide["elements"][i+1:]:
                overlap = min(a["x"]+a["w"],b["x"]+b["w"])-max(a["x"],b["x"])>2 and min(a["y"]+a["h"],b["y"]+b["h"])-max(a["y"],b["y"])>2
                # Imported underlays deliberately sit behind native content.
                # Revision API keeps their provenance, flag and geometry server-owned.
                underlay = any(e.get('template_decoration') and e.get('locked') and e.get('source_style') and (e['type'] in ('image','shape') or e.get('decor')) for e in (a,b))
                # Owner decision 46 (28.09.2026): two texts overlap where their glyphs meet, which only the check of the
                # rendering measures (quality.js, TEXT_COLLISION); frames of 33 of 36 such pairs overlapped with no glyph
                # touching (analysis/style-experiments/20260928-pdf-fix).
                if overlap and not underlay and not (a["type"] == "text" and b["type"] == "text"):
                    errors.append({"slide":slide["id"],"code":"OBJECT_OVERLAP","elements":[a["id"],b["id"]]})
    for fid in expected:
        if found[fid]!=1:
            errors.append({"code":"FACT_COVERAGE","fact":fid,"count":found[fid]})
    if doc["schema"] == "vsp.document/2":
        errors += binding_errors(doc)
        # A6 (part): decor PowerPoint draws from the layout or master but the preview cannot is a warning, not an error.
        for slide in doc["slides"]:
            for note in slide.get("preview_notes", []):
                warnings.append({"slide":slide["id"], "code":note["code"], "media":note.get("media", []), "message":note.get("message", "")})
    # C2: checks of Appendix 1 with a severity; they do not stop the output, the user chooses fixes (audit/checks.json).
    from .audit import appendix_findings
    return {"errors":errors,"warnings":warnings,"fact_count":len(expected),"fact_occurrences":dict(found),
            "findings":appendix_findings(doc),
            "scope":"Exact structured facts, geometry and overlap; deterministic checks of Appendix 1 in findings (audit/checks.json). Browser fit is measured separately."}
