"""Small native OOXML serializer for our own model; no office editor/runtime."""
import base64
import io
import json
import re
from xml.sax.saxutils import escape, quoteattr
import zipfile
from .importer import NS, EMU

P, A, R = NS["p"],NS["a"],NS["r"]
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
# Not A+"/table": PowerPoint refuses to open the whole file when this URI is wrong, while lenient importers still find the a:tbl and draw it.
TABLE_URI = "http://schemas.openxmlformats.org/drawingml/2006/table"
ROOT = f'xmlns:a="{A}" xmlns:r="{R}" xmlns:p="{P}"'


def color(value):
    if not re.fullmatch(r"[0-9a-fA-F]{6}",value):
        raise ValueError("Неверный цвет")
    return value


def rels(items):
    return f'<Relationships xmlns="{PKG}">' + ''.join(f'<Relationship Id="{rid}" Type="{R}/{kind}" Target={quoteattr(target)}/>' for rid,kind,target in items) + '</Relationships>'


def text_body(value, e, table=False, header=False):
    font=quoteattr(e["font"])
    fill=color(e.get("accent") if header else e.get("color","151515"))
    size=round(e["font_size"]*75)
    bold=' b="1"' if header or e.get("bold") else ''
    align={'left':'l','center':'ctr','right':'r'}.get(e.get('align'),'l')
    paragraphs=[]
    # G1: the gap before every paragraph of native text (facts beside a chart), as placeholders write it (A5b).
    before=f'<a:spcBef><a:spcPts val="{round(e["paragraph_gap"]*75)}"/></a:spcBef>' if e.get('paragraph_gap') and not table else ''
    # Exact line pitch: line_height is a multiple of the size, as CSS line-height the preview draws. A percentage would be
    # of PowerPoint's single spacing (about 1.2 of the size), so 118 % drew lines some 16 % taller than the preview
    # (analysis/style-experiments/20260923-native-line-spacing).
    pitch=f'<a:lnSpc><a:spcPts val="{round(e.get("line_height",1.18)*e["font_size"]*75)}"/></a:lnSpc>'
    for line in value.split('\n'):
        paragraphs.append(f'<a:p><a:pPr algn="{align}">{pitch}{before}<a:buNone/></a:pPr><a:r><a:rPr lang="ru-RU" sz="{size}"{bold}><a:solidFill><a:srgbClr val="{fill}"/></a:solidFill><a:latin typeface={font}/><a:ea typeface={font}/><a:cs typeface={font}/></a:rPr><a:t>{escape(line)}</a:t></a:r><a:endParaRPr lang="ru-RU" sz="{size}"/></a:p>')
    tag='a:txBody' if table else 'p:txBody'
    # PowerPoint draws the gap before the first paragraph only with spcFirstLastPara; the preview draws it (renderer.js).
    first=' spcFirstLastPara="1"' if before and e.get('space_first_last',True) else ''
    return f'<{tag}><a:bodyPr wrap="square" lIns="0" rIns="0" tIns="0" bIns="0" anchor="t"{first}><a:noAutofit/></a:bodyPr><a:lstStyle/>{"".join(paragraphs)}</{tag}>'


def xfrm(e, prefix="a"):
    return f'<{prefix}:xfrm><a:off x="{round(e["x"]*EMU)}" y="{round(e["y"]*EMU)}"/><a:ext cx="{round(e["w"]*EMU)}" cy="{round(e["h"]*EMU)}"/></{prefix}:xfrm>'


def group():
    return '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'


