"""Fonts of the server and the fonts a template asks for (owner decision 24, plan item 38, 25.09.2026).

The catalog is every font file of the font directories: var/fonts by default, filled by scripts/fetch_fonts.py from the
lock profiles/mvp/fonts.json (google/fonts pinned to one commit); other directories, such as installed Microsoft core
fonts on the server, through VSP_FONT_DIRS (paths joined by os.pathsep; they replace the default). index.json of a
directory describes each face by the names PowerPoint writes: the family (name 1) of a static build holds weights like
"Montserrat ExtraBold", the typographic family (name 16) the common name, the full name (name 4) "Arial Bold".

The inventory lists every typeface of the template (theme, slides, layouts, masters; latin, east asian, complex script)
with the source that serves it: embedded in the template and extracted, a pinned asset, the content package, the same
family from the catalog, a metric-compatible substitute (Calibri - Carlito: same advance widths, so the same line
breaks), or none. These faces go to style['server_fonts'], not style['fonts']: they serve only the drawing (preview,
HTML, PDF, the fit check), added to the documents after the composer has chosen the typefaces (serve_fonts). The
composer takes a typeface of a frame only when the template itself serves it (style['fonts']); counting the fonts of the
server there moved frames of web-ssau from Elektra, in which all its sample slides are set, to the Calibri of its layout
defaults (regression of step 1, 25.09.2026). The PPTX keeps the name of the template and never embeds them. The inventory goes to run.json and the upload journal, so an unavailable
font can be shown to have been neither embedded nor openly available.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import xml.etree.ElementTree as ET
import zipfile

ROOT=Path(__file__).resolve().parents[3]
LOCK=ROOT/'profiles/mvp/fonts.json'
DEFAULT_DIRS=[ROOT/'var/fonts']
FONT_SUFFIXES=('.ttf','.otf','.ttc')
A='{http://schemas.openxmlformats.org/drawingml/2006/main}'
P='{http://schemas.openxmlformats.org/presentationml/2006/main}'
R='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
# Weight words of a family name written with its weight ("Open Sans Light", "Roboto Black"), CSS weights.
WEIGHTS={'thin':100,'hairline':100,'extralight':200,'ultralight':200,'light':300,'book':400,'regular':400,'normal':400,
    'medium':500,'semibold':600,'demibold':600,'demi':600,'bold':700,'extrabold':800,'ultrabold':800,'black':900,'heavy':900}


def key(name):
    return re.sub(r'[^0-9a-zа-яё]','',(name or '').lower())


def font_dirs():
    extra=[Path(p) for p in os.environ.get('VSP_FONT_DIRS','').split(os.pathsep) if p]
    return extra or DEFAULT_DIRS


def _tables(data,offset=0):
    out={}
    for i in range(struct.unpack_from('>H',data,offset+4)[0]):
        tag,_,start,length=struct.unpack_from('>4sIII',data,offset+12+16*i)
        out[tag.decode('latin-1')]=data[start:start+length]
    return out


def _names(name):
    """Name records by id: English of the Windows platform first, then any language, then Macintosh Roman; every
    family (1, 16) and full name (4) in every language as aliases (a Russian Windows may write the Russian family)."""
    _,count,start=struct.unpack_from('>HHH',name)
    found,aliases={},set()
    for i in range(count):
        platform,encoding,language,ident,length,offset=struct.unpack_from('>6H',name,6+12*i)
        raw=name[start+offset:start+offset+length]
        if platform==3 and encoding in (0,1,10):
            rank,text=(0 if language==0x409 else 1),raw.decode('utf-16-be','replace')
        elif platform==1 and encoding==0:
            rank,text=2,raw.decode('mac_roman','replace')
        else:
            continue
        if ident in (1,4,16):
            aliases.add(text)
        if ident not in found or rank<found[ident][0]:
            found[ident]=(rank,text)
    return {k:v for k,(_,v) in found.items()},aliases


def covers(cmap,code):
    """Whether a Unicode subtable (format 4 or 12) of the cmap has a segment holding `code` (a segment may still map a
    code to glyph 0; good enough to tell a Cyrillic font from a Latin one)."""
    if len(cmap)<4:return False
    for i in range(struct.unpack_from('>H',cmap,2)[0]):
        platform,_,offset=struct.unpack_from('>HHI',cmap,4+8*i)
        if platform not in (0,3):continue
        form=struct.unpack_from('>H',cmap,offset)[0]
        if form==4:
            segments=struct.unpack_from('>H',cmap,offset+6)[0]//2
            ends=struct.unpack_from(f'>{segments}H',cmap,offset+14)
            starts=struct.unpack_from(f'>{segments}H',cmap,offset+16+2*segments)
            if any(s<=code<=e for s,e in zip(starts,ends)):return True
        elif form==12:
            for g in range(struct.unpack_from('>I',cmap,offset+12)[0]):
                s,e,_=struct.unpack_from('>III',cmap,offset+16+12*g)
                if s<=code<=e:return True
    return False


def char_map(cmap):
    """{code point: glyph id} of the Unicode subtables (format 4 and 12) of a cmap."""
    out={}
    if len(cmap)<4:return out
    for i in range(struct.unpack_from('>H',cmap,2)[0]):
        platform,encoding,offset=struct.unpack_from('>HHI',cmap,4+8*i)
        if platform not in (0,3) or (platform==3 and encoding not in (1,10)):continue
        form=struct.unpack_from('>H',cmap,offset)[0]
        if form==4:
            n=struct.unpack_from('>H',cmap,offset+6)[0]//2
            ends=struct.unpack_from(f'>{n}H',cmap,offset+14);starts=struct.unpack_from(f'>{n}H',cmap,offset+16+2*n)
            deltas=struct.unpack_from(f'>{n}h',cmap,offset+16+4*n);at=offset+16+6*n
            ranges=struct.unpack_from(f'>{n}H',cmap,at)
            for k in range(n):
                for code in range(starts[k],min(ends[k],0xFFFE)+1):
                    if ranges[k]==0:
                        glyph=(code+deltas[k])&0xFFFF
                    else:
                        glyph=struct.unpack_from('>H',cmap,at+2*k+ranges[k]+2*(code-starts[k]))[0]
                        glyph=(glyph+deltas[k])&0xFFFF if glyph else 0
                    if glyph:out.setdefault(code,glyph)
        elif form==12:
            for g in range(struct.unpack_from('>I',cmap,offset+12)[0]):
                start,end,glyph=struct.unpack_from('>III',cmap,offset+16+12*g)
                for code in range(start,end+1):out.setdefault(code,glyph+code-start)
    return out


def describe(data):
    """Faces of a TrueType or OpenType file (a collection gives one per face); [] when it is not a font."""
    if data[:4]==b'ttcf':
        offsets=struct.unpack_from(f'>{struct.unpack_from(">I",data,8)[0]}I',data,12)
    elif data[:4] in (b'\0\1\0\0',b'OTTO',b'true'):
        offsets=(0,)
    else:
        return []
    faces=[]
    for index,offset in enumerate(offsets):
        t=_tables(data,offset)
        if 'name' not in t or 'OS/2' not in t:continue
        names,aliases=_names(t['name']);os2=t['OS/2']
        axes={}
        if 'fvar' in t:
            f=t['fvar'];_,_,start,_,count,size=struct.unpack_from('>6H',f)
            for a in range(count):
                tag,low,default,high=struct.unpack_from('>4siii',f,start+a*size)
                axes[tag.decode('latin-1')]=[low/65536,default/65536,high/65536]
        faces.append({'index':index,'family':names.get(1),'subfamily':names.get(2),'typographic_family':names.get(16),
            'typographic_subfamily':names.get(17),'full_name':names.get(4),'aliases':sorted(aliases),'version':names.get(5),
            'weight':struct.unpack_from('>H',os2,4)[0],'italic':bool(struct.unpack_from('>H',os2,62)[0]&1),
            'fs_type':struct.unpack_from('>H',os2,8)[0],'axes':axes,'cyrillic':covers(t.get('cmap',b''),0x0416)})
    return faces


def license_of(path,directory):
    """Licence of a font file: the licence text next to it and the licence of its google/fonts folder."""
    rel=path.relative_to(directory).as_posix()
    spdx={'ofl':'OFL-1.1','apache':'Apache-2.0','ufl':'UFL-1.0'}.get(rel.split('/')[1] if rel.startswith('google/') and rel.count('/')>1 else '')
    text=next((p for p in sorted(path.parent.iterdir()) if p.suffix.lower()=='.txt' and re.search(r'ofl|licen[cs]e|eula',p.name,re.I)),None)
    return {'spdx':spdx,'file':text.relative_to(directory).as_posix() if text else None}


def build_index(directory):
    """index.json of a font directory: every face of every font file under it, with the SHA-256 of its file."""
    directory=Path(directory);faces,skipped=[],[]
    for path in sorted(p for p in directory.rglob('*') if p.suffix.lower() in FONT_SUFFIXES and p.is_file()):
        data=path.read_bytes();rel=path.relative_to(directory).as_posix()
        try:
            found=describe(data)
        except (struct.error,IndexError,ValueError) as exc:
            skipped.append({'file':rel,'reason':type(exc).__name__});continue
        if not found:
            skipped.append({'file':rel,'reason':'not-a-font'});continue
        digest=hashlib.sha256(data).hexdigest()
        for face in found:
            faces.append({'file':rel,'sha256':digest,'bytes':len(data),'license':license_of(path,directory),**face})
    index={'schema':'vsp.font-index/1','faces':faces,'skipped':skipped}
    (directory/'index.json').write_bytes((json.dumps(index,ensure_ascii=False,indent=1)+'\n').encode('utf-8'))
    return index


_CACHE={}
def load_catalog(dirs=None):
    """Faces of the index.json of every font directory, each with its directory; a directory without an index is skipped."""
    faces=[]
    for d in dirs if dirs is not None else font_dirs():
        index=Path(d)/'index.json'
        if not index.is_file():continue
        stamp=(str(index),index.stat().st_mtime_ns)
        if stamp not in _CACHE:
            _CACHE[stamp]=[{**f,'dir':str(Path(d))} for f in json.loads(index.read_bytes().decode('utf-8'))['faces']]
        faces+=_CACHE[stamp]
    return faces


def lock():
    return json.loads(LOCK.read_bytes().decode('utf-8'))


def split_weight(name):
    """("Open Sans", 300) for "Open Sans Light"; (name, None) when the last word is not a weight."""
    words=(name or '').split()
    # Two words first: "Semi Bold" is 600, not a "Semi" family at 700.
    if len(words)>2 and key(words[-2]+words[-1]) in WEIGHTS:
        return ' '.join(words[:-2]),WEIGHTS[key(words[-2]+words[-1])]
    if len(words)>1 and key(words[-1]) in WEIGHTS:
        return ' '.join(words[:-1]),WEIGHTS[key(words[-1])]
    return name,None


def _pick(faces,target):
    upright=[f for f in faces if not f['italic']] or faces
    return min(upright,key=lambda f:(abs(f['weight']-target) if not f['axes'].get('wght') else 0 if f['axes']['wght'][0]<=target<=f['axes']['wght'][2] else 1000))


def match(catalog,family):
    """Faces of the catalog for a typeface of the template, as {'regular': face, 'bold': face}, with how it matched:
    'name' (a family or full name of the face), 'weight' (the family without its weight word, at that weight), or {}."""
    k=key(family)
    same=[f for f in catalog if k in {key(a) for a in f['aliases']}]
    if same:
        family_faces=[f for f in same if key(f['family'])==k or key(f.get('typographic_family'))==k]
        if not family_faces:
            return {'regular':same[0]},'name',None
        regular=_pick(family_faces,400)
        bold=_pick(family_faces,700)
        # One variable face serves both weights through its weight range.
        return ({'regular':regular,'bold':bold} if bold is not regular else {'regular':regular}),'name',None
    base,weight=split_weight(family)
    if weight is None:return {},None,None
    b=key(base)
    faces=[f for f in catalog if key(f['family'])==b or key(f.get('typographic_family'))==b]
    if not faces:return {},None,None
    face=_pick(faces,weight)
    if not face['axes'].get('wght') and abs(face['weight']-weight)>50:return {},None,None
    return {'regular':face},'weight',weight


def entry(face,family,weight,origin,fixed=None):
    """A face of style['fonts'] drawn under the name of the template; a variable face carries its weight range (or the
    fixed weight of a family written with its weight). No 'eot': the PPTX never embeds a catalog face."""
    data=(Path(face['dir'])/face['file']).read_bytes()
    if hashlib.sha256(data).hexdigest()!=face['sha256']:
        raise ValueError('Font file differs from its index: '+face['file'])
    item={'family':family,'weight':weight,'sha256':face['sha256'],'data':base64.b64encode(data).decode(),
        'source_part':'fonts/'+face['file'],'origin':origin,'served_by':face['family'],'version':face.get('version'),
        'license':(face.get('license') or {}).get('spdx')}
    wght=face['axes'].get('wght')
    if fixed is not None:
        item['weight_range']=str(fixed)
    elif wght:
        item['weight_range']=f"{round(wght[0])} {round(wght[2])}"
    return item


def eot_header(raw):
    """Facts of an embedded font part (EOT): compressed (MTX), subset, embedding permission (fsType)."""
    if len(raw)<36:return {'eot':False}
    size,_,_,flags=struct.unpack_from('<IIII',raw)
    fs_type=struct.unpack_from('<H',raw,32)[0]
    return {'eot':size==len(raw),'compressed':bool(flags&4),'subset':bool(flags&1),'fs_type':fs_type}


def template_fonts(data):
    """Every typeface the template names, by script (latin, ea, cs, sym) and kind of part, with theme references
    resolved; and every embedded face with the facts of its part."""
    z=zipfile.ZipFile(__import__('io').BytesIO(data))
    names=set(z.namelist())
    themes={}
    for part in sorted(n for n in names if re.fullmatch(r'ppt/theme/theme\d+\.xml',n)):
        root=ET.fromstring(z.read(part))
        for role,short in (('majorFont','mj'),('minorFont','mn')):
            for script,tag in (('latin','lt'),('ea','ea'),('cs','cs')):
                node=root.find(f'{A}themeElements/{A}fontScheme/{A}{role}/{A}{script}')
                if node is not None and node.get('typeface'):
                    themes.setdefault(part,{})[f'+{short}-{tag}']=node.get('typeface')
    first_theme=next(iter(themes.values()),{})
    families={}
    def note(name,script,where,count=1):
        if not name:return
        f=families.setdefault(name,{'family':name,'scripts':set(),'parts':{}})
        f['scripts'].add(script);f['parts'][where]=f['parts'].get(where,0)+count
    for part,values in themes.items():
        for ref,name in values.items():
            note(name,{'lt':'latin','ea':'ea','cs':'cs'}[ref[-2:]],'theme')
    kinds=(('ppt/slides/','slide'),('ppt/slideLayouts/','layout'),('ppt/slideMasters/','master'),('ppt/notesMasters/','notes'))
    for part in sorted(names):
        kind=next((k for prefix,k in kinds if part.startswith(prefix) and part.endswith('.xml')),None)
        if kind is None:continue
        for node in ET.fromstring(z.read(part)).iter():
            tag=node.tag.rsplit('}',1)[-1]
            if tag in ('latin','ea','cs','sym') and node.tag.startswith(A) and node.get('typeface'):
                name=node.get('typeface')
                if name.startswith('+'):name=first_theme.get(name)
                note(name,tag,kind)
    embedded=[]
    presentation=ET.fromstring(z.read('ppt/presentation.xml'))
    rels={}
    if 'ppt/_rels/presentation.xml.rels' in names:
        for rel in ET.fromstring(z.read('ppt/_rels/presentation.xml.rels')):
            rels[rel.get('Id')]='ppt/'+rel.get('Target').lstrip('/').removeprefix('ppt/')
    for font in presentation.iter(P+'embeddedFont'):
        face=font.find(P+'font');family=face.get('typeface') if face is not None else None
        for weight in ('regular','bold','italic','boldItalic'):
            node=font.find(P+weight)
            part=rels.get(node.get(R+'id')) if node is not None else None
            if part in names:
                raw=z.read(part)
                embedded.append({'family':family,'weight':weight,'part':part,'raw':raw,**eot_header(raw)})
    return [{**f,'scripts':sorted(f['scripts'])} for f in families.values()],embedded


def embedded_reason(face):
    if not face.get('eot'):return 'not-eot'
    if face['fs_type']&0x0206:return 'restricted-fs-type'
    return 'not-decoded'


def extract_embedded(style,embedded,name):
    """Regular and bold faces the template embeds for a typeface other than its main one (the importer extracts only
    the main one), through the same decoder and rules; for the drawing only, like the fonts of the server."""
    from .fonts import decode_eot
    added=[]
    for e in embedded:
        if e['family']!=name or e['weight'] not in ('regular','bold'):continue
        sfnt=decode_eot(e['raw'],name,e['weight'])
        if sfnt:
            style['server_fonts'].append({'family':name,'weight':e['weight'],'source_part':e['part'],'sha256':hashlib.sha256(sfnt).hexdigest(),
                'data':base64.b64encode(sfnt).decode(),'origin':'reference','subset':bool(e.get('subset'))})
            added.append(e['weight'])
    return added


def catalog_fonts(style,template,catalog=None):
    """The inventory of the template (style['font_inventory']) and, for every latin typeface of the template that neither
    the importer, the pinned assets nor the content package serve: the faces the template embeds for it; else a face of
    the catalog under its name, the same family when the catalog has it, else the metric-compatible substitute of the
    lock. Only these count as loaded in the composer: a face with other metrics would break the fit the browser measures."""
    catalog=load_catalog() if catalog is None else catalog
    try:
        substitutes={key(k):v for k,v in lock()['substitutes'].items()}
    except FileNotFoundError:
        substitutes={}
    families,embedded=template_fonts(template)
    style['server_fonts']=[]
    chars=style.get('font_candidates') or {}
    served={}
    for f in style['fonts']:
        served.setdefault(f['family'],f.get('origin') or 'reference')
    for item in families:
        name=item['family'];item['chars']=chars.get(name,0)
        faces=[e for e in embedded if e['family']==name]
        if faces:
            item['embedded']=[{k:e[k] for k in ('weight','compressed','subset','fs_type') if k in e} for e in faces]
        if name not in served and faces and 'latin' in item['scripts'] and extract_embedded(style,embedded,name):
            served[name]='embedded-drawing'
        if name in served:
            item['source']={'reference':'embedded','embedded-drawing':'embedded','licensed-asset':'pinned-asset'}.get(served[name],served[name])
            continue
        if faces:
            item['embedded_not_used']=sorted({embedded_reason(e) for e in faces})
        if 'latin' not in item['scripts']:
            item['source']='not-checked'
            continue
        # A face named only by the notes pages draws nothing of the slides (template B, template C: Calibri).
        if set(item['parts'])<={'notes'}:
            item['source']='notes-only'
            continue
        found,how,weight=match(catalog,name)
        origin='catalog'
        if not found and key(name) in substitutes:
            found,how,weight=match(catalog,substitutes[key(name)])
            origin='metric-substitute'
            # The text of the service is Russian: a substitute without Cyrillic (Caladea for Cambria, Gelasio for
            # Georgia) would draw the Cyrillic in the next font of the stack under the name of the template's font.
            if found and not all(f.get('cyrillic') for f in found.values()):
                item['substitute_without_cyrillic']=sorted({f['family'] for f in found.values()})
                found={}
        if not found:
            item['source']='unavailable'
            continue
        for slot,face in found.items():
            style['server_fonts'].append(entry(face,name,slot,origin,weight))
        item['source']=origin
        item['served_by']=sorted({f['family'] for f in found.values()})
        item['files']=sorted({f['file'] for f in found.values()})
    # A subset font holds only the glyphs of the original presentation (subset_gaps names what the new text lacks).
    subset_parts={e['part'] for e in embedded if e.get('subset')}
    for f in style['fonts']:
        if f.get('source_part') in subset_parts:
            f['subset']=True
    for e in embedded:
        e.pop('raw',None)
    style['font_inventory']={'schema':'vsp.font-inventory/1','families':families,'embedded':embedded,
        'catalog_faces':len(catalog),'catalog_dirs':[str(d) for d in font_dirs()]}
    by={}
    for f in families:
        by.setdefault(f.get('source'),[]).append(f)
    if style.get('font') in {f['family'] for f in by.get('catalog',[])+by.get('metric-substitute',[])+by.get('embedded',[])}:
        style['diagnostics']=[d for d in style['diagnostics'] if d.get('code')!='FONT_AVAILABILITY']
    if by.get('catalog'):
        style['diagnostics'].append({'level':'warning','code':'CATALOG_FONT','families':{f['family']:f['served_by'][0] for f in by['catalog']},
            'message':'Шрифт образца не встроен в него; использована та же гарнитура из открытого набора шрифтов сервера. Совпадение версии со шрифтом автора не подтверждено.'})
    if by.get('metric-substitute'):
        style['diagnostics'].append({'level':'warning','code':'METRIC_SUBSTITUTE_FONT','families':{f['family']:f['served_by'][0] for f in by['metric-substitute']},
            'message':'Шрифт образца не встроен и не найден на сервере: предпросмотр, HTML, PDF и замер вместимости используют открытую метрически совместимую замену (те же ширины знаков). В PPTX остаётся шрифт образца.'})
    if by.get('unavailable'):
        style['diagnostics'].append({'level':'warning','code':'FONT_NOT_ON_SERVER','families':[f['family'] for f in by['unavailable']],
            'message':'Эти шрифты образца не встроены в него (или встроенный файл не удалось прочитать) и не найдены среди открытых шрифтов сервера: где они нужны, браузер рисует подстановку. В PPTX остаётся шрифт образца.'})
    return style


def serve_fonts(documents,style):
    """The fonts of the server for the drawing of every document, after the composer has chosen its typefaces."""
    for doc in documents:
        have={(f['family'],f['weight']) for f in doc['style']['fonts']}
        doc['style']['fonts']+=[f for f in style.get('server_fonts') or [] if (f['family'],f['weight']) not in have]
    return documents


def subset_gaps(style,documents):
    """Characters of the new text that a subset font of the template lacks: it holds only the glyphs of the original
    presentation (EOT flag 1), so the browser draws them in another font; they are named in SUBSET_FONT_MISSING_GLYPHS."""
    subset={}
    for f in style['fonts']+(style.get('server_fonts') or []):
        if f.get('subset'):subset.setdefault(f['family'],f)
    if not subset:return {}
    need={}
    for doc in documents:
        for slide in doc['slides']:
            for e in slide['elements']:
                if e.get('font') not in subset:continue
                text=''.join(str(c) for row in e.get('rows') or [] for c in row) if e.get('type')=='table' else e.get('text') or ''
                need.setdefault(e['font'],set()).update(c for c in text if not c.isspace())
    gaps={}
    for family,chars in need.items():
        mapped=char_map(_tables(base64.b64decode(subset[family]['data'])).get('cmap',b''))
        missing=''.join(sorted(c for c in chars if ord(c) not in mapped))
        if missing:gaps[family]=missing
    if gaps:
        style['diagnostics'].append({'level':'warning','code':'SUBSET_FONT_MISSING_GLYPHS','families':gaps,
            'message':'Шрифт, встроенный в образец, урезан до знаков исходной презентации: этих знаков нового текста в нём нет, их рисует другой шрифт. На машине без полного шрифта PowerPoint поступит так же.'})
    return gaps
