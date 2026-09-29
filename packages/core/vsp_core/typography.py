"""Shared text inheritance: placeholder -> layout -> master -> master text styles -> presentation -> theme.

Moved out of patterns.frame()/read_patterns (21.09.2026) without behaviour change,
so that the reference inventory resolves typography by the same rules.
"""
import io
import re
import zipfile

from .importer import NS


SERVICE_PLACEHOLDERS=('dt','ftr','sldNum','hdr')


def style_role(ph):
    """Master text style of a placeholder key (type, idx); no placeholder means free text.

    The date, footer, slide number and header placeholders take otherStyle, as PowerPoint sets them (owner decision 46,
    28.09.2026: with bodyStyle the slide number of deck 28 took its marker "•", an indent of 24 px and a space of 10 pt
    before, and "10" broke into two lines in the preview, HTML and PDF, while PowerPoint 2013 set "10" in one line with no
    marker; analysis/style-experiments/20260928-pdf-fix)."""
    if not ph:return 'otherStyle'
    return 'titleStyle' if ph[0] in ('title','ctrTitle') else 'otherStyle' if ph[0] in SERVICE_PLACEHOLDERS else 'bodyStyle'


def run_candidates(chain, master, presentation, ph):
    """Run property nodes in inheritance order, and the first paragraph properties of each chain node."""
    props=[];paras=[]
    for n in chain:
        if n is None: continue
        props.extend(n.findall('p:txBody/a:p/a:r/a:rPr',NS)[:1])
        props.extend(n.findall('p:txBody/a:p/a:pPr/a:defRPr',NS)[:1])
        props.extend(n.findall('p:txBody/a:lstStyle/a:lvl1pPr/a:defRPr',NS)[:1])
        paras.extend(n.findall('p:txBody/a:p/a:pPr',NS)[:1])
    role=style_role(ph)
    if master is not None: props.extend(master.findall(f'p:txStyles/p:{role}/a:lvl1pPr/a:defRPr',NS))
    if presentation is not None: props.extend(presentation.findall('p:defaultTextStyle/a:lvl1pPr/a:defRPr',NS))
    return props,paras


def color_context(theme, master, owners):
    """Theme palette and the scheme colour map: master clrMap, then overrideClrMapping of each owner in order."""
    from .patterns import rgb
    palette={};mapping={'tx1':'dk1','tx2':'dk2','bg1':'lt1','bg2':'lt2'}
    if theme is not None:
        for entry in theme.findall('a:themeElements/a:clrScheme/*',NS):
            if len(entry): palette[entry.tag.rsplit('}',1)[-1]]=rgb(entry[0],{})
    if master is not None:
        cmap=master.find('p:clrMap',NS)
        if cmap is not None: mapping.update(cmap.attrib)
    for owner in owners:
        if owner is not None:
            cmap=owner.find('p:clrMapOvr/a:overrideClrMapping',NS)
            if cmap is not None: mapping.update(cmap.attrib)
    return palette,mapping


def theme_fonts(theme, fallback):
    """Major and minor latin typefaces of the theme; empty without a theme."""
    fonts={}
    if theme is not None:
        for kind in ('major','minor'):
            n=theme.find(f'a:themeElements/a:fontScheme/a:{kind}Font/a:latin',NS)
            fonts[kind]=n.get('typeface') if n is not None else fallback
    return fonts


EM_DASH, EN_DASH = '—', '–'


def template_dash(data):
    """The dash of the sample's own text: the em dash when the texts of its slides, layouts and masters use it more often than
    the en dash, else the en dash. Owner decision 44 (28.09.2026): «в презентациях, если это не задано по другому в шаблоне,
    который мы повторяем, используй среднее тире, а не длинное»."""
    em=en=0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if re.match(r'ppt/(slides|slideLayouts|slideMasters)/[^/]+\.xml$',name):
                text=z.read(name).decode('utf-8','replace')
                em+=text.count(EM_DASH)+text.count('&#8212;');en+=text.count(EN_DASH)+text.count('&#8211;')
    return EM_DASH if em>en else EN_DASH


def with_dash(value, dash, skip=('quote',)):
    """value with every em dash of its strings set to `dash` (decision 44); keys in `skip` keep their text: the quotes of the
    brief are the evidence the checks compare with the brief itself."""
    if dash==EM_DASH:return value
    if isinstance(value,str):return value.replace(EM_DASH,dash)
    if isinstance(value,list):return [with_dash(v,dash,skip) for v in value]
    if isinstance(value,dict):return {k:(v if k in skip else with_dash(v,dash,skip)) for k,v in value.items()}
    return value