def element_xml(e, i, rid=None):
    """One native element as slide XML; `i` is its cNvPr id, `rid` the relationship of an image (the caller owns the part)."""
    name=quoteattr(e['id'])
    if e['type']=='text':
        return f'<p:sp><p:nvSpPr><p:cNvPr id="{i}" name={name}/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr>{xfrm(e)}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln><a:noFill/></a:ln></p:spPr>{text_body(e["text"],e)}</p:sp>'
    elif e['type']=='image':
        crop='<a:srcRect '+ ' '.join(f'{k}="{round(e.get("crop",{}).get(k,0)*100000)}"' for k in ('l','t','r','b'))+'/>'
        return f'<p:pic><p:nvPicPr><p:cNvPr id="{i}" name={name}/><p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr><p:blipFill><a:blip r:embed="{rid}"/>{crop}<a:stretch><a:fillRect/></a:stretch></p:blipFill><p:spPr>{xfrm(e)}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>'
    elif e['type']=='shape':
        # G2: the arrow between steps of a process is a preset shape with its default adjustments (renderer.js draws the same).
        geometry=e['geometry'] if e['geometry'] in ('rect','roundRect','rightArrow') else 'rect'
        opacity=round(e.get('opacity',1)*100000)
        return f'<p:sp><p:nvSpPr><p:cNvPr id="{i}" name={name}/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>{xfrm(e)}<a:prstGeom prst="{geometry}"><a:avLst/></a:prstGeom><a:solidFill><a:srgbClr val="{color(e["fill"])}"><a:alpha val="{opacity}"/></a:srgbClr></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr></p:sp>'
    elif e['type']=='table':
        grid=''.join(f'<a:gridCol w="{round(e["w"]*v*EMU)}"/>' for v in e['column_widths'])
        rows=[]
        for row_n,row in enumerate(e['rows']):
            edges=''.join(f'<a:ln{side} w="9525"><a:solidFill><a:srgbClr val="{color(e.get("border_color","000000"))}"/></a:solidFill></a:ln{side}>' for side in ('L','R','T','B'))
            fill=e.get('header_fill','F3F6FA') if row_n==0 else e.get('row_fills',['F3F6FA','FFFFFF'])[row_n%2]
            cells=''.join(f'<a:tc>{text_body(c,e,True,row_n==0)}<a:tcPr marL="76200" marR="76200" marT="76200" marB="76200">{edges}<a:solidFill><a:srgbClr val="{color(fill)}"/></a:solidFill></a:tcPr></a:tc>' for c in row)
            rows.append(f'<a:tr h="{round(e["h"]*EMU/len(e["rows"]))}">{cells}</a:tr>')
        return f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{i}" name={name}/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>{xfrm(e,"p")}<a:graphic><a:graphicData uri="{TABLE_URI}"><a:tbl><a:tblPr firstRow="1"/><a:tblGrid>{grid}</a:tblGrid>{"".join(rows)}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>'
    return None


def export_pptx(doc):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        overrides=[]
        def put(part, data, content_type=None):
            z.writestr(part, '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'+data if isinstance(data,str) else data)
            if content_type:
                overrides.append(f'<Override PartName="/{part}" ContentType="{content_type}"/>')
        ppt='application/vnd.openxmlformats-officedocument.presentationml.'
        put('_rels/.rels',rels([('rId1','officeDocument','ppt/presentation.xml')]))
        pres_rels=[('rId1','slideMaster','slideMasters/slideMaster1.xml')]
        slide_ids=[]
        for n,s in enumerate(doc['slides'],1):
            slide_ids.append(f'<p:sldId id="{255+n}" r:id="rId{n+1}"/>')
            pres_rels.append((f'rId{n+1}','slide',f'slides/slide{n}.xml'))
            elements=[]
            sr=[('rId1','slideLayout','../slideLayouts/slideLayout1.xml')]
            for i,e in enumerate(s['elements'],2):
                if e['type']=='image':
                    ext='png' if e['mime']=='image/png' else 'jpg'
                    part=f'ppt/media/image{n}_{i}.{ext}'
                    put(part,base64.b64decode(e['data'],validate=True))
                    sr.append((f'rId{i}','image',f'../media/image{n}_{i}.{ext}'))
                value=element_xml(e,i,f'rId{i}')
                if value is not None:elements.append(value)
            put(f'ppt/slides/slide{n}.xml',f'<p:sld {ROOT}><p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="{color(s["background"])}"/></a:solidFill><a:effectLst/></p:bgPr></p:bg><p:spTree>{group()}{"".join(elements)}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>',ppt+'slide+xml')
            put(f'ppt/slides/_rels/slide{n}.xml.rels',rels(sr))
        font_nodes=[]
        grouped={}
        # Only the faces of the template and the pinned assets carry an EOT; faces of the content package and of the fonts
        # of the server (item 38) serve the drawing and are never embedded in the PPTX.
        for i,font in enumerate(f for f in doc['style']['fonts'] if f.get('eot')):
            part=f'fonts/font{i}.fntdata'
            put('ppt/'+part,base64.b64decode(font['eot'],validate=True))
            if font.get('license_text'):put(f'ppt/fonts/font{i}-LICENSE.txt',font['license_text'],'text/plain')
            rid=f'rIdFont{i}'
            pres_rels.append((rid,'font',part))
            grouped.setdefault(font['family'],[]).append(f'<p:{font["weight"]} r:id="{rid}"/>')
        for family,entries in grouped.items():
            font_nodes.append(f'<p:embeddedFont><p:font typeface={quoteattr(family)}/>{"".join(entries)}</p:embeddedFont>')
        font_xml=f'<p:embeddedFontLst>{"".join(font_nodes)}</p:embeddedFontLst>' if font_nodes else ''
        put('ppt/presentation.xml',f'<p:presentation {ROOT} embedTrueTypeFonts="1"><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst><p:sldIdLst>{"".join(slide_ids)}</p:sldIdLst><p:sldSz cx="{round(doc["width"]*EMU)}" cy="{round(doc["height"]*EMU)}"/><p:notesSz cx="6858000" cy="9144000"/>{font_xml}<p:defaultTextStyle/></p:presentation>',ppt+'presentation.main+xml')
        put('ppt/_rels/presentation.xml.rels',rels(pres_rels))
        cmap='<p:clrMap accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" bg1="lt1" bg2="lt2" folHlink="folHlink" hlink="hlink" tx1="dk1" tx2="dk2"/>'
        put('ppt/slideMasters/slideMaster1.xml',f'<p:sldMaster {ROOT}><p:cSld><p:spTree>{group()}</p:spTree></p:cSld>{cmap}<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst><p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles></p:sldMaster>',ppt+'slideMaster+xml')
        put('ppt/slideMasters/_rels/slideMaster1.xml.rels',rels([('rId1','slideLayout','../slideLayouts/slideLayout1.xml'),('rId2','theme','../theme/theme1.xml')]))
        put('ppt/slideLayouts/slideLayout1.xml',f'<p:sldLayout {ROOT} type="blank" preserve="1"><p:cSld name="VerySimplePresentation model"><p:spTree>{group()}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>',ppt+'slideLayout+xml')
        put('ppt/slideLayouts/_rels/slideLayout1.xml.rels',rels([('rId1','slideMaster','../slideMasters/slideMaster1.xml')]))
        colors={'dk1':'151515','lt1':'FFFFFF','dk2':'202020','lt2':'F3F6FA','accent1':doc['style']['accent'],'accent2':'697583','accent3':'FF9500','accent4':'0077FF','accent5':'DDE3EA','accent6':'D6ECFF','hlink':'0077FF','folHlink':'697583'}
        palette=''.join(f'<a:{k}><a:srgbClr val="{color(v)}"/></a:{k}>' for k,v in colors.items())
        fonts=''.join(f'<a:{role}Font><a:latin typeface={quoteattr(doc["style"]["font"])}/><a:ea typeface=""/><a:cs typeface=""/></a:{role}Font>' for role in ('major','minor'))
        fill='<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
        theme=f'<a:theme xmlns:a="{A}" name="VerySimplePresentation sampled"><a:themeElements><a:clrScheme name="Sample">{palette}</a:clrScheme><a:fontScheme name="Sample">{fonts}</a:fontScheme><a:fmtScheme name="Native"><a:fillStyleLst>{fill*3}</a:fillStyleLst><a:lnStyleLst>'+''.join(f'<a:ln w="{v}">{fill}<a:prstDash val="solid"/></a:ln>' for v in (9525,25400,38100))+f'</a:lnStyleLst><a:effectStyleLst>{"<a:effectStyle><a:effectLst/></a:effectStyle>"*3}</a:effectStyleLst><a:bgFillStyleLst>{fill*3}</a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>'
        put('ppt/theme/theme1.xml',theme,'application/vnd.openxmlformats-officedocument.theme+xml')
        types='<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/><Default Extension="jpg" ContentType="image/jpeg"/><Default Extension="fntdata" ContentType="application/x-fontdata"/>'+''.join(overrides)+'</Types>'
        put('[Content_Types].xml',types)
    return out.getvalue()


# Text of every slide (owner decision 21, 1a): the words of the speaker under each slide, not printed (the PDF has the slides).
NOTES_SCRIPT=('function speakerNotes(doc,container){const notes=(doc.speaker_notes||{}).slides||{};'
    'for(const el of container.querySelectorAll(".slide")){const n=notes[el.dataset.slide];if(!n||!n.text)continue;'
    'const aside=document.createElement("aside");aside.className="notes";aside.style.maxWidth=doc.width+"px";'
    'const h=document.createElement("strong");h.textContent="Текст выступления";const p=document.createElement("p");p.textContent=n.text;'
    'aside.append(h,p);el.after(aside);}}')


def export_html(doc, renderer_js):
    payload=json.dumps(doc,ensure_ascii=False).replace('<','\\u003c')
    notes='.notes{{margin:-12px auto 24px;padding:12px 16px;box-sizing:border-box;background:#fff;border-left:4px solid #8a94a6;font:16px/1.45 Arial,sans-serif;color:#1f2933}}.notes p{{margin:6px 0 0}}' if doc.get('speaker_notes') else ''
    ready='renderDocument(doc,document.getElementById("slides")).then(r=>{speakerNotes(doc,document.getElementById("slides"));return r;})' if doc.get('speaker_notes') else 'renderDocument(doc,document.getElementById("slides"))'
    return f'<!doctype html><html lang="ru"><meta charset="utf-8"><title>{escape(doc["brief"]["title"])}</title><style>body{{margin:0;background:#e9edf2}}.slide{{margin:24px auto;background:white}}{notes.format() if notes else ""}@media print{{@page{{size:{doc["width"]}px {doc["height"]}px;margin:0}}body{{background:white}}.slide{{margin:0;break-after:page}}{".notes{display:none}" if notes else ""}}}</style><main id="slides"></main><script>{renderer_js}</script>{f"<script>{NOTES_SCRIPT}</script>" if notes else ""}<script>const doc={payload};window.ready={ready};</script></html>'
