"""Select a reference composition, then populate new native objects with facts.

In the carrier export mode (A5b, docs/CARRIER_PRODUCT_2026-09-21.md) titles go into the
layout placeholders they came from, and text sections of the sequence and columns
variants may be laid out in the body placeholders of a suitable reference layout.
"""
import copy
import math
import re
from collections import Counter
from itertools import combinations, product
from .patterns import contrast, contains, overlap, blend
from .placement import estimate_lines, body_box, reflow_grid, partition_region, grow_region
from .diversity import composition_signature, audit_diversity
from .style_compatibility import decoration_signature
from .reference import classify_layout, resolve_scheme, slide_tone, TITLE_TYPES
from .importer import FACE_ROLES
from .sample_clone import plan_cards, card_elements, readable_cards
from .icons import card_icons, places_icons
from .audit import template_tokens
from . import item_marks

EMU=9525
ALIGN={'l':'left','ctr':'center','r':'right'}
PLACEHOLDER_KINDS={'sequence':'content_1','columns':'content_2'}
# C4a (carrier mode only): instead of refusing, a slide falls back to the logic of
# spikes/template_carrier/carrier.py on the reference's own layouts. Variant -> card arrangement of the spike.
FALLBACK_ARRANGEMENTS={'sequence':'single','columns':'columns','split':'cards'}
FALLBACK_BODY_KINDS={'sequence':('content_1','content_2'),'columns':('content_2',),'split':()}
DIVERSITY_REASON='Комплект не выдан: не удалось получить три различающиеся композиции'
PREVIEW_INCOMPLETE='В PPTX оформление полнее, чем в предпросмотре'
# A6b: largest share of the slide an undrawable decor frame may cover to become an invisible obstacle (ghost).
# A larger frame is a background, not an obstacle: in deck 09 the whole background with the shield is one EMF
# of 1280x720, and an obstacle the size of the canvas forbids any placement and flags every text.
# Such a slide keeps only the warning PREVIEW_INCOMPLETE_DECOR.
GHOST_MAX_AREA=.5
# Item 37, cause 9 (holdout-next 25.09.2026): capitals are about this much wider than the mixed text placement.estimate_lines
# counts (Arial: capitals about .67 of the size, a sign of the estimate .56).
CAPS_WIDTH=1.2
# C2 at the source, v3: clear space between the footer and a logo or undrawable decor frame: the 2 px of the render check
# (quality.js, BRAND_ASSET_OVERLAP) plus 1 px for the rounding of glyph boxes (deck 22: 1.79 px below the band of the roundel).
FOOTER_CLEAR=3
# A7 v2, B4 (docs/VARIANT_AXIS.md): arrangements of sample cards each variant prefers, best first — columns: facts side by
# side; sequence: one under another across the width; split: a column of cards in a zone beside the title. Variants pick
# in CARD_ORDER; a composition another variant already has for the section is taken only when no distinct one is left,
# so a section stays on cards in every variant whenever some sample holds its facts.
CARD_PREFERENCE={'columns':('row','grid','stack','column'),'sequence':('stack','column','grid','row'),'split':('column','grid','stack','row')}
CARD_ORDER=('split','sequence','columns')
# E1 (proposals of 24.09.2026): a template with body layouts keeps its list and columns on the layout placeholders; its
# sample cards serve the variant where every fact is a block of its own. Before, any body layout switched the cards of the
# whole deck off (template C: the blocks variant stood on half-empty native boxes with small text).
CARDS_BESIDE_PLACEHOLDERS=('split',)
# Largest share of the slide height between the title and the first text of the cards before a card slide counts as
# half empty.
EMPTY_BAND=.25
# Owner remark of 28.09.2026 («слайд 25», not only template A): in 47 of 54 issued variants (QS4, K3-5) one sample or layout stood
# on at least half of the content slides, in 30 on at least 80 % (template A split: sample 25 on 11 of 11). Every earlier pick of a
# composition in the same variant adds REPEAT_PENALTY to its score (select), so near-equal compositions alternate while a
# worse one (a layout of its own, missing decor, photo places: +6 and more) still does not come in; a sample of cards that
# already took CARD_REPEAT_SHARE of the text sections of a variant (at least 2) goes behind the other samples that hold the
# section, before the preferred arrangement of the variant decides.
REPEAT_PENALTY=1.0
PEER_BODY=.85
CARD_REPEAT_SHARE=.34
# Owner decision 49 (29.09.2026), limitation 1: a title of a sample slide that its frame holds only below this share of its own
# size (titles.TITLE_SHRUNK) takes a wider frame along its band (widen_sample_title); the estimate counts a sign as .56 of the
# size, bold wide faces take more (as hug_sample_plate, 1.2).
TITLE_WIDEN_BELOW=.85
TITLE_WIDEN_SAFE=1.2
# Owner request of 29.09.2026, late evening, on the three variants of the template A: the list variant «вообще в слайдах
# (кроме обложки) не использует элементы стиля презентации»; it is issued third now (pipeline.VARIANT_ORDER) and «на каждый слайд
# типовые решения ОТЛИЧНЫЕ от пустой страницы … выкручиваем показатель страниц с изображениями на максимум». After the other two
# variants chose, every text section of the list variant takes a composition that shows a picture of the template when one holds
# it: a card set whose clones carry a picture (template A 15: the discs, 18: the cube) or a sample slide with its cut-out illustration
# (patterns.py: template A 7, 8: the spring; 22: a 3D block), the least used first. A picture is an image of PICTURE_AREA of the slide
# (no pictogram, no background); a composition without one scores PICTURELESS more in that choice.
PICTURE_AREA=(.03,.9)
PICTURELESS=12
# The preview embeds a picture on every slide that shows it: template C 35-36 carry a PNG of 9.8 and 6.9 MB, on 10 slides of
# the list variant the HTML grew past 100 MB and Chromium closed the page of the check of the rendering (rebuild set1 of
# 29.09.2026). A picture heavier than HEAVY_PICTURE bytes (base64) draws the choice only while the pictures of that kind the
# variant took stay within HEAVY_BUDGET; the server has 1.2 GB for the whole run.
HEAVY_PICTURE=2_000_000
HEAVY_BUDGET=20_000_000


def preview_note(media, **extra):
    return {'code':'PREVIEW_INCOMPLETE_DECOR','media':sorted(set(media)),'message':PREVIEW_INCOMPLETE,**extra}


# Session 16 (27.09.2026): share of the slide the empty places of photographs of a composition may take before it comes after
# the others (build_reference_documents, empty_photo).
EMPTY_PHOTO = .15


def decor_missing(brand, layout_part):
    """Item 31 (25.09.2026): how much of the mandatory decor of the sample slides (reference.brand_decor) a content slide on
    this layout would lack: items a layout or master draws that this one does not, and items only copies of slide shapes
    carry that its text or title frames would cover (deck 25: the band of the sample layout; the other layouts draw no band).
    0 for a template without such decor, so its choices stay as before."""
    if not brand or not brand.get('items'):return 0
    tone=brand.get('layout_tones',{}).get(layout_part,'light')
    items=[i for i in brand['items'] if tone in i.get('tones',[tone])]
    drawn,blocked=set(brand['layouts'].get(layout_part,[])),set(brand.get('blocked',{}).get(layout_part,[]))
    return sum(1 for i in items if not i['transferable'] and i['id'] not in drawn)+sum(1 for i in items if i['id'] in blocked)


def layout_sample_text(brand, layout_part):
    """Item 35, H3 (holdout 25.09.2026, deck 08): text of its sample slide that a layout draws itself in the content zone
    (reference.brand_decor, sample_texts: "LEVEL 3 WILL:" of the layouts of the level slides, the labels of the form layouts of
    deck 15); a derived slide on that layout would show it as its own. Empty for a template without such layouts, so its choices
    stay as before; a layout with such text is taken only when no other holds the slide."""
    return ((brand or {}).get('sample_texts') or {}).get(layout_part) or []


class CarrierLayouts:
    """Layout choice and colours of the carrier fallback, ported from Composer of spikes/template_carrier/carrier.py.

    Differences from the spike: the inventory is the JSON-compatible one of vsp_core.reference
    (free_text_colours as lists), colours are returned as RGB of the document model, and
    furniture of sample slides is not cloned onto fallback slides (it is no obstacle either).
    One instance per output document: `pick` rotates among equally good layouts.
    """
    def __init__(self, carrier):
        self.inv=carrier;self.width,self.height=carrier['width'],carrier['height']
        self.layouts=carrier['layouts']
        self.classes={l['part']:classify_layout(l,self.width,self.height) for l in self.layouts}
        used_masters=Counter(l['master'] for l in self.layouts for _ in range(l['used_by_slides']))
        self.main_master=used_masters.most_common(1)[0][0] if used_masters else self.layouts[0]['master']
        # Session 16 (26.09.2026): the layout of the first slide of the file, an instruction slide included. The cover is then
        # the composition of the slide after them (patterns.read_patterns); this layout stays the last resort of content
        # slides as before (deck 28: every layout is of the cover kind, and with the black cover layout here the slides of key
        # numbers went black, with a chart whose labels vanished).
        self.first_slide_layout=carrier.get('first_slide_layout') or (carrier['slides'][0]['layout'] if carrier['slides'] else None)
        self.card_style=(carrier['card_styles'] or [None])[0]
        self.observed={role:data['adopted'] for role,data in carrier['observed_styles'].items()}
        self.reserved_cover=None;self.rotation=Counter()
        self.brand=carrier.get('brand_decor') or {}
        self.used_masters={l['master'] for l in self.layouts if l['used_by_slides']}

    def foreign(self, layouts):
        """Holdout-next2 26.09.2026, deck 31: layouts only of masters no sample slide uses (the Office masters under the
        brand ones, with the splash picture as their background) while a layout of a used master can hold the slide (a title
        and a free zone or a body). Such layouts are the last resort, not the first."""
        return bool(layouts) and all(l['master'] not in self.used_masters for l in layouts) and any(
            l['master'] in self.used_masters and self.classes[l['part']]['title'] and self.classes[l['part']]['kind'] in ('title_only','content_1','content_2','content_n')
            for l in self.layouts)

    def ranked(self, *kinds):
        # Item 31: a layout that carries the mandatory decor of the sample slides comes before any that does not.
        order={l['part']:n for n,l in enumerate(self.layouts)}
        found=[l for l in self.layouts if self.classes[l['part']]['kind'] in kinds and l['part']!=self.reserved_cover]
        # Holdout-next2 (deck 31): a layout of a master the sample slides use comes before any of a master they do not.
        own=[l for l in found if l['master'] in self.used_masters]
        found=own or found
        most=max((l['used_by_slides'] for l in found),default=0)
        # Item 35, H3: a layout that draws text of its sample slide comes after every layout that does not.
        return sorted(found,key=lambda l:(bool(layout_sample_text(self.brand,l['part'])),decor_missing(self.brand,l['part']),kinds.index(self.classes[l['part']]['kind']),l['used_by_slides']==0,
                                          l['used_by_slides']*2<most,-self.classes[l['part']]['plausibility'],
                                          l['master']!=self.main_master,-l['used_by_slides'],order[l['part']]))

    def pick(self, purpose, *kinds, candidates=None):
        """Rotate among equally good candidates so a deck does not repeat one layout."""
        candidates=self.ranked(*kinds) if candidates is None else candidates
        if not candidates:return None
        most=max(l['used_by_slides'] for l in candidates)
        def level(layout):
            cls=self.classes[layout['part']]
            return (bool(layout_sample_text(self.brand,layout['part'])),decor_missing(self.brand,layout['part']),cls['kind'],layout['used_by_slides']==0,layout['used_by_slides']*2<most,cls['plausibility'],layout['master'])
        tier=[l for l in candidates if level(l)==level(candidates[0])]
        choice=tier[self.rotation[purpose]%len(tier)];self.rotation[purpose]+=1
        return choice

    def cover_layout(self):
        by_part={l['part']:l for l in self.layouts}
        first=by_part.get(self.first_slide_layout)
        # An opening layout the reference uses once or twice is its dedicated cover.
        if first is not None and self.classes[first['part']]['title'] and first['used_by_slides']<=2:
            self.reserved_cover=first['part'];return first
        covers=sorted((l for l in self.layouts if self.classes[l['part']]['kind']=='cover'),
                      key=lambda l:(l['used_by_slides']==0,l['master']!=self.main_master,-self.classes[l['part']]['plausibility'],-l['used_by_slides']))
        if covers:
            self.reserved_cover=covers[0]['part'];return covers[0]
        if first is not None and self.classes[first['part']]['title']:return first
        with_title=[l for l in self.layouts if self.classes[l['part']]['title']]
        return (with_title or self.layouts)[0]

    def theme(self, layout):
        return self.inv['masters'][layout['master']]['theme']

    def content_title(self, layout):
        """Title placeholder as the reference really places it on content slides, if that box is on the canvas."""
        title=self.classes[layout['part']]['title'];box=self.observed.get('title',{}).get('box')
        if title and box and contains([0,0,self.width,self.height],list(box),0):return {**title,'box':list(box)}
        return title

    def zone(self, layout):
        """Free content area of a layout that has a title but no usable body placeholder."""
        w,h=self.width,self.height;title=self.content_title(layout)
        left=min(max(title['box'][0] if title else w*.06,w*.03),w*.12)
        top=(title['box'][1]+title['box'][3]+h*.03) if title else h*.12
        right,bottom=w-left,h*.90
        if title and title['box'][2]<w*.45 and title['box'][0]<w*.3 and title['box'][3]>h*.3:
            left,top=title['box'][0]+title['box'][2]+w*.03,max(title['box'][1],h*.1)
        for x,y,bw,bh in layout['decor']:
            if bw*bh<w*h*.15 and h*.72<y<bottom and x<right and x+bw>left:bottom=min(bottom,y-h*.02)
        samples=[b for b in layout['sample_content_boxes'] if b[1]>=top-h*.05 and b[1]+b[3]<=h*.95]
        if samples:
            left=max(left,min(b[0] for b in samples));right=min(right,max(b[0]+b[2] for b in samples))
        if right-left<w*.4 or bottom-top<h*.3:
            left,right,top,bottom=w*.06,w*.94,max(top,h*.2) if bottom-top>=h*.3 else h*.25,h*.88
        return [left,top,right-left,bottom-top]

    def free_bottom_span(self, layout):
        w,h=self.width,self.height;y0,y1=h*.935,h*.985
        blocked=sorted((x,x+bw) for x,y,bw,bh in layout['decor'] if y<y1 and y+bh>y0 and bw<w*.9)
        spans,cursor=[],w*.04
        for start,end in blocked:
            if start-cursor>0:spans.append((cursor,start-w*.01))
            cursor=max(cursor,end+w*.01)
        spans.append((cursor,w*.96))
        start,end=max(spans,key=lambda s:s[1]-s[0])
        return [start,y0,max(end-start,1),y1-y0]

    def rgb_of(self, layout, colour):
        kind,value,transforms=colour
        if any(name!='alpha' for name,_ in transforms):return None
        return value if kind=='srgbClr' else resolve_scheme(self.theme(layout),value)

    def background_rgb(self, layout):
        return layout['background_rgb'] or resolve_scheme(self.theme(layout),'bg1') or 'FFFFFF'

    def text_colour(self, layout, surface_rgb=None):
        """RGB for native text: what the reference uses on this layout, if it is readable here."""
        surface=surface_rgb or self.background_rgb(layout)
        colours=self.inv['free_text_colours'];deck=Counter()
        for items in colours.values():
            for item in items:deck[(item['color'][0],item['color'][1],tuple(map(tuple,item['color'][2])))]+=item['chars']
        here=colours.get(layout['part'],[])
        observed=([(here[0]['color'][0],here[0]['color'][1],tuple(map(tuple,here[0]['color'][2])))] if here else [])+([deck.most_common(1)[0][0]] if deck else [])
        scheme=[('schemeClr','tx1',()),('schemeClr','bg1',())]
        for c in observed+scheme:
            value=self.rgb_of(layout,c)
            if value and contrast(value,surface)>=4.5:return value
        return max((self.rgb_of(layout,c) or '000000' for c in scheme),key=lambda v:contrast(v,surface))

    def brand_fill(self, layout):
        """Fill for emphasis (table header): the reference's own title band colour, else the theme accent."""
        for colour in (self.observed.get('title',{}).get('shape_fill'),('schemeClr','accent1',())):
            value=self.rgb_of(layout,(colour[0],colour[1],tuple(map(tuple,colour[2])))) if colour else None
            if value:return value
        return '0077FF'

    def card_look(self, layout):
        """Card fill RGB, its opacity, geometry and the readable text RGB on it."""
        background=self.background_rgb(layout)
        style=self.card_style or {'prst':'roundRect','color_kind':'schemeClr','color':'tx1','transforms':[('alpha','7000')]}
        transforms=tuple(map(tuple,style['transforms']))
        fill=self.rgb_of(layout,(style['color_kind'],style['color'],transforms)) or background
        alpha=next((int(v)/100000 for n,v in transforms if n=='alpha'),1.0)
        return {'fill':fill,'opacity':alpha,'geometry':style['prst'] if style['prst'] in ('rect','roundRect') else 'rect',
                'text':self.text_colour(layout,blend(fill,background,alpha)),'source':{k:copy.deepcopy(style.get(k)) for k in ('prst','color_kind','color','transforms')}}


def text_size(value, box, preferred, line_height=1.18):
    for size in range(round(min(96,preferred)*2),23,-1):
        size/=2
        # Deliberately conservative estimate; the browser remains the fit oracle.
        lines=estimate_lines(value,box[2],size)
        if lines*size*line_height<=box[3]:return size
    return min(12,preferred)


def placeholder_metrics(t, size):
    """Line height (multiple of the size) and paragraph gap (px) of placeholder typography t at a font size in px.

    Spacing in points keeps the multiple it has at the own size of the typography, as for the clones and the decor of the
    samples (sample_clone._line_pitch): holdout-next2, deck 11 — a title of 30 pt with 30 pt spacing set at 25 px took 1.6 of
    its size, the check of the render kept that multiple at every size it tried, a second line never fitted the frame, and
    the title fell to 14 pt under text of 28 pt. The export writes the multiple where the points give another one."""
    spacing=t.get('line_spacing') or {'pct':100.0}
    line=1.2*spacing['pct']/100 if 'pct' in spacing else spacing['pt']/t['size_pt'] if t.get('size_pt') else spacing['pt']*4/3/size
    before=t.get('space_before') or {'pt':0.0}
    gap=before['pt']*4/3 if 'pt' in before else before['pct']/100*size
    return line,gap


def placeholder_bullet(t):
    """Marker of placeholder typography as a text property, or None."""
    indent,hanging=t.get('margin_left_emu',0)/EMU,-t.get('indent_emu',0)/EMU
    char=t.get('bullet_char') or ''
    if t.get('bullet')!='char' or not 1<=len(char)<=2 or indent<0 or hanging<0:return None
    return {'char':char,'indent':indent,'hanging':hanging}


def placeholder_room(box, t):
    """Width and height a placeholder frame leaves for text: minus insets and the marker indent."""
    l,top,r,b=[v/EMU for v in t.get('insets_emu') or [91440,45720,91440,45720]]
    bullet=placeholder_bullet(t)
    return box[2]-l-r-(bullet['indent'] if bullet else 0),box[3]-top-b


def placeholder_text_size(value, box, t, floor):
    """Largest size in px, not above the typography size, at which the paragraphs fit the frame minus insets.

    Same conservative estimate as text_size, plus the gap before every paragraph (the first only
    with bodyPr spcFirstLastPara, as PowerPoint draws a:spcBef) and the marker indent.
    None when even `floor` does not fit, or when the insets leave no room.
    """
    width,height=placeholder_room(box,t)
    if width<=0 or height<=0:return None
    # Item 37, cause 9: text PowerPoint sets in capitals takes more width than the estimate counts a sign.
    if t.get('caps')=='all':width/=CAPS_WIDTH
    paragraphs=value.split('\n');preferred=min(96,t['size_pt']*4/3)
    gaps=len(paragraphs)-(0 if t.get('space_first_last') else 1)
    def fits(size):
        line,gap=placeholder_metrics(t,size)
        return estimate_lines(value,width,size)*size*line+gap*gaps<=height
    if fits(preferred):return preferred
    for size in range(math.ceil(preferred*2)-1,math.ceil(floor*2)-1,-1):
        if fits(size/2):return size/2
    return None


def placeholder_face(style, kind, own, frame=None):
    """Typeface of text in a layout placeholder of type `kind` whose own face (layout -> master -> theme) is `own`.

    M7, cause 7 (holdout-next 25.09.2026): the face is the one the text of the sample slides of the same role is set in
    (style.sample_faces, runs; SSIH: titles in Cambria, the rest in Arial) when one face holds 60 % of those runs; the
    face of the placeholder when the sample slides have no text of that role, or when their faces are mixed and the face
    of the placeholder holds at least half the runs of the most common one (deck 18: Calibri Light 3, Arial 3, Calibri 2 —
    not WHO: Calibri Light 1 of 27 runs). Before, every placeholder took the dominant face of the reference unless its own
    face was embedded, and the exporter wrote that face into the runs over the one of the layout. Otherwise the old rule
    stays: the own face when embedded, then the dominant face.
    """
    loaded={f['family'] for f in style['fonts']}
    old=own if own in loaded else frame['font'] if frame and frame['font'] in loaded else style['font']
    if 'sample_faces' not in style or not own:return old
    faces=Counter(style['sample_faces'].get(FACE_ROLES.get(kind,kind)) or {})
    if not faces:return own
    face,count=faces.most_common(1)[0]
    return face if count>=.6*sum(faces.values()) else own if faces[own]>=count/2 else old


def placeholder_pair(layout, key):
    """Placeholder of a layout that a slide placeholder (type, idx) inherits from: titles by type, the rest by idx."""
    if not key:return None
    usable=[q for q in layout['placeholders'] if q['box'] and not q['rotated'] and not q['vertical']]
    if key[0] in TITLE_TYPES:
        return next((q for q in usable if q['type']==key[0]),None) or next((q for q in usable if q['type'] in TITLE_TYPES),None)
    return next((q for q in usable if q['idx']==key[1] and q['type'] not in TITLE_TYPES),None)


def placeholder_layouts(style, kind):
    """Reference layouts of one kind for text in body placeholders, best first.

    Order as Composer.ranked of spikes/template_carrier/carrier.py: layouts the reference
    uses first, then those used at least half as often as the most used one, then
    plausibility, the main master, frequency and document order. Candidates need
    plausibility >= 3 and a composition of the layout itself (its background and decor
    for the preview).
    """
    carrier=style['carrier'];layouts=carrier['layouts']
    classes={l['part']:classify_layout(l,carrier['width'],carrier['height']) for l in layouts}
    own={p['layout_part']:p for p in style['reference']['patterns'] if p.get('source_kind')=='layout'}
    used_masters=Counter(l['master'] for l in layouts for _ in range(l['used_by_slides']))
    main_master=used_masters.most_common(1)[0][0] if used_masters else layouts[0]['master']
    order={l['part']:n for n,l in enumerate(layouts)}
    found=[l for l in layouts if classes[l['part']]['kind']==kind]
    most=max((l['used_by_slides'] for l in found),default=0)
    # Item 31 (25.09.2026): a layout that carries the mandatory decor of the sample slides first (deck 25: the sample layout
    # with the band before the plain ones).
    brand=carrier.get('brand_decor') or {}
    # Item 35, H3: a layout that draws text of its sample slide comes last.
    found.sort(key=lambda l:(bool(layout_sample_text(brand,l['part'])),decor_missing(brand,l['part']),l['used_by_slides']==0,l['used_by_slides']*2<most,
                             -classes[l['part']]['plausibility'],l['master']!=main_master,-l['used_by_slides'],order[l['part']]))
    return [(l,classes[l['part']],own[l['part']]) for l in found if classes[l['part']]['plausibility']>=3 and l['part'] in own]


def build_reference_documents(brief,style,config):
    carrier=config.get('export_mode')=='carrier'
    ref=style['reference'];w,h=style['width'],style['height'];docs=[]
    # Item 31 (25.09.2026): the mandatory decor of the sample slides (reference.brand_decor) steers the choice of layouts and
    # compositions, and the items only slides hold come onto the content slides as copies of their shapes (carry_brand).
    brand=(style['carrier'].get('brand_decor') or {}) if carrier else {}
    # Session 16, B (holdout-next3, «Газпром»): when no composition free of text of its sample holds a section, H3 below falls
    # back to one on a layout that draws that text, and our titles stood over the titles of the sample on 8 of 11 slides. While
    # the template has a clean layout (a title placeholder, a content kind, no text of a sample, a master the sample slides
    # use: "Дополнительный слайд" of Gazprom), such a section is refused and goes to the clean layout by the fallback.
    clean_layout=bool(carrier) and any(
        cls['title'] and cls['kind'] in ('title_only','content_1','content_2','content_n') and not layout_sample_text(brand,l['part'])
        and l['master'] in {m['master'] for m in style['carrier']['layouts'] if m['used_by_slides']}
        for l in style['carrier']['layouts'] for cls in [classify_layout(l,style['carrier']['width'],style['carrier']['height'])])
    brand_items=[i for i in brand.get('items',[]) if i['transferable'] and i.get('preview')]
    def drawn_pages(p):
        """Item 35, H5: frames of the slide numbers the layout of a composition, or its master, draws itself."""
        return (brand.get('pages') or {}).get(p.get('layout_part'),[])
    def composition_missing(p,same_place=False):
        """decor_missing for a composition: one of a sample slide lacks the items of its tone that neither that slide nor its
        layout shows (deck 21 25, an instruction slide of the author, has no header); one of a layout, as decor_missing.
        same_place (the list variant on pictures, owner request of 29.09.2026): an item counts as shown when the layout draws
        another item in the very place where the layouts draw it (template A: layout 6 of the spring slide draws the logo as brand1,
        layout 11 as brand1 and brand2 in one frame, 29.8, 494.9)."""
        if not brand.get('items'):return 0
        if p.get('source_kind','slide')=='slide' and str(p.get('source_part','')).startswith('ppt/slides/'):
            tone=slide_tone(p['title'].get('color'))
            have=set(brand['layouts'].get(p['layout_part'],[]))|set(brand['slides'].get(p['source_part'],[]))
            lacking=[i['id'] for i in brand['items'] if tone in i['tones'] and i['id'] not in have]
            if not same_place or not lacking:return len(lacking)
            drawn=list(brand['layouts'].get(p['layout_part'],{}).values())
            def placed(item):
                return any(all(abs(a-b)<=2 for a,b in zip(box,own)) for boxes in brand['layouts'].values() for k,box in boxes.items() if k==item for own in drawn)
            return sum(1 for i in lacking if not placed(i))
        return decor_missing(brand,p['layout_part'])
    # C2 at the source: the footer of the product (disclosure, page number) takes the smallest size of the scale of the
    # reference between 8 and 12 pt and a colour of its palette that reads on the slide, not values of its own.
    # A larger footer (14 pt of deck 04) came onto the logo and the placement repairs could not move it (stage C report,
    # journal item 9); a reference without small sizes keeps an 11 px footer and the warning SIZE_OFF_SCALE.
    sizes=template_tokens(style)['sizes_pt']
    scale=[v for v in sizes if 8<=v<=12]
    footer_size=min(scale)*4/3 if scale else 11
    footer_h=max(18,math.ceil(footer_size*config['line_height'])+1)
    # C2 at the source, v2: a footer that keeps off logos may take a size of the scale up to 14 pt (deck 05, deck 03 have no
    # smaller one); it is used only where a span of the bottom band free of logos holds it.
    band_scale=[v for v in sizes if 8<=v<=14]
    band_size=min(band_scale)*4/3 if band_scale else footer_size
    band_h=max(18,math.ceil(band_size*config['line_height'])+1)
    palette=style.get('palette') or {}
    def on_scale(pt):
        """Size of the scale of the reference nearest to pt, in px (the note of a table, a subtitle the product derives)."""
        return min(sizes,key=lambda v:(abs(v-pt),v))*4/3 if sizes else pt*4/3
    def readable_ink(p):
        """Colour of text the product adds on a composition: its title colour or a colour of the palette that reads there."""
        candidates=[p['title'].get('color')]+[palette.get(k) for k in ('dk1','lt1','dk2','lt2')]
        return next((c for c in candidates if c and contrast(c,p['background'])>=4.5),p['ink'])
    # Compositions with undrawable inherited decor (A6 part): PowerPoint draws that decor on the carrier;
    # the standalone export would lose it, so there they stay excluded as before.
    patterns=ref['patterns'] if carrier else [p for p in ref['patterns'] if p.get('preview_complete',True)]
    covers=[p for p in patterns if p['role']=='cover']
    content=[p for p in patterns if p['role']=='content' and p.get('intent','text')=='text' and len(p['slots'])>=1]
    global_reason=None
    if not covers or not content:
        if ref.get('unrendered_decorations'):
            global_reason='Не поддерживается формат элементов оформления образца. Стандартные макеты не заменяют его стиль; комплект не создан.'
        elif ref.get('unanchored_raster_slides'):
            global_reason='Оформление образца находится в изображении без подтвержденных текстовых областей. Стандартные макеты не заменяют его стиль; комплект не создан.'
        else:
            global_reason='В образце не найдены поддерживаемые обложка и содержательная композиция'
        # C4a: in the carrier mode the missing cover or content slides fall back to layouts of the reference.
        if not carrier:raise ValueError(global_reason)
    def refuse(message):
        # C4a: the carrier mode returns the reason; the slide is then built by the fallback.
        if carrier:return None,global_reason or message
        raise ValueError(message)
    def find_title_plate():
        """Item 37, cause 2 (holdout-next 25.09.2026, Rosseti): the title of most content sample slides stands in a filled
        text box of one place and colour (the blue bar of Rosseti, 0C5B9D, on 11 of its 12 content slides; the layouts have a
        plain grey title placeholder there). That plate is the title style of the template: every derived content slide whose
        title stands at that place gets it. None when fewer than half the non-cover sample slides (and fewer than two) show it."""
        if not carrier:return None
        samples=[p for p in patterns if p.get('source_kind')=='slide' and p['role']!='cover']
        groups={}
        for p in samples:
            t=p['title']
            if t.get('fill') and t.get('geometry') in ('rect','roundRect'):
                groups.setdefault((t['fill'],tuple(round(v/8) for v in t['box'])),[]).append(t)
        if not groups:return None
        frames=max(groups.values(),key=len)
        if len(frames)<max(2,math.ceil(len(samples)/2)):return None
        t=frames[0]
        return {'box':list(t['box']),'fill':t['fill'],'geometry':t['geometry'],'color':t['color'],'font_size':t['font_size'],'bold':t.get('bold',False),
                'inset':list(t.get('inset') or [7.2,3.6,7.2,3.6]),'source':{'part':t['source']['part'],'shape':t['source']['shape']},'count':len(frames),'of':len(samples)}
    # Owner decisions 27-28 (26.09.2026): the scene markup of the preparation stage (scenes.py), only its fields checked
    # against the file and the pixels; empty without a model or a prepared template, so every choice stays as before.
    markup=((style.get('scene_markup') or {}).get('accepted') or {}) if carrier else {}
    def markup_plate():
        """The title band the model saw on the content sample slides (scenes.accept: its colour confirmed by the pixels of two
        of them, its title colour readable on it), as a plate of find_title_plate: the size and weight of the titles of those
        samples. Only where the rule of find_title_plate finds none (a band the file draws otherwise than as a filled title
        text box)."""
        band=markup.get('title_band')
        if not band:return None
        seen=[p for p in patterns if p.get('source_kind')=='slide' and p['source_slide'] in band['samples']]
        if not seen:return None
        # Regression mp2 of 26.09.2026 (deck 06, deck 17): the band the model saw is drawn by the decor of the composition itself (the
        # pictures come from it), and a plate over it covered the logo on the band and shrank the titles. A band that decor of
        # those samples covers for the most part is already there; only a band of another origin becomes a plate.
        b=band['box']
        if any(overlap(d['box'],b)>=b[2]*b[3]*.5 for p in seen for d in p['decorations']):return None
        sizes=sorted(p['title']['font_size'] for p in seen)
        return {'box':list(band['box']),'fill':band['fill'],'geometry':'rect','color':band['color'],'font_size':sizes[len(sizes)//2],
                'bold':sum(bool(p['title'].get('bold')) for p in seen)*2>=len(seen),'inset':[7.2,3.6,7.2,3.6],
                'source':{'part':None,'shape':None},'count':len(seen),'of':len(seen),'origin':'scene-markup'}
    def markup_zones(p,*kinds):
        """Boxes of the zones without text the model saw on the sample slide of composition p (or on a sample slide shown by
        this composition of its layout), of the given kinds."""
        return [z['box'] for z in markup.get('no_text_zones',[]) if z['what'] in kinds
                and (z.get('pattern') is not None and z['pattern']==p.get('id') or p.get('source_slide') is not None and z['slide']==p['source_slide'])]
    # Holdout-next2 26.09.2026 (19th Congress of Serbian geologists): the title frame of the layout starts over the label of the
    # logo in the top left corner, and the title covered it on every content slide; the model saw the logo there and the title
    # zone to its right. A title frame of a content composition over a logo the model saw (a zone checked against an object of
    # the file) starts or ends beside it, when at least half of its width remains; otherwise it stays.
    def off_logos(p,box):
        """A title frame beside the logos the model saw on the sample slides of composition p."""
        for z in markup_zones(p,'logo') if p is not None and p['role']!='cover' else []:
            t=box;gap=max(8,w*.01)
            if overlap(t,z)<=0:continue
            new=[z[0]+z[2]+gap,t[1],t[0]+t[2]-z[0]-z[2]-gap,t[3]] if z[0]+z[2]/2<t[0]+t[2]/2 else [t[0],t[1],z[0]-gap-t[0],t[3]]
            if new[2]>=t[2]*.5:box=[round(v,2) for v in new]
        return box
    pattern_by_id={p['id']:p for p in patterns}
    for p in patterns:
        box=off_logos(p,p['title']['box'])
        if box!=p['title']['box']:p['title']={**p['title'],'box':box,'box_before_logo':list(p['title']['box'])}
    title_plate=find_title_plate() or markup_plate()
    selected_per_section={}
    # Owner remark «слайд 25»: picks of every composition per variant (REPEAT_PENALTY).
    variant_usage=Counter()
    # Owner decision 31 (Н12): repeats of the columns variant (select) and the sections that took one.
    repeat_columns={'on':True,'done':[]}
    def large_frames(p):
        """Shapes of the large body frames of a composition: on the slide, not narrower than 20 % or lower than 14 % of it,
        not over the place of a photograph of its sample."""
        return {str(s['source'].get('shape')) for s in p['slots'] if s.get('classification',{}).get('role')=='body'
                and s['box'][0]>=0 and s['box'][1]>=0 and s['box'][0]+s['box'][2]<=w+.1 and s['box'][1]+s['box'][3]<h*.97
                and s['box'][2]>=w*.20 and s['box'][3]>=h*.14
                and not any(overlap(s['box'],f)>s['box'][2]*s['box'][3]*.5 for f in p.get('picture_frames') or [])}
    def empty_large(candidate):
        return large_frames(candidate[2])-{str(s['source'].get('shape')) for s in candidate[3]}
    def split_frame(candidate):
        return any(s['source'].get('derivation')=='partition-observed-text-region' for s in candidate[3])
    def on_photo(p,slots):
        """Item 37, cause 4 (holdout-next 25.09.2026, deck 23 slide 13): the title or a text region of the composition lies for more
        than half over the place of a photograph of its sample (an empty picture placeholder, patterns.picture_frames). The
        product sets no photograph there: the text stands on the empty half and the other half stays empty. Such a composition
        comes after the others, as a composition with an empty band between title and text does."""
        frames=(p.get('picture_frames') or [])+markup_zones(p,'photo_area')
        return any(overlap(q,f)>q[2]*q[3]*.5 for q in [p['title']['box']]+[x['box'] for x in slots] for f in frames)
    def empty_photo(p,slots):
        """Session 16 (27.09.2026), external audit of 26.09, proposal 1 (suitability of a composition): the places of photographs
        of the sample (patterns.picture_frames, markup photo_area) that no text of the slide takes cover at least
        EMPTY_PHOTO of the slide. The product sets no photograph there, and the slide shows a large empty field (MINVU columns:
        three photo places of 27 % of the slide on 8 slides, the facts in the captions under them). Such a composition comes
        after the others, as one with text over a photo place does (on_photo); it stays when nothing else holds the section."""
        frames=(p.get('picture_frames') or [])+markup_zones(p,'photo_area')
        return sum(f[2]*f[3] for f in frames if not any(overlap(x['box'],f)>f[2]*f[3]*.3 for x in slots))>=w*h*EMPTY_PHOTO
    heavy={'left':HEAVY_BUDGET}
    def weight(asset,assets=None):
        return len((assets or ref['assets']).get(asset,{}).get('data',''))
    def pattern_pictures(p,slots,budget=False):
        """Pictures a slide on composition p with text in slots shows: its cut-out illustrations (patterns.py) that no text slot
        covers for a fifth, which only the list variant places (illustrate), and the pictures of its decor (PICTURE_AREA); with
        budget, not the heavy ones beyond what is left of HEAVY_BUDGET."""
        own=[i for i in p.get('illustrations',[]) if not any(overlap(q['box'],i['box'])>q['box'][2]*q['box'][3]*.2 for q in slots)]
        decor=[d for d in p['decorations'] if d.get('asset') is not None and d['purpose']!='furniture'
               and PICTURE_AREA[0]<=d['box'][2]*d['box'][3]/(w*h)<PICTURE_AREA[1]]
        return [q for q in own+decor if not budget or weight(q['asset'])<=HEAVY_PICTURE or weight(q['asset'])<=heavy['left']]
    def heavy_bytes(p,slots):
        """Bytes of the heavy pictures a slide on composition p carries (decor and the illustrations illustrate places)."""
        return sum(weight(q['asset']) for q in pattern_pictures(p,slots) if weight(q['asset'])>HEAVY_PICTURE)
    def row_groups(p,n):
        """Owner request of 29.09.2026: a list of equal text rows beside an illustration of a sample slide (template A 7: six rows of
        40 px beside the spring, a fact of 80 signs needs three lines) holds n facts when its rows fall into n equal groups of
        neighbours: a fact takes the frame from the top of the first row of its group to the bottom of the last, and the markers
        beside the other rows of the group go (drop_row_markers)."""
        rows=[q for q in p['slots'] if q.get('classification',{}).get('role') in ('body','caption','label') and q['box'][0]>=0 and q['box'][1]>=0]
        columns=[]
        for q in sorted(rows,key=lambda q:q['box'][1]):
            column=next((c for c in columns if abs(c[0]['box'][0]-q['box'][0])<=6 and abs(c[0]['box'][2]-q['box'][2])<=6),None)
            if column is None:columns.append([q])
            else:column.append(q)
        out=[]
        for column in columns:
            k=len(column)
            if n<2 or k<=n or k%n:continue
            per=k//n;merged=[]
            for g in range(n):
                group=column[g*per:(g+1)*per]
                q=copy.deepcopy(group[0]);top=group[0]['box'][1];bottom=group[-1]['box'][1]+group[-1]['box'][3]
                q['box']=[q['box'][0],top,q['box'][2],bottom-top];q['rows_merged']=[list(r['box']) for r in group[1:]]
                merged.append(q)
            out.append(merged)
        return out
    def composition_of(pattern,slots):
        boxed=any(q.get('fill') or q.get('surface_override') or any(contains(f['box'],q['box']) and f.get('fill') for f in pattern['surfaces']) for q in slots)
        return composition_signature([q['box'] for q in slots],pattern['title']['box'],w,h,boxed)
    def fits(slot,text):
        box=body_box(slot);preferred=max(config['min_body_size_px'],min(28,slot['font_size']))
        size=text_size(text,box,preferred)
        return size>=config['min_body_size_px'] and box[3]>=size*config['line_height']
    def select(section,variant,offset,pictures=False,taken=None):
        """pictures: the choice of the list variant on pictures of the template (PICTURELESS); taken: the compositions of the other
        variants for the section, instead of every one chosen so far (that choice comes after them and leaves no trace)."""
        def pictureless(p,slots):
            return PICTURELESS if pictures and not pattern_pictures(p,slots,budget=True) else 0
        if section['kind']=='table':
            minimum=(len(section['facts'])+1)*(min(20,style['body_size'])*config['line_height']+16)
            native=[p for p in patterns if p['role']=='table' and max(p['native_table']['box'][1],p['title']['box'][1]+p['title']['box'][3]+12)+minimum<=h*.79]
            # Item 35, H3: compositions on layouts that draw text of their sample slide only when no other holds the table.
            native=[p for p in native if not layout_sample_text(brand,p.get('layout_part'))] or ([] if clean_layout else native)
            if native:return native[offset%len(native)],[]
            eligible=[p for p in content if p['title']['box'][1]<h*.25 and p['title']['box'][2]>w*.55 and max(h*.30,p['title']['box'][1]+p['title']['box'][3]+18)+minimum<=h*.79]
            eligible=[p for p in eligible if not layout_sample_text(brand,p.get('layout_part'))] or ([] if clean_layout else eligible)
            if not eligible:return refuse('В образце нет поддерживаемой области таблицы')
            eligible.sort(key=lambda p:(p.get('source_kind')=='layout',sum(d['box'][2]*d['box'][3] for d in p['decorations'] if d['purpose']=='background'),p.get('sort_order',p['source_slide'])))
            return eligible[0],[]
        facts=section['facts'];candidates=[]
        for p in content:
            if p['title']['box'][1]>h*.28 and p['family']!='split':continue
            bodies=[s for s in p['slots'] if s.get('classification',{}).get('role')=='body' and s['box'][0]>=0 and s['box'][1]>=0 and s['box'][0]+s['box'][2]<=w+.1 and s['box'][1]+s['box'][3]<h*.97]
            reflow=reflow_grid(p,bodies,len(facts),w,h) if len(bodies)>=len(facts) else None
            options=[reflow] if reflow else [list(c) for c in combinations(bodies,len(facts))]
            if pictures and p.get('illustrations'):options+=row_groups(p,len(facts))
            # Partition only source regions. Original layouts keep preference.
            for body in bodies:
                for columns in (1,2):
                    divided=partition_region(p,body,len(facts),w,h,columns)
                    if divided:options.append(divided)
            for columns in (1,2):
                alternative=reflow_grid(p,bodies,len(facts),w,h,column_count=columns) if len(bodies)>=len(facts) else None
                if alternative and [s['box'] for s in alternative] not in [[s['box'] for s in option] for option in options]:options.append(alternative)
            acceptable=[]
            for slots in options:
                slots.sort(key=lambda s:(round(s['box'][1]/20),s['box'][0]))
                if not all(fits(slot,f['text']) for slot,f in zip(slots,facts)):continue
                x0=min(s['box'][0] for s in slots);x1=max(s['box'][0]+s['box'][2] for s in slots)
                y0=min(s['box'][1] for s in slots);y1=max(s['box'][1]+s['box'][3] for s in slots)
                waste=((x1-x0)*(y1-y0)-sum(s['box'][2]*s['box'][3] for s in slots))/(w*h)
                acceptable.append((waste,slots))
            if not acceptable:continue
            # Keep alternative groupings; picking one best box here would throw
            # away the very variety the next selection stage needs.
            for waste,slots in acceptable:
                score=(0 if p['family']==variant else 2)+abs(len(p['slots'])-len(facts))*.25+waste
                score+=sum(1 for s in slots if s['box'][3]<h*.075)
                score+=sum(2 for s in slots if s['box'][2]<w*.20 or s['box'][3]<h*.14)
                score+=sum(3 for s in slots if s['font']!=style['font'])
                score-=sum(.3 for s in slots if s.get('fill') or s.get('surface_override'))
                if reflow:score-=1
                score+=sum(3 for d in p['decorations'] if d['purpose']=='background' and d['box'][2]*d['box'][3]>w*h*.65)
                score+=8 if p.get('source_kind')=='layout' else 0
                # Item 31: a composition without the mandatory decor of the sample slides comes after one with it.
                score+=8 if composition_missing(p,pictures) else 0
                score+=4 if any(s['source'].get('derivation')=='partition-observed-text-region' for s in slots) else 0
                # An empty band between the title and the text leaves the middle of the slide empty (deck 22, slide 6: captions
                # at the bottom under an illustration the product does not carry), as EMPTY_BAND does for sample cards.
                title=p['title']['box']
                if min(s['box'][1] for s in slots)-(title[1]+title[3])>h*EMPTY_BAND:score+=6
                score+=6 if on_photo(p,slots) else 0
                score+=6 if empty_photo(p,slots) else 0
                score+=REPEAT_PENALTY*variant_usage[(variant,p['id'])]
                candidates.append((score+pictureless(p,slots),p.get('sort_order',p['source_slide']),p,slots))
        if not candidates and len(facts)>=2:
            # G2 for several facts (proposals of 24.09.2026): no region of the compositions holds one fact each (deck 29 2:
            # one sample line in a 68 px frame), and the section would leave the style for a plain fallback layout. The facts
            # then go into the allowed region of a plain text region (grow_region): as paragraphs of one text, the way a body
            # placeholder holds them, or in two columns of that region; the list suits the list variant, the columns the
            # columns variant, and the variants take distinct compositions as before.
            value='\n'.join(f['text'] for f in facts)
            def fits_list(slot):
                box=body_box(slot);preferred=max(config['min_body_size_px'],min(28,slot['font_size']))
                size=text_size(value,box,preferred,config['line_height'])
                return size>=config['min_body_size_px'] and estimate_lines(value,box[2],size)*size*config['line_height']+size*.5*(len(facts)-1)<=box[3]
            for p in content:
                if p['title']['box'][1]>h*.28 and p['family']!='split':continue
                for body in [s for s in p['slots'] if s.get('classification',{}).get('role')=='body' and s['box'][0]>=0 and s['box'][1]>=0 and s['box'][0]+s['box'][2]<=w+.1]:
                    grown=grow_region(p,body,w,h)
                    if grown is None:continue
                    options=[]
                    if fits_list(grown):
                        listed=copy.deepcopy(grown);listed['list']=len(facts);options.append(('list',[listed]))
                    for kind,columns in (('stack',1),('columns',2)):
                        region=grown
                        if columns==1:
                            # Blocks one under another, each as high as its longest fact and a line more: the stack of the
                            # blocks variant, not facts spread over the whole region.
                            size=max(config['min_body_size_px'],min(28,grown['font_size']));gap=max(12,min(28,grown['font_size']*.9))
                            cell=max(estimate_lines(f['text'],grown['box'][2],size) for f in facts)*size*config['line_height']+size
                            region=copy.deepcopy(grown);region['box'][3]=min(grown['box'][3],len(facts)*cell+(len(facts)-1)*gap)
                        divided=partition_region(p,region,len(facts),w,h,columns)
                        if divided and all(fits(slot,f['text']) for slot,f in zip(divided,facts)):options.append((kind,divided))
                    preferred={'sequence':'list','split':'stack','columns':'columns'}.get(variant)
                    for kind,slots in options:
                        score=(0 if p['family']==variant else 2)+(0 if kind==preferred else 1)
                        score+=8 if p.get('source_kind')=='layout' else 0
                        score+=8 if composition_missing(p,pictures) else 0
                        score+=sum(3 for d in p['decorations'] if d['purpose']=='background' and d['box'][2]*d['box'][3]>w*h*.65)
                        score+=6 if on_photo(p,slots) else 0
                        score+=6 if empty_photo(p,slots) else 0
                        score+=REPEAT_PENALTY*variant_usage[(variant,p['id'])]
                        candidates.append((score+pictureless(p,slots),p.get('sort_order',p['source_slide']),p,slots))
        if not candidates:return refuse('Нет поддерживаемой области основного текста нужной вместимости: '+section['id'])
        candidates.sort(key=lambda x:x[:2])
        # Item 35, H3 (holdout 25.09.2026): a composition on a layout that draws text of its sample slide ("LEVEL 3 WILL:" of
        # deck 08 over the facts; the labels "CHALLENGE:", "SOLUTION:" of deck 15) is taken only when no other holds the
        # section; wrong words on a slide weigh more than a missing band, so this comes before the rule of the decor.
        free=[c for c in candidates if not layout_sample_text(brand,c[2].get('layout_part'))]
        if candidates and not free and clean_layout:
            return refuse('Композиции образца стоят на макетах, которые рисуют текст своего образца')
        candidates=free or candidates
        # Item 31 (25.09.2026): while a composition with the mandatory decor of the sample slides holds the section, one
        # without it is not taken, even for a composition another variant already has (deck 25: only the sample layout has the
        # band; distinct compositions sent the blocks and columns variants to plain layouts). The variants still differ as
        # decks (audit_diversity and its fallbacks below).
        candidates=[c for c in candidates if not composition_missing(c[2],pictures)] or candidates
        used=set(taken) if taken is not None else selected_per_section.setdefault(section['id'],set())
        signature=composition_of
        pool=[c for c in candidates if signature(c[2],c[3]) not in used]
        if not pool and len(facts)>=3:
            for score,n,p,old_slots in candidates:
                bodies=[s for s in p['slots'] if s.get('classification',{}).get('role')=='body' and contains([0,0,w,h*.92],s['box'])]
                alternative=reflow_grid(p,bodies,len(facts),w,h,column_count=2)
                if alternative and [s['box'] for s in alternative]!=[s['box'] for s in old_slots] and all(fits(slot,f['text']) for slot,f in zip(alternative,facts)):
                    pool=[(score,n,p,alternative)];break
        pool=pool or candidates
        result=pool[0]
        # Owner decision 31 (Н12, 26.09.2026): the columns variant may repeat the composition another variant took for the
        # same section when otherwise it splits a frame and a large frame of that composition stays empty (deck 11: both facts
        # stacked in the left frame, the blue panel of the right half empty on every slide; 37 slides of 5 templates in 123
        # runs). It takes the best candidate of its own family that splits nothing, leaves no large frame empty and has no
        # small frame (research-bsu-columns.md of 20260926-hn2-rest, rule Bgn); the deck guard below undoes it where the
        # variants stop differing.
        if variant=='columns' and repeat_columns['on'] and split_frame(result) and empty_large(result):
            own=[c for c in candidates if c[2]['family']=='columns' and not any(s['source'].get('derivation') for s in c[3])
                 and not empty_large(c) and not any(s['box'][2]<w*.20 or s['box'][3]<h*.14 for s in c[3])]
            if own and signature(own[0][2],own[0][3]) in used:
                result=own[0];repeat_columns['done'].append(section['id'])
        used.add(signature(result[2],result[3]))
        variant_usage[(variant,result[2]['id'])]+=1
        return result[2],result[3]

    layouts={l['part']:l for l in style['carrier']['layouts']} if carrier else {}
    placeholder_candidates={kind:placeholder_layouts(style,kind) for kind in PLACEHOLDER_KINDS.values()} if carrier else {}
    # C4a: the composition of a layout itself (layout-N) gives a fallback slide its background and decor for the preview.
    own_patterns={p['layout_part']:p for p in patterns if p.get('source_kind')=='layout'} if carrier else {}
    # A7: templates without body layouts for the placeholder mode (template A, template B) lay text sections out on cloned
    # cards of their sample slides. Picks are made up front for all variants, so every compose() call is deterministic.
    by_slide={p['source_slide']:p for p in patterns if p.get('source_kind')=='slide'}
    sample_sets=[s for s in style['carrier'].get('card_samples',[]) if s['slide'] in by_slide and s['layout'] in layouts and s.get('kind','cards')=='cards'
                 ] if carrier else []
    # The zones of charts, key numbers and processes (visual_zone) stay as before E1: they take new objects, not text in
    # the slots of the cards; the cards that take facts must hold them readably.
    zone_samples=sample_sets if not any(placeholder_candidates.values()) else []
    card_samples=[s for s in sample_sets if readable_cards(s)]
    card_variants=[v for v in CARD_ORDER if v in config['variants'] and (v in CARDS_BESIDE_PLACEHOLDERS or not any(placeholder_candidates.values()))]
    card_picks={}
    icon_picks=(style['carrier'].get('icon_choice') or {}).get('picks',{}) if carrier else {}
    # Owner request of 29.09.2026: the card sets of the list variant with a picture, chosen after the other variants (PICTURE_AREA).
    card_pictures={};card_signatures={};picture_signatures={}
    def plan_pictured(sample,plan):
        nodes={n['id']:n for n in sample['nodes']}
        return any(any(q['type']=='image' for q in nodes[x['shape']]['primitives']) and PICTURE_AREA[0]<=x['frame'][2]*x['frame'][3]/(w*h)<PICTURE_AREA[1]
                   for x in plan['shapes'])
    def card_heavy(sample,plan):
        """Bytes of the heavy pictures the clones of a card plan carry (HEAVY_PICTURE)."""
        nodes={n['id']:n for n in sample['nodes']};assets=style['carrier'].get('card_assets') or {}
        found={q['asset'] for x in plan['shapes'] for q in nodes[x['shape']]['primitives'] if q['type']=='image'}
        return sum(weight(a,assets) for a in found if weight(a,assets)>HEAVY_PICTURE)
    if card_samples:
        usage=Counter();taken={};plans={}
        limit=max(2,math.ceil(sum(1 for x in brief['sections'] if x['kind']=='text')*CARD_REPEAT_SHARE))
        for variant in card_variants:
            order=CARD_PREFERENCE[variant]
            for section in brief['sections']:
                if section['kind']!='text':continue
                ranked=[]
                for sample in card_samples:
                    for mode in order:
                        key=(section['id'],sample['slide'],mode)
                        if key not in plans:plans[key]=plan_cards(sample,section['facts'],w,h,mode)
                        plan=plans[key]
                        if plan is None:continue
                        # Signature of the fact texts only: a label is a heading of its fact, not another block.
                        signature=composition_signature([t['box'] for t in plan['texts'] if t['role']=='fact'],by_slide[sample['slide']]['title']['box'],w,h,True)
                        # A slide with an empty band between its title and the cards (the sample lost its text outside the
                        # cards above them: template B 12, 13) is worse than a composition another variant has; then a distinct
                        # composition, the preferred arrangement, samples that lose no text outside their cards, cards with
                        # labels of the facts, rotation among samples within the variant, fewest removed cards, the layout the
                        # reference uses most, document order.
                        title=by_slide[sample['slide']]['title']['box']
                        empty=min(t['box'][1] for t in plan['texts'])-(title[1]+title[3])>h*EMPTY_BAND
                        # G3: within the preferred arrangement, a section with chosen pictograms takes cards that have
                        # places for them (template A 24), so that the choice reaches the slide.
                        icon_miss=bool(icon_picks.get(section['id'])) and not places_icons(sample,plan,style,section['id'])
                        rank=(empty,signature in taken.get(section['id'],set()),usage[(variant,sample['slide'])]>=limit,order.index(mode),icon_miss,sample.get('removed_outside_cards',0)>0,not plan['labels'],
                              usage[(variant,sample['slide'])],abs(len(sample['cards'])-len(section['facts'])),-layouts[sample['layout']]['used_by_slides'],sample['slide'])
                        ranked.append((rank,sample,plan,signature))
                if ranked:
                    _,sample,plan,signature=min(ranked,key=lambda r:r[0])
                    card_picks[(variant,section['id'])]=(sample,plan);card_signatures[(variant,section['id'])]=signature
                    usage[(variant,sample['slide'])]+=1;taken.setdefault(section['id'],set()).add(signature)
        if 'sequence' in card_variants:
            # The list variant again: only plans whose clones show a picture; a band left empty under the title, a composition
            # another variant has for the section and the most used sample come last, then the arrangements of the variant.
            pictured_usage=Counter()
            for section in brief['sections']:
                if section['kind']!='text':continue
                others={card_signatures[(v,section['id'])] for v in card_variants if v!='sequence' and (v,section['id']) in card_signatures}
                ranked=[]
                for sample in card_samples:
                    for mode in CARD_PREFERENCE['sequence']:
                        key=(section['id'],sample['slide'],mode)
                        if key not in plans:plans[key]=plan_cards(sample,section['facts'],w,h,mode)
                        plan=plans[key]
                        if plan is None or not plan_pictured(sample,plan):continue
                        title=by_slide[sample['slide']]['title']['box']
                        empty=min(t['box'][1] for t in plan['texts'])-(title[1]+title[3])>h*EMPTY_BAND
                        signature=composition_signature([t['box'] for t in plan['texts'] if t['role']=='fact'],title,w,h,True)
                        ranked.append(((empty,signature in others,pictured_usage[sample['slide']],CARD_PREFERENCE['sequence'].index(mode),sample['slide']),sample,plan,signature))
                if ranked:
                    _,sample,plan,signature=min(ranked,key=lambda r:r[0])
                    card_pictures[section['id']]=(sample,plan);picture_signatures[section['id']]=signature;pictured_usage[sample['slide']]+=1
    # G3: in the columns variant a text section whose every fact got a pictogram stands on a row of pictogram units of the
    # sample (template C 26-28), before cards and layout placeholders; the units rotate among the samples.
    unit_samples=[s for s in style['carrier'].get('card_samples',[]) if s.get('kind')=='units' and s['arrangement']=='row'
                  and s['slide'] in by_slide and s['layout'] in layouts] if carrier and 'columns' in config['variants'] else []
    unit_usage=Counter()
    for section in brief['sections']:
        picked=icon_picks.get(section['id'],{})
        if section['kind']!='text' or not unit_samples or any(f['id'] not in picked for f in section['facts']):continue
        ranked=[]
        for sample in unit_samples:
            plan=plan_cards(sample,section['facts'],w,h,'row')
            if plan is None or card_icons(sample,plan,style,section['id']) is None:continue
            ranked.append(((unit_usage[sample['slide']],abs(len(sample['cards'])-len(section['facts'])),sample['slide']),sample,plan))
        if ranked:
            _,sample,plan=min(ranked,key=lambda r:r[0])
            card_picks[('columns',section['id'])]=(sample,plan);unit_usage[sample['slide']]+=1

    def compose(vi,variant,picks,placeholders,forced=None):
        doc={'schema':'vsp.document/1','revision':0,'variant':variant,'width':w,'height':h,
             'style':{k:copy.deepcopy(style[k]) for k in ('font','accent','ink','fonts','header_asset')},
             'brief':copy.deepcopy(brief),'slides':[],'composition_engine':'reference-patterns-v1'}
        fallback=CarrierLayouts(style['carrier']) if carrier else None
        def text_background(slide,box):
            """Solid background under a text box, and whether a background picture lies under it (C4b, 3).

            On the carrier a text box on a background picture (decor of purpose 'background': an image of the
            reference that is not a protected asset) keeps the colour of the reference; the rendered check decides
            the contrast. An opaque shape above the picture brings the solid rule back. Standalone: never a picture.
            """
            background=slide['background'];raster=False
            for e in slide['elements']:
                if e['type']=='shape' and contains([e[k] for k in ('x','y','w','h')],box):
                    background=blend(e['fill'],background,e.get('opacity',1))
                    if e.get('opacity',1)>=1:raster=False
                elif carrier and e['type']=='image' and e.get('template_decoration') and not e.get('protected_asset') and contains([e[k] for k in ('x','y','w','h')],box):
                    raster=True
            return background,raster
        def add_text(slide,eid,value,frame,facts=None,color=None):
            box=frame['box'];size=text_size(value,box,frame['font_size'],config['line_height'])
            background,raster=text_background(slide,box)
            original_color=color or frame['color'];ink=original_color
            if contrast(ink,background)<3 and not raster:
                ink=max(('FFFFFF','151515'),key=lambda c:contrast(c,background))
            own=[]
            if carrier and raster and (color is None and frame.get('source',{}).get('kind') in ('slide','layout') or frame.get('scene_ink')) and any(
                    e['type']=='image' and e.get('template_decoration') and e['w']*e['h']>=w*h*.9 for e in slide['elements']):
                # Item 37, cause 3 (holdout-next 25.09.2026, deck 19): on the picture that fills the slide, text in the colour
                # the composition sets it in (white on the light blue of deck 19, about 2.5:1) is the pair of colours of the
                # template itself, as H2b keeps it on a solid backdrop; the render check reports it as the reference's own
                # colour when it reads at least 2:1 there, and repaints it only below that (quality.js qInspectText).
                own=[{'reason':'template-colour-kept','colour':ink,'background':'picture'}]
            # Only fonts loaded from this reference can be asserted available.
            loaded={f['family'] for f in style['fonts']}
            family=frame['font'] if frame['font'] in loaded else style['font']
            # size_own: the size of the frame of the reference before the composer fitted the text (quality.js grows text and
            # groups titles by it; deck quality, 23.09.2026).
            slide['elements'].append({'id':eid,'type':'text','text':value,**dict(zip(('x','y','w','h'),[round(v,2) for v in box])),
                'font':family,'font_size':size,'size_own':round(frame.get('size_own',frame['font_size']),2),'color':ink,'bold':frame.get('bold',False),
                'align':frame.get('align','left'),'line_height':config['line_height'],'fact_ids':facts or [],
                'source_style':copy.deepcopy(frame.get('source',{})),
                'reference_box':copy.deepcopy(box),
                'content_role':frame.get('classification',{}),
                'style_adjustments':own+([] if ink==original_color else [{'reason':'solid-background-contrast','from':original_color,'to':ink,'background':background}])})
        def begin(sid,role,p,skip_part=None,cloned=()):
            s={'id':sid,'role':role,'background':p['background'],'elements':[],
               'reference':{'pattern':p['id'],'source_slide':p['source_slide'],'source_part':p['source_part'],
                            'layout_part':p['layout_part'],'background_origin':p['background_origin'],'family':p['family'],'source_kind':p.get('source_kind','slide')}}
            unrendered_own=[]
            for n,d in enumerate(p['decorations']):
                # A7: shapes of the sample slide itself come as clones, not as decorations of its composition.
                if skip_part and d['source'].get('part')==skip_part and d['source'].get('kind')!='background-fill':continue
                # Item 37, cause 3: a line or an undrawable logo of the sample slide (patterns.py) is a copy of that shape in
                # the PPTX (bind_carrier: furniture); without the carrier there is no such copy, and the slide stays as before.
                if d['source'].get('kind') in ('slide-line','unrendered-slide-picture') and not carrier:continue
                if d.get('kind')=='ghost':
                    s['elements'].append({'id':f'decor{n}','type':'shape',**dict(zip(('x','y','w','h'),d['box'])),'geometry':'rect','fill':p['background'],
                        'opacity':0,'ghost':True,'template_decoration':True,'locked':True,'fact_ids':[],'source_style':d['source']})
                    unrendered_own.append(d['source']['media'])
                    continue
                if d.get('kind')=='backplate':
                    # Holdout-next2 (deck 32): an undrawable picture over the whole sample slide under its text is its backplate:
                    # the picture of the sample slide in the PPTX (bind_carrier: furniture), invisible in the preview, and no
                    # obstacle — the text of the slide stands on it as on the sample.
                    s['elements'].append({'id':f'decor{n}','type':'shape',**dict(zip(('x','y','w','h'),d['box'])),'geometry':'rect','fill':p['background'],
                        'opacity':0,'background_decoration':True,'template_decoration':True,'locked':True,'fact_ids':[],'source_style':d['source']})
                    unrendered_own.append(d['source']['media'])
                    continue
                if d.get('kind')=='shape':
                    s['elements'].append({'id':f'decor{n}','type':'shape',**dict(zip(('x','y','w','h'),d['box'])),'geometry':d['geometry'],'fill':d['fill'],'opacity':d.get('opacity',1),
                        **({'adj':d['adj']} if d.get('adj') is not None else {}),
                        'background_decoration':True,'template_decoration':True,'locked':True,'fact_ids':[],'source_style':d['source']})
                    continue
                asset=ref['assets'][d['asset']]
                s['elements'].append({'id':f'decor{n}','type':'image',**dict(zip(('x','y','w','h'),d['box'])),
                    'mime':asset['mime'],'data':asset['data'],'crop':d['crop'],'fact_ids':[],
                    'template_decoration':True,'protected_asset':d['purpose']=='furniture','locked':True,'source_style':d['source']})
            if carrier and p.get('preview_complete') is False:
                # A6b: every frame of undrawable inherited decor (EMF) is an invisible obstacle: a transparent
                # locked shape bound to the layout or master, not written to the PPTX (PowerPoint draws the decor).
                # quality.js treats it as a logo and as an obstacle for placement repairs, not as a backplate.
                # Frames larger than GHOST_MAX_AREA of the slide (after clipping to the canvas) are backgrounds.
                for n,u in enumerate(p.get('unrendered',[])):
                    x,y,uw,uh=u['box'];x0,y0,x1,y1=max(0,x),max(0,y),min(w,x+uw),min(h,y+uh)
                    if x1<=x0 or y1<=y0 or (x1-x0)*(y1-y0)>w*h*GHOST_MAX_AREA:continue
                    s['elements'].append({'id':f'ghost{n}','type':'shape',**dict(zip(('x','y','w','h'),[round(v,2) for v in (x0,y0,x1-x0,y1-y0)])),
                        'geometry':'rect','fill':p['background'],'opacity':0,'ghost':True,'template_decoration':True,'locked':True,'fact_ids':[],
                        'source_style':{'kind':'unrendered-inherited-decoration','part':u['source_part'],'media':u['media'],'box':list(u['box']),
                                        'reason':u['reason'],'pattern':u['pattern']}})
            if carrier and role!='cover':
                # Item 31: lines and text of the mandatory decor that the layout or master draws are not in the model (the
                # preview draws neither); an invisible obstacle for each keeps the footer and the content off them (deck 25: the
                # disclosure crossed the line of the band).
                for k,item in enumerate(i for i in brand.get('items',[]) if not i['transferable'] and i['kind'] in ('line','text')
                                        and i['id'] in brand['layouts'].get(p['layout_part'],[])):
                    x,y,bw,bh=brand['layouts'][p['layout_part']][item['id']]   # where this layout draws it
                    # Beyond the side margins of the footer (end) nothing of the slide is placed; an obstacle there only
                    # widens the free span before it (Office Urban: its line at the right edge).
                    if x>=w-max(20,w*.035) or x+bw<=max(20,w*.035):continue
                    x,y=(x-1,y) if bw<2 else (x,y-1) if bh<2 else (x,y);bw,bh=max(bw,2),max(bh,2)
                    x0,y0,x1,y1=max(0,x),max(0,y),min(w,x+bw),min(h,y+bh)
                    if x1-x0<.5 or y1-y0<.5 or (x1-x0)*(y1-y0)>w*h*GHOST_MAX_AREA:continue
                    s['elements'].append({'id':f'brandghost{k}','type':'shape',**dict(zip(('x','y','w','h'),[round(v,2) for v in (x0,y0,x1-x0,y1-y0)])),
                        'geometry':'rect','fill':p['background'],'opacity':0,'ghost':True,'template_decoration':True,'locked':True,'fact_ids':[],
                        'source_style':{'kind':'inherited-brand-decor','part':p['layout_part'],'item':item['id']}})
            if carrier:
                # Item 35, H5 (holdout 25.09.2026, deck 33): a slide number the layout or its master draws itself is not in the
                # model; an invisible obstacle keeps the footer of the product off it (end() sets no number of its own there).
                for k,(x,y,bw,bh) in enumerate(drawn_pages(p)):
                    x0,y0,x1,y1=max(0,x),max(0,y),min(w,x+bw),min(h,y+bh)
                    if x1-x0<.5 or y1-y0<.5:continue
                    s['elements'].append({'id':f'pageghost{k}','type':'shape',**dict(zip(('x','y','w','h'),[round(v,2) for v in (x0,y0,x1-x0,y1-y0)])),
                        'geometry':'rect','fill':p['background'],'opacity':0,'ghost':True,'template_decoration':True,'locked':True,'fact_ids':[],
                        'source_style':{'kind':'inherited-page-number','part':p['layout_part']}})
            carry_brand(s,p,cloned)
            s['style_contract']={'schema':'vsp.style-contract/1','background':p['background'],
                'basis':copy.deepcopy(p.get('compatibility',{})),
                'decorations':{e['id']:decoration_signature(e) for e in s['elements']}}
            if p.get('preview_complete') is False:
                # A6 (part): the layout or master draws decor the preview cannot; PowerPoint draws it on the carrier.
                s['preview_notes']=[preview_note([u['media'] for u in p.get('unrendered',[])])]
            if unrendered_own:
                # Item 37, cause 3: the sample slide itself draws a logo the preview cannot (EMF of deck 19).
                s.setdefault('preview_notes',[]).append(preview_note(unrendered_own,reason='sample-slide-picture'))
            doc['slides'].append(s);return s
        def band_obstacles(s):
            """Frames the footer keeps off: the footer itself, logos, protected assets and undrawable decor frames, and the text
            and pictures of the mandatory decor carried from the sample slides (item 31; a band shape stays a backplate)."""
            return [e for e in s['elements'] if e['id'] in ('disclosure','page') or e.get('brand_item') and e['type'] in ('text','image')
                    or e['type'] in ('image','shape') and e.get('template_decoration')
                    and (e.get('protected_asset') or e.get('ghost') or e['type']=='image' and e['w']<w*.35 and e['h']<h*.2)]
        def free_spans(s,top,bottom,margin,clear=0):
            """Spans (x0, x1) of a horizontal band of a slide that no logo, protected asset or undrawable decor frame covers
            or comes within `clear` px of above or below."""
            blocked=sorted((e['x'],e['x']+e['w']) for e in band_obstacles(s) if e['y']<bottom+clear and e['y']+e['h']>top-clear)
            spans,cursor=[],margin
            for a,b in blocked:
                if a-8>cursor:spans.append((cursor,a-8))
                cursor=max(cursor,b+8)
            if w-margin>cursor:spans.append((cursor,w-margin))
            return spans
        def used_box(e):
            """Where a text element really has glyphs: its frame narrowed to the estimated lines by its anchor (a placeholder
            frame is often far taller than its text); other elements: their frame."""
            box=[e[k] for k in ('x','y','w','h')]
            if e['type']!='text':return box
            l,t,r,b=e.get('inset') or [0,0,0,0]
            lines=estimate_lines(e['text'],max(1,e['w']-l-r),e['font_size'])
            need=min(e['h'],lines*e['font_size']*e.get('line_height',config['line_height'])+t+b)
            anchor=e.get('anchor','t')
            y=e['y'] if anchor=='t' else e['y']+e['h']-need if anchor=='b' else e['y']+(e['h']-need)/2
            return [e['x']+l,y,max(1,e['w']-l-r),need]
        def brand_elements(item,source,n):
            """Document elements of one item of mandatory decor copied from a sample slide: its preview (reference.brand_decor,
            sample_clone.brand_decor_preview) bound to the source shape, which the PPTX carries whole (binding furniture, once
            per slide). A running title (deck 21: "Название презентации", the title of the sample cover) takes the title of the brief
            at the largest size of the scale down to 60 % of its own at which it keeps one line."""
            binding={'kind':'furniture','source':{'part':source[0],'shape':str(source[1])},'brand_item':item['id']}
            loaded={f['family'] for f in style['fonts']}
            out=[]
            for m,prim in enumerate(item['preview']):
                # Clipped to the canvas, as PowerPoint draws it (a turned band ends at the edge of the slide).
                x0,y0=max(0,prim['box'][0]),max(0,prim['box'][1]);x1,y1=min(w,prim['box'][0]+prim['box'][2]),min(h,prim['box'][1]+prim['box'][3])
                if x1-x0<.5 or y1-y0<.5:continue
                bw,bh=x1-x0,y1-y0
                base={'id':f'brand{n}-{m}',**dict(zip(('x','y','w','h'),[round(v,2) for v in (x0,y0,bw,bh)])),'fact_ids':[],
                      'template_decoration':True,'locked':True,'binding':binding,'brand_item':item['id'],
                      'source_style':{'kind':'brand-decor','part':source[0],'shape':str(source[1]),'item':item['id']}}
                if prim['type']=='image':
                    asset=brand['assets'][prim['asset']]
                    out.append({**base,'type':'image','mime':asset['mime'],'data':asset['data'],'crop':prim['crop'],'protected_asset':bw*bh<w*h*.05})
                elif prim['type']=='shape':
                    out.append({**base,'type':'shape','geometry':prim['geometry'],'fill':prim['fill'],'opacity':prim.get('opacity',1),
                                'background_decoration':True,**({'adj':prim['adj']} if prim.get('adj') is not None else {})})
                elif prim['type']=='ghost':
                    if bw*bh<=w*h*GHOST_MAX_AREA:out.append({**base,'type':'shape','geometry':'rect','fill':'FFFFFF','opacity':0,'ghost':True})
                else:
                    t=prim['typography'];value=prim['text'];lines=list(prim['lines']);own=t['size_pt'];size_pt=own
                    spacing=t.get('line_spacing') or {'pct':100.0}
                    line=1.2*spacing['pct']/100 if 'pct' in spacing else spacing['pt']/own
                    l,tp,r,b=t.get('insets') or [7.2,3.6,7.2,3.6]
                    if prim.get('running_title'):
                        value=brief['title'];lines=[value]
                        room=max(1,prim['frame'][2]-l-r)
                        steps=sorted({v for v in sizes if own*.6-.01<=v<=own+.01}|{own},reverse=True)
                        size_pt=next((v for v in steps if estimate_lines(value,room,v*4/3)==1),steps[-1])
                        binding['replace_text']=[{'from':' '.join(prim['text'].split()),'to':value,**({'size_pt':size_pt} if size_pt!=own else {})}]
                    element={**base,'type':'text','text':value,'font':t['font'] if t['font'] in loaded else style['font'],'font_size':round(size_pt*4/3,2),
                             'color':t['color'],'bold':t['bold'],'align':t['align'],'line_height':round(line,4),'inset':[l,tp,r,b],'anchor':t['anchor'],
                             'decor':True,'package_lines':lines}
                    if prim.get('rotation'):element.update(rotation=prim['rotation'],frame=list(prim['frame']))
                    out.append(element)
            return out
        def carried(p):
            """Items of mandatory decor copied from the sample slides that a slide on composition p takes (item 31): on a slide
            built on a sample slide, the items that sample shows; on a slide on a layout, the items of the tone of its title
            that the layout neither draws nor blocks (a text frame over the item). Returns (item, source shape) pairs."""
            if not brand_items:return []
            scene=p['source_part'] if p.get('source_kind','slide')=='slide' and str(p.get('source_part','')).startswith('ppt/slides/') else None
            drawn=set(brand['layouts'].get(p['layout_part'],[]))
            if scene is not None:
                return [(i,(scene,i['occurrences'][scene])) for i in brand_items if i['id'] not in drawn and i['id'] in brand['slides'].get(scene,[])
                        and scene in i['occurrences']]
            tone=slide_tone(p['title'].get('color')) if p.get('title') else brand.get('layout_tones',{}).get(p['layout_part'],'light')
            blocked=set(brand.get('blocked',{}).get(p['layout_part'],[]))
            return [(i,(i['source']['part'],i['source']['shape'])) for i in brand_items if i['id'] not in drawn and i['id'] not in blocked and tone in i['tones']]
        def carry_brand(s,p,cloned=()):
            """Item 31 (25.09.2026): mandatory decor of the sample slides that only the slides hold (the band of deck 25 with its
            text, the header of deck 21 with the name of the presentation, the band of deck 18) comes onto a content slide as a copy
            of the sample shape before its content is placed, so charts, cards and the footer keep off it (logo_boxes,
            band_obstacles, visual_zone). A slide built on a sample slide takes the items that sample shows, whole, in place of
            the pieces of them its composition brought (deck 21: the logos of the header without its text), unless the slide
            already has that shape (A7 clones the visible decor of its sample: cloned)."""
            if s['role']=='cover':return
            shown=set(cloned)
            for e in s['elements']:
                src=e.get('source_style') if e.get('template_decoration') else None
                if src and src.get('part') and src.get('shape') is not None:shown.add((src['part'],str(src['shape'])))
            for n,(item,source) in enumerate(carried(p)):
                if (source[0],str(source[1])) in shown:continue
                box=item['box']
                pieces=[e for e in s['elements'] if e.get('template_decoration') and (e.get('source_style') or {}).get('part')==source[0]
                        and contains(box,[e[k] for k in ('x','y','w','h')],3)]
                for e in pieces:s['elements'].remove(e)
                s['elements'].extend(brand_elements(item,source,n))
        def clear_of_brand(p,box):
            """The largest part of a zone that the decor carried onto a slide on composition p leaves free."""
            return clear_of(box,[i['box'] for i,_ in carried(p)])
        def put_title_plate(s):
            """Item 37, cause 2: the plate of the sample titles (find_title_plate) under a title that stands at its place — the
            title frame and the plate overlap by at least half the smaller one and nothing but decor lies on the plate. The title
            takes the plate as its frame (less its insets), and its colour, weight and size (fitted down to 12 px); the plate is a
            native filled shape under it."""
            t=next((e for e in s['elements'] if e['id']=='title' and e['type']=='text'),None)
            if t is None:return
            pb=title_plate['box'];tb=[t[k] for k in ('x','y','w','h')]
            if overlap(tb,pb)<.5*min(tb[2]*tb[3],pb[2]*pb[3]):return
            if any(overlap([e[k] for k in ('x','y','w','h')],pb)>0 for e in s['elements']
                   if e is not t and not e.get('template_decoration') and not e.get('ghost') and e['id'] not in ('disclosure','page')):return
            l,top,r,b=title_plate['inset'];before={k:t[k] for k in ('x','y','w','h','font_size','color','bold')}
            # At least a quarter of the size of the plate text on the left and right: the text box of Rosseti itself has no
            # insets, and letters on the very edge of the bar are cramped and read against the white beside it.
            l,r=max(l,title_plate['font_size']*.25),max(r,title_plate['font_size']*.25)
            # The title takes the plate less the insets of its text box (the title placeholder of Rosseti has none: the first letter
            # stood on the edge of the plate, and the render check sampled the white beside it), fitted with 8 % of that height to
            # spare (two lines to the very edge put descenders on the white under the plate).
            box=[pb[0]+l,pb[1]+top,max(1,pb[2]-l-r),max(1,pb[3]-top-b)]
            if (t.get('binding') or {}).get('kind')=='placeholder':
                typography={**t['binding']['typography'],'size_pt':title_plate['font_size']*.75}
                size=placeholder_text_size(t['text'],[box[0],box[1],box[2],box[3]*.92],typography,12) or 12
            else:
                size=text_size(t['text'],[box[0],box[1],box[2],box[3]*.92],title_plate['font_size'],config['line_height'])
            t.update({k:round(v,2) for k,v in zip(('x','y','w','h'),box)});t.update(font_size=size,color=title_plate['color'],bold=title_plate['bold'])
            t['style_adjustments']=[a for a in t.get('style_adjustments',[]) if a.get('reason')!='solid-background-contrast']+[
                {'reason':'title-plate','from':before,'plate':{k:title_plate[k] for k in ('box','fill','count','of')}}]
            s['elements'].insert(s['elements'].index(t),{'id':'titleplate','type':'shape',**dict(zip(('x','y','w','h'),[round(v,2) for v in pb])),
                'geometry':title_plate['geometry'],'fill':title_plate['fill'],'opacity':1,'background_decoration':True,'template_decoration':True,'locked':True,
                'fact_ids':[],'source_style':{'kind':'title-plate','part':title_plate['source']['part'],'plate_shape':title_plate['source']['shape']}})
            s['style_contract']['decorations']['titleplate']=decoration_signature(s['elements'][s['elements'].index(t)-1])
        def hug_sample_plate(s,p):
            """Owner decision 48, pitch P7 (29.09.2026, template D): the title of a sample slide stands on a filled plate
            of that slide (the pink bar of template D holds a short title in capitals, light on it). A derived title is longer: the check
            of the rendering found it overflowing the one line of its frame, grew the frame down off the plate and recoloured the
            title dark on the white beside it, and the plate stood empty (5 of 11 slides of the split variant of P7, 2 of 11 of
            P6). The plate follows the title: as wide as its lines need (up to the frame and half the slide, where logos of the
            template begin), as high as they are; the title stands on it in the colour of the template, where that reads on the
            plate (3:1, the threshold of the check of the rendering). Nothing changes where the plate would reach other content."""
            t=next((e for e in s['elements'] if e['id']=='title' and e['type']=='text'),None)
            if t is None or not t.get('text') or any(e['id']=='titleplate' for e in s['elements']):return
            rb=t.get('reference_box') or [t[k] for k in ('x','y','w','h')]
            l,top,r,b=t.get('inset') or [9.6,4.8,9.6,4.8]
            px,py=rb[0]+l+4,rb[1]+rb[3]/2
            plates=[e for e in s['elements'] if e['type']=='shape' and e.get('template_decoration') and not e.get('ghost') and e.get('opacity',1)>=.95
                    and e.get('geometry') in ('rect','roundRect') and e.get('fill') and (e.get('source_style') or {}).get('part')==p.get('source_part')
                    and e['w']*e['h']<w*h*.1 and e['h']<=3*rb[3] and e['x']<=px<=e['x']+e['w'] and e['y']<=py<=e['y']+e['h']]
            if len(plates)!=1:return
            plate=plates[0];pb=[plate[k] for k in ('x','y','w','h')]
            def blocks(e):
                return (e is not t and e is not plate and not e.get('ghost') and e['id'] not in ('page','disclosure')
                        and not (e.get('template_decoration') and e['w']*e['h']>=w*h*.5))
            others=[[e[k] for k in ('x','y','w','h')] for e in s['elements'] if blocks(e)]
            if any(overlap(o,pb)>0 for o in others):return
            colour=((t.get('binding') or {}).get('typography') or {}).get('color') or p['title'].get('color') or t['color']
            if contrast(colour,plate['fill'])<3:return
            lh=t.get('line_height',config['line_height'])
            # The estimate counts .56 of the size a sign; bold wide faces take more (Montserrat Bold: .6), and a line the
            # browser breaks where the estimate does not would run off the plate.
            SAFE=1.2
            pad=max(8,t['x']+l-plate['x'])
            room=min(t['x']+t['w'],plate['x']+max(plate['w'],w*.5))-(plate['x']+pad)-pad
            if room<=0:return
            size=next((z/2 for z in range(round(t['font_size']*2),math.floor(t['font_size']*1.6)-1,-1)
                       if estimate_lines(t['text'],room/SAFE,z/2)<=3),None)
            if size is None:return
            lines=estimate_lines(t['text'],room/SAFE,size)
            lo,hi=1,math.ceil(room)
            while lo<hi:
                mid=(lo+hi)//2
                if estimate_lines(t['text'],mid/SAFE,size)<=lines:hi=mid
                else:lo=mid+1
            text_h=lines*size*lh+top+b
            vpad=max(4,(plate['h']-(t['font_size']*lh+top+b))/2)
            box=[plate['x'],plate['y'],max(plate['w'],2*pad+lo),max(plate['h'],text_h+2*vpad)]
            # Pitch P8 (29.09.2026, template D sample 11): two lines with the padding of the sample reached 0.2 px into the white card
            # under the plate. The padding above and below gives way first (not below 4 px), 4 px clear of what lies under it.
            under=[o[1] for o in others if o[1]>=box[1]+plate['h']/2 and o[0]<box[0]+box[2] and o[0]+o[2]>box[0]]
            if under and box[1]+box[3]>min(under)-4:
                box[3]=max(plate['h'],min(under)-4-box[1])
                if box[3]<text_h+8:return
            if box[0]+box[2]>w or any(overlap(o,box)>0 for o in others):return
            before={k:t[k] for k in ('x','y','w','h','font_size','color')}
            t.update({k:round(v,2) for k,v in zip(('x','y','w','h'),(plate['x']+pad-l,box[1]+(box[3]-text_h)/2,lo+l+r,text_h))})
            t.update(font_size=size,color=colour)
            t['style_adjustments']=[a for a in t.get('style_adjustments',[]) if a.get('reason')!='solid-background-contrast']+[
                {'reason':'sample-title-plate','from':before,'plate':{'id':plate['id'],'from':pb,'to':[round(v,2) for v in box]}}]
            plate.update({k:round(v,2) for k,v in zip(('x','y','w','h'),box)})
            s['style_contract']['decorations'][plate['id']]=decoration_signature(plate)
        def widen_sample_title(s,p):
            """Owner decision 49 (29.09.2026), limitation 1: the title frame of a sample slide is as wide as the short title of the
            sample (template D, sample 2: 212 px); a derived title fitted into it came out at 15 px of its own 26.7, and the check of
            the rendering grew it back to the size of the other titles in five lines down the slide (pitch P6, slide 9). Below
            TITLE_WIDEN_BELOW of its own size the frame widens along the band of the title, away from the side it is aligned to,
            over space no other element takes (the text of the slide by its estimated lines), within the surface it stands on and
            not past half the slide (the logos of template D are drawn in the background picture, not in the model): the narrowest width
            at which the title takes its own size in two lines, else three; as high as those lines."""
            t=next((e for e in s['elements'] if e['id']=='title' and e['type']=='text'),None)
            if t is None or not t.get('text') or (t.get('binding') or {}).get('kind')=='placeholder':return
            own=t.get('size_own') or t['font_size']
            if t['font_size']>=own*TITLE_WIDEN_BELOW:return
            x,y,tw,th=(t[k] for k in ('x','y','w','h'));lh=t.get('line_height',config['line_height']);m=max(20,w*.035)
            tb=[x,y,tw,th]
            ground=[[e[k] for k in ('x','y','w','h')] for e in s['elements'] if e is not t and e['type']=='shape' and not e.get('ghost')
                    and e.get('opacity',1)>=.95 and e['w']*e['h']<w*h*.9 and contains([e[k] for k in ('x','y','w','h')],tb)]
            def blocks(e):
                return (e is not t and e['id'] not in ('page','disclosure') and not (e.get('template_decoration') and e['w']*e['h']>=w*h*.5)
                        and not (e['type']=='shape' and not e.get('ghost') and contains([e[k] for k in ('x','y','w','h')],tb)))
            others=[used_box(e) for e in s['elements'] if blocks(e)]
            if any(overlap(o,tb)>0 for o in others):return
            align=t.get('align','left')
            if align=='right':room=x+tw-max(m,min(x,w*.5))
            elif align=='center':room=2*min(x+tw/2-m,w-m-(x+tw/2))
            else:room=min(w-m,max(x+tw,w*.5))-x
            for k in (2,3):
                height=k*own*lh+2
                for width in range(math.ceil(tw),math.floor(room)+1,4):
                    if estimate_lines(t['text'],width/TITLE_WIDEN_SAFE,own)>k:continue
                    left=x+tw-width if align=='right' else x+tw/2-width/2 if align=='center' else x
                    box=[left,y,width,max(th,height)]
                    if any(overlap(o,box)>0 for o in others) or not all(contains(g,box,.5) for g in ground):continue
                    before={a:t[a] for a in ('x','y','w','h','font_size')}
                    t.update({a:round(v,2) for a,v in zip(('x','y','w','h'),box)})
                    t['font_size']=text_size(t['text'],box,own,lh)
                    t['style_adjustments']=t.get('style_adjustments',[])+[{'reason':'sample-title-widened','from':before,'lines':k}]
                    return
        def end(s,p,index):
            """C2 at the source, v2: the footer of a slide. The footer and slide number placeholders the layout keeps in the bottom
            band of the slide take the disclosure and the page number with the typography of the reference; otherwise both go
            into the bottom band, where no logo lies, with the smallest size of the scale up to 14 pt, else as before."""
            if carrier and title_plate and s['role']!='cover':put_title_plate(s)
            if carrier and s['role']!='cover' and p.get('source_kind')=='slide' and not any(e['id']=='titleplate' for e in s['elements']):
                widen_sample_title(s,p)
            if carrier and s['role']!='cover' and p.get('source_kind')=='slide':hug_sample_plate(s,p)
            color=readable_ink(p);m=max(20,w*.035)
            # On a picture of the reference the solid background of the composition says nothing about the colour under the
            # footer (deck 03: dark blue text on a dark blue picture): the footer takes the colour of the title of the slide,
            # which the reference sets for that picture.
            title=next((e for e in s['elements'] if e['id']=='title' and e['type']=='text'),None)
            if carrier and title is not None and text_background(s,[m,h-max(footer_h,band_h)-2,w-2*m,max(footer_h,band_h)])[1]:
                color=title['color']
            # Item 37, cause 3: the footer in the colour of a title that keeps the pair of colours of the template on its
            # full-slide picture keeps it as well (deck 19: white on light blue).
            own=[a for a in (title or {}).get('style_adjustments',[]) if a.get('reason')=='template-colour-kept' and a.get('background')=='picture'
                 and title['color']==color][:1]
            def one_surface(frame,value):
                """Owner decision 49 (29.09.2026), limitation 2 (deck 20, sample slide 6): the frame of the disclosure ran across
                the edge of the white panel of the slide (0-506 of 960 px) in the colour of the dark slide beside it, and its short
                line stood on the panel, white on white (27 of the 34 findings LOW_RENDERED_CONTRAST of the 19 rebuilt inputs); the
                model of the document took the dark slide under the frame, the check of the rendering the white under the letters.
                A left-aligned line (estimated, TITLE_WIDEN_SAFE to spare) that ends before the edge of a surface its frame crosses
                keeps off that edge (8 px), and takes a colour that reads on what then lies under it: the colour of the title, of the
                palette, else the neutral one that reads more."""
                x0,top,bw,bh=frame['box'];extent=len(value)*frame['font_size']*.56*TITLE_WIDEN_SAFE
                edges=sorted({v for e in s['elements'] if (e['type']=='shape' and not e.get('ghost') and e.get('opacity',1)>=.95
                              or e['type']=='image' and e.get('template_decoration') and not e.get('protected_asset'))
                              and e['y']<=top+.5 and e['y']+e['h']>=top+bh-.5 for v in (e['x'],e['x']+e['w']) if x0+1<v<x0+bw-1})
                if not edges or edges[0]<=x0+extent+8:return frame
                box=[x0,top,edges[0]-8-x0,bh];background,raster=text_background(s,box)
                colour=frame['color']
                if not raster and contrast(colour,background)<4.5:
                    candidates=[(title or {}).get('color')]+[palette.get(k) for k in ('dk1','lt1','dk2','lt2')]
                    colour=next((c for c in candidates if c and contrast(c,background)>=4.5),max(('FFFFFF','151515'),key=lambda c:contrast(c,background)))
                return {**frame,'box':box,'color':colour}
            def footer_text(eid,value,frame):
                given=frame
                if eid=='disclosure':frame=one_surface(frame,value)
                add_text(s,eid,value,frame)
                if frame is not given:
                    s['elements'][-1]['style_adjustments'].append({'reason':'footer-on-one-surface','from':{'box':given['box'],'color':given['color']},
                                                                   'to':{'box':[round(v,2) for v in frame['box']],'color':frame['color']}})
                if own and frame['color']==given['color']:s['elements'][-1]['style_adjustments'].extend(copy.deepcopy(own))
            service=classify_layout(layouts[p['layout_part']],w,h)['service'] if carrier and p.get('layout_part') in layouts else {}
            def bottom(ph):
                # A footer placeholder of the reference in the bottom band, if its own colour reads there (4.5:1, large text
                # 3:1; grey 12 pt footers of Office templates on white or on a coloured band do not: deck 18, deck 09, deck 16,
                # Euraxess) and no logo lies under it (the slide number placeholder of deck 18 lies on its logo).
                if not (ph and ph['box'][1]>=h*.85 and contains([0,0,w,h],ph['box'],.1) and min(placeholder_room(ph['box'],ph['typography']))>0):
                    return None
                if any(overlap(ph['box'],logo)>0 for logo in logo_boxes(s)):
                    return None
                t=ph['typography'];background,raster=text_background(s,ph['box'])
                # Holdout-next2 (deck 17): on the cover with a backplate of the sample slide (patterns.py: a picture of the slide
                # itself over the whole slide) the placeholder of the layout kept its light blue on the blue picture; there the
                # footer takes the colour of the title, which the template sets for that picture. Only the cover: the content
                # slides of deck 17 carry a picture of their slide too, and their footer placeholders read there (a first version
                # moved their page number white onto white).
                if raster and s['role']=='cover' and any(e['type']=='image' and e.get('template_decoration') and e['w']*e['h']>=w*h*.9
                                  and (e.get('source_style') or {}).get('slide') is not None for e in s['elements']):
                    return None
                large=t['size_pt']>=18 or (t.get('bold') and t['size_pt']>=14)
                return ph if raster or contrast(t['color'],background)>=(3 if large else 4.5) else None
            ftr,num=bottom(service.get('ftr')),bottom(service.get('sldNum'))
            placed=set()
            if carrier and drawn_pages(p):
                # H5: the layout draws its own slide number; a second one of the product stood beside it (deck 33).
                num=None;placed.add('page')
            for eid,value,ph in (('disclosure',brief['disclosure'],ftr),('page',str(index),num)):
                if ph is None:continue
                # The size of the placeholder itself, one line; a footer has no gap before its paragraph (written as 0, since
                # the typography read for a non-title placeholder may carry the gap of the body style).
                size=ph['typography']['size_pt']*4/3;room=placeholder_room(ph['box'],ph['typography'])
                if size>=8*4/3 and estimate_lines(value,room[0],size)==1 and size*placeholder_metrics(ph['typography'],size)[0]<=room[1]+1:
                    placeholder_text(s,eid,value,ph['box'],ph,size,p['layout_part']);s['elements'][-1]['paragraph_gap']=0;placed.add(eid)
            if placed=={'disclosure','page'}:return
            words=len(brief['disclosure'])*band_size*.56   # one line of the disclosure, as estimate_lines counts it
            # The render check keeps FOOTER_CLEAR px between the glyphs and a logo frame; a frame that ends just above the
            # band (deck 22: the band of the roundel down to 698 of 720 px) lets the band move below it while it fits the slide.
            y=h-band_h-2
            low=max((e['y']+e['h'] for e in band_obstacles(s) if e['id'] not in ('disclosure','page') and e['y']<y and e['y']+e['h']>y-FOOTER_CLEAR),default=None)
            for top in [y]+([low+FOOTER_CLEAR] if low is not None and low+FOOTER_CLEAR+band_h<=h else []):
                spans=[a for a in free_spans(s,top,top+band_h,m,FOOTER_CLEAR) if a[1]-a[0]>=40]
                right=spans[-1] if spans else None
                page_box=[right[1]-28,top,28,band_h] if right else None
                rest=[(a,b-(36 if (a,b)==right else 0)) for a,b in spans]
                left=max(rest,key=lambda a:a[1]-a[0]) if rest else None
                if left and page_box and left[1]-left[0]>=(0 if 'disclosure' in placed else words):
                    if 'disclosure' not in placed:
                        footer_text('disclosure',brief['disclosure'],{'box':[left[0],top,left[1]-left[0],band_h],'font':style['font'],'font_size':band_size,'color':color})
                    if 'page' not in placed:
                        footer_text('page',str(index),{'box':page_box,'font':style['font'],'font_size':band_size,'color':color,'align':'right'})
                    return
            if 'disclosure' not in placed:
                footer_text('disclosure',brief['disclosure'],{'box':[m,h-footer_h-2,w-2*m-35,footer_h],'font':style['font'],'font_size':footer_size,'color':color})
            if 'page' not in placed:
                footer_text('page',str(index),{'box':[w-m-28,h-footer_h-2,28,footer_h],'font':style['font'],'font_size':footer_size,'color':color,'align':'right'})
        def solid_subtitle_placeholder(s,p,frame):
            """C2 at the source: a subtitle frame of the sample on a picture of the reference (deck 06: grey text over the
            collage of photographs, unreadable on the photo under a longer subtitle) yields to an empty text placeholder of
            the cover layout on a solid area, whose own colour reads there (4.5:1, large text 3:1) and where the subtitle
            fits above the band of the footer (deck 06: the second line of the blue band). Returns the placeholder and the frame
            limited to that band, or None, which keeps the frame of the sample."""
            if not carrier or p.get('layout_part') not in layouts or not text_background(s,frame['box'])[1]:return None
            title=next((e for e in s['elements'] if e['id']=='title'),None)
            taken={(title['binding']['type'],title['binding']['idx'])} if title and title.get('binding',{}).get('kind')=='placeholder' else set()
            found=[q for q in layouts[p['layout_part']]['placeholders'] if q['box'] and not q['rotated'] and not q['vertical']
                   and q['type'] in ('body','subTitle','obj') and (q['type'],q['idx']) not in taken and contains([0,0,w,h],q['box'],.1)]
            limit=h-band_h-2-FOOTER_CLEAR   # the bottom band of end() stays for the footer
            for q in sorted(found,key=lambda q:-q['box'][2]*q['box'][3]):
                t=q['typography'];box=list(q['box']);box[3]=min(box[3],limit-box[1])
                if box[3]<=0 or min(placeholder_room(box,t))<=0 or title and overlap(q['box'],[title[k] for k in ('x','y','w','h')])>0:continue
                if any(overlap(q['box'],logo)>0 for logo in logo_boxes(s)):continue
                background,raster=text_background(s,q['box'])
                large=t['size_pt']>=18 or (t.get('bold') and t['size_pt']>=14)
                if raster or contrast(t['color'],background)<(3 if large else 4.5):continue
                if placeholder_text_size(brief['subtitle'],box,t,12) is None:continue
                return q,box
            return None
        def plate(s,q,box):
            """Item 37, cause 8 (holdout-next 25.09.2026, Rosseti): a layout placeholder with an opaque fill of its own that
            shows on its backdrop (a picture under it, or a colour it stands out from) is a plate of the design. PowerPoint
            draws the fill under the text of the slide placeholder; the preview does not, so the render check read the
            dark-blue text of the white plate of the cover against the picture and made it white: white on white."""
            if not q or not q.get('fill'):return False
            background,raster=text_background(s,box)
            return raster or contrast(q['fill'],background)>=1.5
        def plate_slot(s,p,slot):
            """A text slot of the sample cover without a letter or digit (Rosseti: five spaces) in a plate placeholder."""
            if re.search(r'\w',slot.get('source_text') or '') or not carrier or p.get('layout_part') not in layouts or not slot.get('placeholder'):return False
            return plate(s,placeholder_pair(layouts[p['layout_part']],slot['placeholder']),slot['box'])
        def free_subtitle_placeholder(s,p):
            """Item 37, cause 8: when every text slot of the sample cover is a plate, the subtitle takes the largest empty text
            placeholder of the cover layout that is no plate, clears the title and the logos and holds the subtitle (Rosseti:
            the frame of the speaker under the title, 577 x 80 px); None derives the frame from the title."""
            if not carrier or p.get('layout_part') not in layouts:return None
            title=next((e for e in s['elements'] if e['id']=='title'),None)
            taken={(title['binding']['type'],title['binding']['idx'])} if title and title.get('binding',{}).get('kind')=='placeholder' else set()
            found=[q for q in layouts[p['layout_part']]['placeholders'] if q['box'] and not q['rotated'] and not q['vertical']
                   and q['type'] in ('body','subTitle','obj') and (q['type'],q['idx']) not in taken and contains([0,0,w,h],q['box'],.1)]
            limit=h-band_h-2-FOOTER_CLEAR
            for q in sorted(found,key=lambda q:-q['box'][2]*q['box'][3]):
                t=q['typography'];box=list(q['box']);box[3]=min(box[3],limit-box[1])
                if box[3]<=0 or min(placeholder_room(box,t))<=0 or title and overlap(q['box'],[title[k] for k in ('x','y','w','h')])>0:continue
                if any(overlap(q['box'],logo)>0 for logo in logo_boxes(s)) or plate(s,q,q['box']):continue
                if placeholder_text_size(brief['subtitle'],box,t,12) is None:continue
                return q,box
            return None
        def placeholder_text(slide,eid,value,box,ph,size,layout_part,facts=None,frame=None):
            # Text in a layout placeholder (vsp.document/2): the placeholder's typography, the contrast rule of add_text.
            t=ph['typography'];moved=[]
            if carrier and eid=='title' and slide['role']!='cover':
                # Item 31 (25.09.2026): a title frame whose right part reaches under a logo of the slide ends 12 px before it,
                # at the size that fits there (deck 25: the title placeholder of the sample layout runs 36 px under the logo, and
                # PowerPoint set the last word of a long title on it).
                for lx,ly,lw,lh in logo_boxes(slide):
                    if overlap(box,[lx,ly,lw,lh])>0 and lx>box[0]+box[2]*.5 and lx-12-box[0]>=box[2]*.5:
                        moved.append({'reason':'title-off-logo','from':[round(v,2) for v in box],'to':[round(v,2) for v in (box[0],box[1],lx-12-box[0],box[3])]})
                        box=[box[0],box[1],lx-12-box[0],box[3]];size=min(size,placeholder_text_size(value,box,t,12) or size)
                # Holdout-next2 (SGD): a logo the model saw, drawn in the background picture (no object of the slide), on either
                # side of the title frame (off_logos).
                clipped=off_logos(pattern_by_id.get(slide['reference']['pattern']) or own_patterns.get(layout_part),list(box))
                if clipped!=list(box):
                    moved.append({'reason':'title-off-markup-logo','from':[round(v,2) for v in box],'to':clipped})
                    box=clipped;size=min(size,placeholder_text_size(value,box,t,12) or size)
            line,gap=placeholder_metrics(t,size)
            background,raster=text_background(slide,box)
            original_color=t['color'];ink=original_color;bold=bool(t.get('bold'));observed=[]
            if carrier and eid=='title' and slide['role']!='cover' and layout_part in layouts:
                # Item 31 (25.09.2026): the colour and weight the sample slides give their titles by hand, on top of the layouts
                # (_observed_styles: 60 % of the titles set it, 60 % of those agree; deck 14: red on 12 of 12), are part of their look.
                seen=fallback.observed.get('title',{})
                colour=fallback.rgb_of(layouts[layout_part],(seen['color'][0],seen['color'][1],seen['color'][2])) if seen.get('color') else None
                # Only where it reads (deck 18: white titles on the band of its sample slides stay the colour of the layout on
                # a white slide).
                if colour and colour.upper()!=str(original_color).upper() and (raster or contrast(colour,background)>=3):
                    observed.append({'reason':'observed-title-colour','from':original_color,'to':colour});original_color=ink=colour
                if seen.get('bold') is not None and bool(seen['bold'])!=bold:
                    observed.append({'reason':'observed-title-weight','from':bold,'to':bool(seen['bold'])});bold=bool(seen['bold'])
            # Holdout 25.09 (H2b): a pair of colours of the template itself — the colour of its placeholder on its own backdrop,
            # no shape of ours behind — stays when it is at least 2:1 (deck 07: yellow titles on blue, 2.95:1); the audit reports
            # it as the reference's own colour (C3). Lower, the backdrop is not the one the author meant, and the rule decides;
            # so it does where the backdrop is only approximated (Office Quiz Show: the mean of a gradient, preview incomplete).
            own=(ink==t['color'] and not observed and contrast(ink,background)>=2 and not slide.get('preview_notes')
                 and all(e.get('template_decoration') for e in slide['elements'] if e['type']=='shape' and contains([e[k] for k in ('x','y','w','h')],box)))
            if contrast(ink,background)<3 and not raster and not own:
                ink=max(('FFFFFF','151515'),key=lambda c:contrast(c,background))
            elif contrast(ink,background)<3 and not raster:
                observed.append({'reason':'template-colour-kept','colour':ink,'background':background,'contrast':round(contrast(ink,background),2)})
            family=placeholder_face(style,ph['type'],t['font'],frame)
            element={'id':eid,'type':'text','text':value,**dict(zip(('x','y','w','h'),[round(v,2) for v in box])),
                'font':family,'font_size':size,'color':ink,'bold':bold,
                'align':ALIGN.get(t.get('align'),'left'),'line_height':line,'fact_ids':facts or [],
                'source_style':copy.deepcopy(frame.get('source',{})) if frame else {'part':layout_part,'placeholder':[ph['type'],ph['idx']],'kind':'layout-placeholder'},
                'reference_box':copy.deepcopy(box),
                'content_role':frame.get('classification',{}) if frame else {'role':'title' if ph['type'] in TITLE_TYPES else 'body','source':'layout-placeholder'},
                'style_adjustments':moved+observed+([] if ink==original_color else [{'reason':'solid-background-contrast','from':original_color,'to':ink,'background':background}]),
                'inset':[v/EMU for v in t.get('insets_emu') or [91440,45720,91440,45720]],'anchor':t.get('anchor') if t.get('anchor') in ('t','ctr','b') else 't',
                'paragraph_gap':gap,'space_first_last':bool(t.get('space_first_last')),
                'binding':{'kind':'placeholder','type':ph['type'],'idx':ph['idx'],'raw':copy.deepcopy(ph['raw']),'typography':copy.deepcopy(t)}}
            bullet=placeholder_bullet(t)
            if bullet:element['bullet']=bullet
            # Item 37, cause 9: capitals of the placeholder (cap="all"/"small"), which the preview then draws and measures.
            if t.get('caps'):element['caps']=t['caps']
            slide['elements'].append(element)
        def framed(slide,eid,value,p,frame):
            # A5b, 1: a composition frame that is a placeholder with a partner in the layout of the composition.
            ph=placeholder_pair(layouts[p['layout_part']],frame.get('placeholder')) if carrier and p['layout_part'] in layouts else None
            adjustment=None
            if carrier and not contains([0,0,w,h],frame['box'],.1):
                # C4a, 2: a frame outside the canvas takes the frame of the same layout placeholder when that one is
                # on the canvas, otherwise it is moved onto the canvas with its size kept.
                layout=layouts.get(p['layout_part'])
                pair=ph or (placeholder_pair(layout,frame.get('placeholder')) if layout and frame.get('placeholder') else None)
                if pair is None and layout and eid=='title':pair=classify_layout(layout,w,h)['title']
                old=list(frame['box']);frame=copy.deepcopy(frame)
                if pair and contains([0,0,w,h],pair['box'],.1):
                    frame['box']=list(pair['box']);how='layout-placeholder-frame'
                else:
                    fw,fh=min(old[2],w),min(old[3],h)
                    frame['box']=[min(max(0,old[0]),w-fw),min(max(0,old[1]),h-fh),fw,fh];how='moved-onto-canvas'
                adjustment={'reason':'frame-outside-canvas','method':how,'from':old,'to':list(frame['box']),'layout_part':p['layout_part']}
            if ph is None or min(placeholder_room(frame['box'],ph['typography']))<=0:
                add_text(slide,eid,value,frame)
            else:
                size=placeholder_text_size(value,frame['box'],ph['typography'],12) or min(12,ph['typography']['size_pt']*4/3)
                placeholder_text(slide,eid,value,frame['box'],ph,size,p['layout_part'],frame=frame)
            if adjustment:slide['elements'][-1]['style_adjustments'].append(adjustment)
        def placeholder_section(section,variant,index,p=None):
            # A5b, 2: the text section in the body placeholders of a reference layout; False keeps the composition mode.
            kind=PLACEHOLDER_KINDS.get(variant);found=placeholder_candidates.get(kind);facts=section['facts']
            if kind=='content_2' and len(facts)<2:
                # Q4 (deck quality, 23.09.2026): one fact makes no columns; it takes the body across the slide rather than a
                # composition with one half-width body (template C: the right column of sample slide 24, the left half empty).
                kind='content_1';found=placeholder_candidates.get(kind)
            if not found:return False
            # Owner decision 48 («слайд 25»): the layouts of the kind the reference itself uses, of the standing of the first one
            # (sample text of the layout, missing mandatory decor), take turns along the deck; a single one stays on every slide.
            standing=lambda c:(bool(layout_sample_text(brand,c[0]['part'])),decor_missing(brand,c[0]['part']))
            # Their bodies hold the text as the first one's do: at least PEER_BODY of its width and height, body by body.
            first=[ph['box'] for ph in found[0][1]['texts'][:2]]
            holds=lambda c:len(c[1]['texts'])>=len(first) and all(ph['box'][2]>=b[2]*PEER_BODY and ph['box'][3]>=b[3]*PEER_BODY for ph,b in zip(c[1]['texts'],first))
            peers=[c for c in found if c[0]['used_by_slides']>0 and standing(c)==standing(found[0]) and holds(c)] or found[:1]
            layout,cls,lp=peers[index%len(peers)]
            # Item 31: a body layout without the mandatory decor of the sample slides yields to the composition of the section
            # when that one has it (deck 25: no layout with two bodies has the band of the sample slides).
            if decor_missing(brand,layout['part']) and p is not None and not composition_missing(p):return False
            if kind=='content_2':
                half=math.ceil(len(facts)/2);groups=[facts[:half],facts[half:]];bodies=cls['texts'][:2];ids=['body1','body2']
            else:
                groups=[facts];bodies=cls['texts'][:1];ids=['body']
            values=['\n'.join(f['text'] for f in g) for g in groups]
            sizes=[placeholder_text_size(v,ph['box'],ph['typography'],config['min_body_size_px']) for v,ph in zip(values,bodies)]
            if None in sizes:return False
            s=begin(section['id'],'text',lp)
            s['selection']={'method':'layout-placeholders-v1','layout':layout['name'],'layout_part':layout['part'],'kind':cls['kind'],
                'plausibility':cls['plausibility'],'used_by_slides':layout['used_by_slides'],'pattern':lp['id'],'candidates':len(found),
                'body_regions':[{'source':{'slide':None,'part':layout['part'],'placeholder':[ph['type'],ph['idx']],'kind':'layout-placeholder'},
                                 'classification':{'role':'body','source':'layout-placeholder'}} for ph in bodies]}
            title=cls['title']
            if min(placeholder_room(title['box'],title['typography']))>0:
                size=placeholder_text_size(section['title'],title['box'],title['typography'],12) or min(12,title['typography']['size_pt']*4/3)
                placeholder_text(s,'title',section['title'],title['box'],title,size,layout['part'])
            else:
                add_text(s,'title',section['title'],lp['title'])
            # Columns side by side keep one size.
            for eid,value,ph,g in zip(ids,values,bodies,groups):
                placeholder_text(s,eid,value,ph['box'],ph,min(sizes),layout['part'],[f['id'] for f in g])
            end(s,lp,index);return True
        def illustrate(s,p,slots):
            """Owner request of 29.09.2026 (the list variant on pictures of the template): the cut-out illustrations of the sample
            slide of composition p that the text does not cover, in their place and in the z-order of that slide (under its decor
            drawn above them: the pictogram plates of template A 7 over the spring). The PPTX carries the picture of the sample slide
            itself (bind_carrier: furniture). An ornament of the template, not a picture of a fact: image_auditor is not asked."""
            own=[i for i in p.get('illustrations',[]) if not any(overlap(q['box'],i['box'])>q['box'][2]*q['box'][3]*.2 for q in slots)]
            for n,i in enumerate(own):
                asset=ref['assets'][i['asset']]
                e={'id':f'illustration{n}','type':'image',**dict(zip(('x','y','w','h'),i['box'])),'mime':asset['mime'],'data':asset['data'],
                   'crop':i['crop'],'fact_ids':[],'template_decoration':True,'protected_asset':False,'locked':True,'ornament':True,
                   'source_style':i['source']}
                above=[k for k,x in enumerate(s['elements']) if re.fullmatch(r'decor\d+',x['id'])
                       and p['decorations'][int(x['id'][5:])]['source'].get('part')==i['source']['part']
                       and p['decorations'][int(x['id'][5:])].get('z_order',-1)>i['z_order']]
                s['elements'].insert(above[0] if above else len(s['elements']),e)
                s['style_contract']['decorations'][e['id']]=decoration_signature(e)
        def drop_row_markers(s,slots):
            """The markers of the rows a fact took with the row above it (row_groups): small decor of the sample slide beside such a
            row (the pictogram plates of template A 7 at 17 px left of the text) would stand empty next to the text of the fact above."""
            rows=[r for q in slots for r in q.get('rows_merged',[])]
            def marker(e):
                cx,cy=e['x']+e['w']/2,e['y']+e['h']/2
                return any(r[1]-8<=cy<=r[1]+r[3]+8 and (r[0]-140<=e['x']+e['w']<=r[0]+4 or r[0]+r[2]-4<=e['x']<=r[0]+r[2]+140) for r in rows)
            gone={e['id'] for e in s['elements'] if rows and re.fullmatch(r'decor\d+',e['id']) and e['w']*e['h']<=w*h*.015 and marker(e)}
            s['elements']=[e for e in s['elements'] if e['id'] not in gone]
            for k in gone:s['style_contract']['decorations'].pop(k,None)
        def card_section(section,variant,index,pick=None):
            # A7: one fact per card of a sample slide, every card cloned as a whole (docs/CARRIER_PRODUCT_2026-09-21.md).
            pick=pick or card_picks.get((variant,section['id']))
            if pick is None:return False
            sample,plan=pick;p=by_slide[sample['slide']]
            s=begin(section['id'],'text',p,skip_part=sample['part'],cloned={(sample['part'],str(x['shape'])) for x in plan['shapes']})
            loaded={f['family'] for f in style['fonts']}
            # G3: pictograms the picker chose for the facts take the places of the sample pictograms in the cards.
            icons=card_icons(sample,plan,style,section['id'])
            elements,incomplete=card_elements(sample,plan,style['carrier']['card_assets'],loaded,style['font'],w,h,icons)
            s['elements'].extend(elements)
            s['style_contract']['decorations'].update({e['id']:decoration_signature(e) for e in elements if e['type']!='text'})
            if incomplete:
                s.setdefault('preview_notes',[]).append(preview_note([],reason='sample-card-preview',details=incomplete))
            s['selection']={'method':'sample-units-v1' if sample.get('kind')=='units' else 'sample-cards-v1','sample_slide':sample['slide'],'sample_part':sample['part'],'arrangement':plan['arrangement'],
                'cards_total':plan['cards_total'],'cards_used':plan['cards_used'],'respread':plan['respread'],'labels':plan['labels'],
                'body_regions':[{'source':{'slide':sample['slide'],'part':sample['part'],'shape':t['shape'],'paragraph':t['paragraph'],'kind':'sample-card'},
                                 'classification':{'role':'body','source':'sample-card'}} for t in plan['texts'] if t['role']=='fact']}
            if icons:
                s['selection']['icons']=[{'fact':c['fact'],'icon':icons[c['container']]['icon'],'meaning':icons[c['container']]['meaning']} for c in plan['cards']]
            framed(s,'title',section['title'],p,p['title'])
            end(s,p,index);return True
        # ---- G1: a native chart of the numbers of a text section beside its facts (variant columns) ----
        def logo_boxes(s):
            """Logos of a slide as the render check sees them (quality.js qLogo): undrawable decor frames, protected assets and
            small pictures; a chart or card on them is a finding (BRAND_ASSET_OVERLAP). Text of the mandatory decor carried from
            the sample slides (item 31) is kept clear of in the same way."""
            return [[e[k] for k in ('x','y','w','h')] for e in s['elements'] if e.get('ghost') or e.get('brand_item')
                    or e['type']=='image' and (e.get('protected_asset')
                    or (e['w']<w*.35 and e['h']<h*.2 and (not e.get('template_decoration') or e['w']/max(e['h'],1)<3.5)))]
        def clear_of_logos(s,box,gap=10):
            """The largest part of a box (above, below, left or right of each logo in it) that no logo covers."""
            return clear_of(box,logo_boxes(s),gap)
        def clear_of(box,obstacles,gap=10):
            """The largest part of a box (above, below, left or right of each obstacle in it) that no obstacle covers."""
            for lx,ly,lw,lh in obstacles:
                x,y,bw,bh=box
                if overlap(box,[lx-gap,ly-gap,lw+2*gap,lh+2*gap])<=0:continue
                parts=[[x,y,bw,ly-gap-y],[x,ly+lh+gap,bw,y+bh-ly-lh-gap],[x,y,lx-gap-x,bh],[lx+lw+gap,y,x+bw-lx-lw-gap,bh]]
                parts=[p for p in parts if p[2]>0 and p[3]>0]
                if not parts:return None
                box=max(parts,key=lambda p:p[2]*p[3])
            return box
        def off_bands(p,box):
            """Item 37, cause 4 (holdout-next 25.09.2026, deck 23): an area the product lays out itself (a table, the zone of a chart
            or of cards) ends 12 px before a decorative band along the left or right edge of the slide that its composition
            draws (patterns.py keeps the frames of the composition off it the same way). Owner decisions 27-28: and a decor strip
            the scene markup saw along that edge of its sample slide (the pixels of a band the file draws in a way we do not read)."""
            for d in list((p or {}).get('decorations',[]))+[{'box':b} for b in (markup_zones(p,'decor_strip') if p else [])]:
                bx,by,bw,bh=d['box'];x,y,fw,fh=box
                if d.get('kind')=='ghost' or bh<h*.6 or bw>w*.3 or not (bx<=2 or bx+bw>=w-2) or by>=y+fh or by+bh<=y:continue
                if bx+bw>=w-2 and x<bx<x+fw and bx-12-x>0:box=[x,y,bx-12-x,fh]
                elif bx<=2 and x<bx+bw<x+fw and x+fw-bx-bw-12>0:box=[bx+bw+12,y,x+fw-bx-bw-12,fh]
            return box
        def scene_ink(s,pattern,box):
            """Item 37, cause 3 (holdout-next 25.09.2026, deck 19): on the picture that fills a slide built on a composition, the
            colour that composition sets its body text in (its title colour without body text): the text the product adds there —
            key numbers and text of cards, steps, the facts beside a chart — reads as the text of the sample slide does. The
            colours of the layout (card_look, text_colour) assume its own plain background (white: black text on light blue).
            None elsewhere, so every other slide keeps its colours."""
            if not carrier or pattern is None or not text_background(s,box)[1]:return None
            if not any(e['type']=='image' and e.get('template_decoration') and e['w']*e['h']>=w*h*.9 for e in s['elements']):return None
            colours=Counter(q['color'] for q in pattern['slots'] if q.get('classification',{}).get('role')=='body' and q.get('color'))
            return colours.most_common(1)[0][0] if colours else pattern['title'].get('color')
        def chart_colours(layout,background,count):
            """Scheme colours of the theme of the layout for the series (slices of a pie): accents that read against the
            background (3:1, WCAG 2.1, 1.4.11) and differ from each other, then the rest; the PPTX writes the scheme names."""
            theme=fallback.theme(layout);picked=[]
            options=[(n,resolve_scheme(theme,n)) for n in [f'accent{k}' for k in range(1,7)]+['tx2','tx1']]
            options=[(n,c) for n,c in options if c]
            def distinct(c):return all(max(abs(a-b) for a,b in zip(bytes.fromhex(c),bytes.fromhex(o)))>=40 for _,o in picked)
            for readable in (True,False):
                for n,c in options:
                    if len(picked)>=count:break
                    if (n,c) not in picked and distinct(c) and (not readable or contrast(c,background)>=3):picked.append((n,c))
            base=picked or [('tx1',fallback.text_colour(layout,background))]
            while len(picked)<count:picked.append(base[len(picked)%len(base)])
            return picked
        def chart_element(s,section,box,layout,family,limit,facts_colour=None):
            chart=section['chart'];background,raster=text_background(s,box)
            # On a picture of the reference the solid background is unknown: the chart text takes the colour the facts of the
            # slide have (the colour the reference gives text there), the grid is that colour half transparent, a pie has no
            # separators in the background colour.
            ink=facts_colour if raster and facts_colour else fallback.text_colour(layout,background)
            picked=chart_colours(layout,background,len(chart['categories'] if chart['kind']=='pie' else chart['series']))
            return {'id':'chart','type':'chart',**dict(zip(('x','y','w','h'),[round(v,2) for v in box])),'chart':copy.deepcopy(chart),
                'data_of':list(chart['fact_ids']),'fact_ids':[],'font':family,'font_size':min(limit,on_scale(14)),'color':ink,
                'grid_color':ink if raster else blend(ink,background,.18),'grid_alpha':.3 if raster else 1,'background':None if raster else background,
                'series_colors':[c for _,c in picked],
                'scheme_colors':[n for n,_ in picked],'lang':'en-US' if brief.get('language')=='en' else 'ru-RU',
                'source_style':{'kind':'native-chart','layout_part':layout['part'],'theme_colours':[n for n,_ in picked]}}
        def chart_split(area,facts,fits):
            """Facts beside the chart (share of the width for the facts), else above it; None when neither leaves the chart room."""
            x,y,zw,zh=area;gap=max(16,zw*.04)
            for share in (.40,.50):
                fw=zw*share
                if fits([x,y,fw,zh]) and zw-fw-gap>=w*.28:return 'side',[x,y,fw,zh],[x+fw+gap,y,zw-fw-gap,zh]
            for share in (.30,.40,.50):
                fh=zh*share
                if fits([x,y,zw,fh]) and zh-fh-gap>=h*.30:return 'stacked',[x,y,zw,fh],[x,y+fh+gap,zw,zh-fh-gap]
            return None
        def chart_section(section,index):
            facts=section['facts'];value='\n'.join(f['text'] for f in facts);floor=config['min_body_size_px']
            found=placeholder_candidates.get('content_2') or []
            if found and decor_missing(brand,found[0][0]['part']):
                # Item 31: the layout with two bodies lacks the mandatory decor of the sample slides; a zone that has it wins.
                zoned=visual_zone(section,'columns')
                if zoned is not None and not composition_missing(zoned[0]):found=[]
            if found:
                # The layout with two bodies: facts in the left one with its typography, the chart in the frame of the right one.
                layout,cls,lp=found[0];left,right=cls['texts'][:2]
                size=placeholder_text_size(value,left['box'],left['typography'],floor)
                if size is not None:
                    s=begin(section['id'],'text',lp)
                    title=cls['title']
                    if min(placeholder_room(title['box'],title['typography']))>0:
                        tsize=placeholder_text_size(section['title'],title['box'],title['typography'],12) or min(12,title['typography']['size_pt']*4/3)
                        placeholder_text(s,'title',section['title'],title['box'],title,tsize,layout['part'])
                    else:
                        add_text(s,'title',section['title'],lp['title'])
                    placeholder_text(s,'body',value,left['box'],left,size,layout['part'],[f['id'] for f in facts])
                    frame=clear_of_logos(s,list(right['box']))
                    if frame is None or frame[2]<w*.25 or frame[3]<h*.25:
                        doc['slides'].pop();return False
                    s['elements'].append(chart_element(s,section,frame,layout,s['elements'][-1]['font'],size,s['elements'][-1]['color']))
                    s['selection']={'method':'chart-v1','arrangement':'placeholders','layout':layout['name'],'layout_part':layout['part'],
                        'chart_kind':section['chart']['kind'],'body_regions':[{'source':{'slide':None,'part':layout['part'],'placeholder':[left['type'],left['idx']],
                        'kind':'layout-placeholder'},'classification':{'role':'body','source':'layout-placeholder'}}]}
                    end(s,lp,index);return True
            zoned=visual_zone(section,'columns',True)
            if zoned is None:return False
            pattern,skip,area,layout=zoned
            master_body=style['carrier']['free_text_size_pt'] or style['carrier']['masters'][layout['master']].get('body_size_pt') or 20
            preferred=max(floor,min(24*w/1280,max(14*w/1280,master_body))*4/3)
            # The facts are the paragraphs of one text element with a gap before each, as in a body placeholder.
            def need(box,size):return estimate_lines(value,box[2],size)*size*config['line_height']+size*.5*len(facts)
            def sizes(box):
                size=preferred
                while size>floor and need(box,size)>box[3]:size=max(floor,size-.5)
                return size
            def fits(box):return need(box,sizes(box))<=box[3]
            split=chart_split(area,facts,fits)
            if split is None:return False
            arrangement,text_area,chart_area=split
            s=visual_begin(section,pattern,skip,layout,'chart')
            size=sizes(text_area)
            if arrangement=='stacked':
                # The chart follows the facts rather than the share of the zone they were given.
                text_area=[text_area[0],text_area[1],text_area[2],min(text_area[3],need(text_area,size)+size*.3)]
                top=text_area[1]+text_area[3]+max(16,area[2]*.04);chart_area=[chart_area[0],top,chart_area[2],area[1]+area[3]-top]
            ink=scene_ink(s,pattern,text_area)
            add_text(s,'body',value,{'box':text_area,'font':style['font'],'font_size':size,'color':ink or fallback.text_colour(layout,text_background(s,text_area)[0]),
                'classification':{'role':'body','source':'chart-facts'},**({'scene_ink':True} if ink else {})},[f['id'] for f in facts])
            s['elements'][-1]['paragraph_gap']=round(size*.5,2)
            chart_area=clear_of_logos(s,chart_area)
            if chart_area is None or chart_area[2]<w*.25 or chart_area[3]<h*.25:
                doc['slides'].pop();return False
            s['elements'].append(chart_element(s,section,chart_area,layout,style['font'],size,s['elements'][-1]['color']))
            s['selection']={'method':'chart-v1','arrangement':arrangement,'layout':layout['name'],'layout_part':layout['part'],
                'pattern':pattern['id'] if pattern is not None else None,'chart_kind':section['chart']['kind'],
                'body_regions':[{'source':{'slide':None,'part':layout['part'],'kind':'chart-facts','box':[round(v,2) for v in text_area]},
                'classification':{'role':'body','source':'chart-facts'}}]}
            visual_end(s,pattern,layout,index)
            return True
        # ---- G2: key numbers on cards (variant split) and steps of a process (variant sequence) ----
        def metrics_section(section,index):
            """The facts of a section on cards in the card style of the template, each with its key number above it."""
            facts=section['facts'];values={m['fact_id']:m['value'] for m in section['metrics']};n=len(facts)
            zoned=visual_zone(section,'split')
            if zoned is None:return False
            pattern,skip,area,layout=zoned
            x,y,zw,zh=area;gap=max(16,zw*.025);columns=n if zw/n>=w*.2 else 2;rows=math.ceil(n/columns)
            cw=(zw-gap*(columns-1))/columns;inset=max(14,w*.014);floor=config['min_body_size_px']
            look=fallback.card_look(layout);surface=blend(look['fill'],fallback.background_rgb(layout),look['opacity'])
            master_body=style['carrier']['free_text_size_pt'] or style['carrier']['masters'][layout['master']].get('body_size_pt') or 20
            size=max(floor,min(24*w/1280,max(14*w/1280,master_body))*4/3)
            # The key number takes the largest size of the scale of the reference up to 40 pt that fits the card in one line.
            big=next((v*4/3 for v in sorted(template_tokens(style)['sizes_pt'],reverse=True) if v<=40 and all(len(values[f['id']])*v*4/3*.56<=cw-2*inset for f in facts)),None)
            if big is None or big<size*1.3:return False
            def bottom(size):return 12+size*1.35+4   # CARD_TEXT_MARGIN keeps 12 px, plus 1.35 lines from three lines on
            def need(size):return max(estimate_lines(f['text'],cw-2*inset,size) for f in facts)*size*config['line_height']
            room=(zh-gap*(rows-1))/rows
            while size>floor and inset+big*1.25+need(size)+bottom(size)>room:size=max(floor,size-.5)
            ch=inset+big*1.25+need(size)+bottom(size)
            if ch>room:return False
            ch=max(ch,min(room,ch*1.25))
            s=visual_begin(section,pattern,skip,layout,'metrics')
            if clear_of_logos(s,[x,y,zw,ch*rows+gap*(rows-1)])!=[x,y,zw,ch*rows+gap*(rows-1)]:
                doc['slides'].pop();return False
            accent=next((c for _,c in chart_colours(layout,surface,6) if contrast(c,surface)>=3),look['text'])
            # The key numbers keep the accent where the text of the scene is dark (ASCE: blue numbers over grey text on the faded
            # photograph); on a scene with light text the accent, chosen for a light card, is dark there, and the numbers take
            # the colour of the text (deck 19: white on light blue).
            # An opaque card is the backdrop of its own text (deck 21: white cards, blue text); the scene colour is for text that the
            # picture shows through (translucent cards of deck 19).
            ink=scene_ink(s,pattern,area) if look['opacity']<1 else None;scene={'scene_ink':True} if ink else {}
            light=bool(ink) and contrast(ink,'000000')>contrast(ink,'FFFFFF')
            if ink:look={**look,'text':ink}
            if light:accent=ink
            family=pattern['title']['font'] if pattern is not None and pattern['title'].get('font') in {f['family'] for f in style['fonts']} else style['font']
            for k,f in enumerate(facts):
                box=[x+(k%columns)*(cw+gap),y+(k//columns)*(ch+gap),cw,ch]
                s['elements'].append({'id':f'card{k}','type':'shape',**dict(zip(('x','y','w','h'),[round(v,2) for v in box])),'geometry':look['geometry'],
                    'fill':look['fill'],'opacity':look['opacity'],'fact_ids':[],'template_decoration':True,'locked':True,
                    'source_style':{'kind':'metric-card','layout_part':layout['part'],'card_style':copy.deepcopy(look['source'])}})
                add_text(s,f'metric{k}',values[f['id']],{'box':[box[0]+inset,box[1]+inset,box[2]-2*inset,big*1.25],'font':family,'font_size':big,'color':accent,
                    'classification':{'role':'metric','source':'metric-card'},**(scene if light else {})})
                s['elements'][-1]['metric_of']=f['id']
                add_text(s,f['id'],f['text'],{'box':[box[0]+inset,box[1]+inset+big*1.25,box[2]-2*inset,ch-inset-big*1.25-bottom(size)],'font':style['font'],
                    'font_size':size,'color':look['text'],'classification':{'role':'body','source':'metric-card'},**scene},[f['id']])
            s['selection']={'method':'metrics-v1','columns':columns,'layout':layout['name'],'layout_part':layout['part'],'pattern':pattern['id'] if pattern is not None else None,
                'body_regions':[{'source':{'slide':None,'part':layout['part'],'kind':'metric-card','box':[round(v,2) for v in area]},'classification':{'role':'body','source':'metric-card'}}]}
            visual_end(s,pattern,layout,index)
            return True
        def process_section(section,index):
            """The facts of a section as steps in a row of cards in the card style of the template, arrows between them."""
            facts=section['facts'];n=len(facts)
            zoned=visual_zone(section,'sequence')
            if zoned is None:return False
            pattern,skip,area,layout=zoned
            x,y,zw,zh=area;arrow=max(22,w*.022);gap=arrow+2*max(8,w*.008);inset=max(14,w*.014);floor=config['min_body_size_px']
            cw=(zw-gap*(n-1))/n
            if cw<w*.14:return False
            look=fallback.card_look(layout)
            master_body=style['carrier']['free_text_size_pt'] or style['carrier']['masters'][layout['master']].get('body_size_pt') or 20
            size=max(floor,min(24*w/1280,max(14*w/1280,master_body))*4/3)
            def bottom(size):return 12+size*1.35+4
            def need(size):return max(estimate_lines(f['text'],cw-2*inset,size) for f in facts)*size*config['line_height']
            while size>floor and inset+need(size)+bottom(size)>zh:size=max(floor,size-.5)
            ch=inset+need(size)+bottom(size)
            if ch>zh:return False
            ch=max(ch,min(zh,ch*1.2,h*.45))
            s=visual_begin(section,pattern,skip,layout,'process')
            if clear_of_logos(s,[x,y,zw,ch])!=[x,y,zw,ch]:
                doc['slides'].pop();return False
            background=text_background(s,area)[0]
            accent=next((c for _,c in chart_colours(layout,background,6) if contrast(c,background)>=3),look['text'])
            # An opaque card is the backdrop of its own text (deck 21: white cards, blue text); the scene colour is for text that the
            # picture shows through (translucent cards of deck 19).
            ink=scene_ink(s,pattern,area) if look['opacity']<1 else None;scene={'scene_ink':True} if ink else {}
            if ink:look={**look,'text':ink}
            for k,f in enumerate(facts):
                box=[x+k*(cw+gap),y,cw,ch]
                s['elements'].append({'id':f'card{k}','type':'shape',**dict(zip(('x','y','w','h'),[round(v,2) for v in box])),'geometry':look['geometry'],
                    'fill':look['fill'],'opacity':look['opacity'],'fact_ids':[],'template_decoration':True,'locked':True,
                    'source_style':{'kind':'process-step','layout_part':layout['part'],'card_style':copy.deepcopy(look['source'])}})
                add_text(s,f['id'],f['text'],{'box':[box[0]+inset,box[1]+inset,box[2]-2*inset,ch-inset-bottom(size)],'font':style['font'],
                    'font_size':size,'color':look['text'],'classification':{'role':'body','source':'process-step'},**scene},[f['id']])
                if k<n-1:
                    s['elements'].append({'id':f'arrow{k}','type':'shape','x':round(box[0]+cw+(gap-arrow)/2,2),'y':round(y+ch/2-arrow*.4,2),'w':round(arrow,2),'h':round(arrow*.8,2),
                        'geometry':'rightArrow','fill':accent,'opacity':1,'fact_ids':[],'template_decoration':True,'locked':True,
                        'source_style':{'kind':'process-arrow','layout_part':layout['part'],'colour':'theme-accent'}})
            s['selection']={'method':'process-v1','steps':n,'layout':layout['name'],'layout_part':layout['part'],'pattern':pattern['id'] if pattern is not None else None,
                'body_regions':[{'source':{'slide':None,'part':layout['part'],'kind':'process-step','box':[round(v,2) for v in area]},'classification':{'role':'body','source':'process-step'}}]}
            visual_end(s,pattern,layout,index)
            return True
        def visual_zone(section,variant,plain=False):
            """G1, G2: the composition a chart, key numbers or a process stand on, so the slide looks like the rest of the deck:
            a sample slide with cards (its layout, title, logos and decor; the zone of its cards, the cards themselves are not
            cloned), a layout with body placeholders (the zone of its bodies), the composition of the section; the one of the
            section unless another gives clearly more room. Otherwise a layout with a title and a free zone."""
            def cut_across(panel,zone):
                # The panel covers the whole height of the zone and between a tenth and nine tenths of its width.
                top,bottom=max(panel[1],zone[1]),min(panel[1]+panel[3],zone[1]+zone[3])
                left,right=max(panel[0],zone[0]),min(panel[0]+panel[2],zone[0]+zone[2])
                return bottom-top>=zone[3]*.9 and zone[2]*.1<right-left<zone[2]*.9
            def zone_of(p,skip,boxes,title):
                x0,y0=min(b[0] for b in boxes),min(b[1] for b in boxes);x1,y1=max(b[0]+b[2] for b in boxes),max(b[1]+b[3] for b in boxes)
                top=max(y0,title[1]+title[3]+h*.02);zone=[x0,top,x1-x0,min(y1,h*.9)-top]
                # Decor of a composition inside that zone would sit under the new objects (the cards of a sample are skipped).
                inside=any(d.get('purpose')!='background' and d['source'].get('part')!=skip and overlap(d['box'],zone)>0 for d in p['decorations'])
                # Session 15 (owner decision 31, deck 11): a zone for a chart (plain: it has no fill of its own, unlike cards)
                # cut by the vertical edge of a filled panel of the composition (the blue right half of slide 12, over the whole
                # height of the zone) set the chart half on white, half on blue, its labels in the colour of the white. Only a
                # vertical edge: the zone refused for a band across its bottom (deck 15, brief visuals) sent the charts onto the
                # patterned picture of another composition (regression hr15).
                inside=inside or plain and any(d.get('purpose')=='background' and d.get('kind')=='shape' and d.get('fill') and cut_across(d['box'],zone)
                                               for d in p['decorations'])
                # Item 31: the zone keeps off the decor a slide on this composition takes from the sample slides.
                zone=clear_of_brand(p,zone) if not inside else zone
                zone=off_bands(p,zone) if zone is not None else None
                return None if inside or zone is None or zone[2]<w*.45 or zone[3]<h*.35 or p['layout_part'] not in layouts else zone
            # (composition, part whose shapes are skipped, boxes of the zone, title placeholder of a layout or None)
            options=[]
            pick=card_picks.get((variant,section['id']))
            for sample in ([pick[0]] if pick and pick[0] in zone_samples else [])+[c for c in zone_samples if not pick or c is not pick[0]]:
                options.append((by_slide[sample['slide']],sample['part'],[c['box'] for c in sample['cards']],None))
            # A layout with body placeholders: its own title placeholder, as placeholder_section places it (the title frame of
            # the composition of a layout may cover the whole slide, deck 03).
            for kind in ('content_2','content_1'):
                for layout,cls,lp in placeholder_candidates.get(kind,[])[:1]:
                    if cls['title']:options.append((lp,None,[q['box'] for q in cls['texts']],cls['title']))
            if picks[section['id']][0] is not None:
                # A section on the allowed region of a sample text (G2, several facts) offers that whole region, not its cells.
                p,slots=picks[section['id']];options.append((p,None,[s['source'].get('allowed_box') or s['box'] for s in slots],None))
            zoned=[(p,skip,zone,ph) for p,skip,boxes,ph in options for zone in [zone_of(p,skip,boxes,ph['box'] if ph else p['title']['box'])] if zone]
            # Item 35, H3: zones on layouts that draw text of their sample slide only when no other zone is left.
            zoned=[z for z in zoned if not layout_sample_text(brand,z[0].get('layout_part'))] or ([] if clean_layout else zoned)
            # Item 31: zones on layouts with the mandatory decor of the sample slides first.
            zoned=[z for z in zoned if not composition_missing(z[0])] or zoned
            if zoned:
                best=max(z[2][2]*z[2][3] for z in zoned)
                pattern,skip,area,ph=next(z for z in zoned if z[2][2]*z[2][3]>=best*.85)
                return pattern,(skip,ph),area,layouts[pattern['layout_part']]
            layout=fallback.pick('visual','title_only','content_1','content_2','content_n') or fallback.cover_layout()
            zone,title=off_bands(own_patterns.get(layout['part']),fallback.zone(layout)),fallback.content_title(layout)
            zone=clear_of_brand(own_patterns.get(layout['part']) or bare_pattern(layout),zone) or zone
            # A layout whose title frame reaches into the free zone (a quote layout with a title over the whole slide) takes
            # no chart or cards: the slide is built as the other slides of the variant.
            if title and overlap(title['box'],zone)>0:return None
            if carrier and layout['part'] not in own_patterns:
                # Item 37, cause 3 (holdout-next 25.09.2026, deck 19): the chart would stand on a plain layout without a
                # composition of its own — white, without the background picture, the logo and the rule of the only content
                # sample slide. When a content sample slide has such decor (a picture over the slide or furniture), the free
                # area of that slide under its title — from the left edge of its text to the same margin on the right, down to
                # the first shape of the slide below — is the zone instead, on that slide's scene. Only here: where no zone was
                # found and the section keeps its text (deck 38 split), nothing changes (a first version did, and the deck-wide
                # title size of deck 38 fell).
                found=[]
                for p in content:
                    t=p['title']['box']
                    if p.get('source_kind')!='slide' or t[1]>h*.28 or p['layout_part'] not in layouts:continue
                    if not any(d['purpose']=='furniture' or d['box'][2]*d['box'][3]>=w*h*.9 for d in p['decorations']):continue
                    x0=max(w*.03,min([t[0]]+[q['box'][0] for q in p['slots']]));x1=w-x0;top=t[1]+t[3];bottom=h*.9
                    # A rule just under the title (the white line of deck 19) starts the zone below it; shapes lower end it.
                    for d in p['decorations']:
                        b=d['box']
                        if b[3]<6 and top-6<=b[1]<=top+h*.08:top=max(top,b[1]+b[3]+8)
                    for d in p['decorations']:
                        b=d['box']
                        if b[2]*b[3]<w*h*.9 and b[1]>=top and b[0]<x1 and b[0]+b[2]>x0:bottom=min(bottom,b[1]-12)
                    if bottom-top<h*.35 or x1-x0<w*.45:continue
                    area=zone_of(p,None,[[x0,top,x1-x0,bottom-top]],t)
                    if area:found.append((p,area))
                if found:
                    p,area=max(found,key=lambda f:f[1][2]*f[1][3])
                    return p,(None,None),area,layouts[p['layout_part']]
            return None,(None,None),zone,layout
        def visual_begin(section,pattern,source,layout,mode):
            skip,ph=source
            if pattern is not None:
                s=begin(section['id'],'text',pattern,skip_part=skip)
                if ph is not None and min(placeholder_room(ph['box'],ph['typography']))>0:
                    size=placeholder_text_size(section['title'],ph['box'],ph['typography'],12) or min(12,ph['typography']['size_pt']*4/3)
                    placeholder_text(s,'title',section['title'],ph['box'],ph,size,layout['part'])
                else:
                    framed(s,'title',section['title'],pattern,pattern['title'])
            else:
                s,cls=fallback_begin(section['id'],'text',layout,mode,mode)
                fallback_title(s,layout,section['title'])
            return s
        def visual_end(s,pattern,layout,index):
            if pattern is not None:end(s,pattern,index)
            else:fallback_end(s,layout,index)
        # ---- C4a: carrier fallback slides (spikes/template_carrier/carrier.py on the reference layouts) ----
        def bare_pattern(layout):
            # No composition of the layout: no decor in the model; PowerPoint draws the layout on the carrier.
            # Owner decision 46 (28.09.2026): except the background picture of its master, which PowerPoint draws under a
            # layout without a background of its own (template D: the cover of the pitch on the purple picture of the master).
            # The preview drew white there, and the title took a dark colour for white — dark on purple in the PPTX. The
            # picture is the one the compositions of the other layouts of that master carry (bound as inherited decor, not
            # written to the PPTX); the colours of the texts then follow it as on those slides.
            decorations=[]
            if layout.get('own_background') is False:
                decorations=next(([copy.deepcopy(d)] for p in patterns for d in p.get('decorations',[])
                                  if (d.get('source') or {}).get('kind')=='background-fill' and d['source'].get('part')==layout['master']
                                  and d.get('asset') in ref['assets']),[])
            return {'id':None,'source_slide':None,'source_part':layout['part'],'layout_part':layout['part'],'background_origin':'fallback',
                    'family':'carrier-fallback','source_kind':'layout','background':fallback.background_rgb(layout),'decorations':decorations,'compatibility':{}}
        def fallback_begin(sid,role,layout,reason,mode,**extra):
            own=own_patterns.get(layout['part'])
            if own is not None:
                s=begin(sid,role,own)
            else:
                s=begin(sid,role,bare_pattern(layout))
                s['preview_notes']=[preview_note([],reason='layout-without-composition',layout_part=layout['part'])]
            cls=fallback.classes[layout['part']]
            s['selection']={'method':'carrier-fallback-v1','reason':reason,'mode':mode,'layout':layout['name'],'layout_part':layout['part'],
                'kind':cls['kind'],'plausibility':cls['plausibility'],'used_by_slides':layout['used_by_slides'],
                'pattern':own['id'] if own is not None else None,**extra}
            return s,cls
        def fallback_title(s,layout,value,eid='title',ph=None):
            ph=ph or fallback.content_title(layout)
            if ph and min(placeholder_room(ph['box'],ph['typography']))>0:
                size=placeholder_text_size(value,ph['box'],ph['typography'],12) or min(12,ph['typography']['size_pt']*4/3)
                placeholder_text(s,eid,value,ph['box'],ph,size,layout['part'])
                return ph['box']
            box=[w*.06,h*.06,w*.88,h*.12]
            add_text(s,eid,value,{'box':box,'font':style['font'],'font_size':28*4/3,'bold':True,'color':fallback.text_colour(layout)})
            return box
        def fallback_end(s,layout,index):
            if any(e.get('brand_item') for e in s['elements']):
                # Item 31: decor carried from the sample slides lies in the band of the footer (deck 18: the logo on its band);
                # the footer keeps off it as the footer of a slide on a composition does (end).
                ink=fallback.text_colour(layout)
                end(s,own_patterns.get(layout['part']) or {'title':{'color':ink},'ink':ink,'background':s['background'],'layout_part':layout['part']},index)
                return
            x,y,sw,sh=fallback.free_bottom_span(layout);ink=fallback.text_colour(layout)
            # Owner decision 46 (28.09.2026): on the background picture of the master (bare_pattern) the footer takes the colour
            # of the title of the slide, as end() does on a picture (template D: the black disclosure of the cover on purple).
            title=next((e for e in s['elements'] if e['id']=='title' and e['type']=='text'),None)
            if title is not None and text_background(s,[x,y,max(1,sw-40),sh])[1]:ink=title['color']
            add_text(s,'disclosure',brief['disclosure'],{'box':[x,y,max(1,sw-40),sh],'font':style['font'],'font_size':footer_size,'color':ink})
            # H5: no second slide number where the layout draws its own.
            if not drawn_pages({'layout_part':layout['part']}):
                add_text(s,'page',str(index),{'box':[x+sw-28,y,28,sh],'font':style['font'],'font_size':footer_size,'color':ink,'align':'right'})
        def fallback_cover(reason):
            layout=fallback.cover_layout()
            s,cls=fallback_begin('cover','cover',layout,reason,'cover-placeholders')
            fallback_title(s,layout,brief['title'],ph=cls['title'])
            subtitles=sorted(cls['texts']+cls['small_texts'],key=lambda q:-(q['box'][2]*q['box'][3]))
            if cls['title']:subtitles=[q for q in subtitles if q['box'][1]>=cls['title']['box'][1]] or subtitles
            subtitles=[q for q in subtitles if min(placeholder_room(q['box'],q['typography']))>0]
            if subtitles:
                q=subtitles[0];size=placeholder_text_size(brief['subtitle'],q['box'],q['typography'],12) or min(12,q['typography']['size_pt']*4/3)
                placeholder_text(s,'subtitle',brief['subtitle'],q['box'],q,size,layout['part'])
            else:
                tb=next(e for e in s['elements'] if e['id']=='title')
                top=tb['y']+tb['h']+12
                add_text(s,'subtitle',brief['subtitle'],{'box':[tb['x'],min(top,h*.78),tb['w'],h*.12],'font':style['font'],'font_size':on_scale(18),'color':fallback.text_colour(layout)})
            fallback_end(s,layout,1)
        def fallback_cards(section,index,reason,arrangement):
            # Native cards in the free zone of a layout without a usable body (native_cards of the spike).
            layout=fallback.pick('native','title_only','content_1','content_2','content_n','section') or fallback.cover_layout()
            s,cls=fallback_begin(section['id'],'text',layout,reason,'native-cards',arrangement=arrangement)
            fallback_title(s,layout,section['title'])
            x,y,zw,zh=off_bands(own_patterns.get(layout['part']),clear_of_brand(own_patterns.get(layout['part']) or bare_pattern(layout),fallback.zone(layout)) or fallback.zone(layout));look=fallback.card_look(layout)
            facts=section['facts'];n=len(facts);gap,inset=w*.015,max(12,w*.016)
            master_body=style['carrier']['free_text_size_pt'] or style['carrier']['masters'][layout['master']].get('body_size_pt') or 20
            scale=w/1280;floor=config['min_body_size_px'];line=config['line_height']
            def bottom(size):return 12+size*1.35+4   # CARD_TEXT_MARGIN keeps 12 px, plus 1.35 lines from three lines on
            def need(value,box,size):return estimate_lines(value,box[2]-2*inset,size)*size*line
            # Holdout-next2 26.09.2026 (deck 31, deck 35, brief visuals): four stacked cards in a zone of 231 px left each card
            # 40-60 px, less than its insets and one line, and a text of negative height refused the whole deck. An
            # arrangement whose cards hold less than one line at the floor size gives way to columns, then to one card.
            for arrangement in [arrangement]+[a for a in ('columns','single') if a!=arrangement]:
                if arrangement=='single':boxes,groups=[[x,y,zw,zh]],[facts]
                elif arrangement=='columns':
                    cw=(zw-gap*(n-1))/n;boxes,groups=[[x+i*(cw+gap),y,cw,zh] for i in range(n)],[[f] for f in facts]
                else:
                    ch=(zh-gap*(n-1))/n;boxes,groups=[[x,y+i*(ch+gap),zw,ch] for i in range(n)],[[f] for f in facts]
                values=['\n'.join(f['text'] for f in g) for g in groups]
                size=max(floor,min(24*scale,max(14*scale,master_body))*4/3)
                while size>floor and any(need(v,b,size)>b[3]-inset-bottom(size) for v,b in zip(values,boxes)):size=max(floor,size-.5)
                if min(b[3]-inset-bottom(size) for b in boxes)>=size*line:break
            s['selection']['arrangement']=arrangement
            # Do not leave a tall empty card under two short lines.
            for box,value in zip(boxes,values):
                box[3]=min(box[3],max((need(value,box,size)+inset+bottom(size))*1.25,h*.16))
            if arrangement=='columns':
                tallest=max(b[3] for b in boxes)
                for box in boxes:box[3]=tallest
            elif arrangement=='cards':
                cursor=y
                for box in boxes:box[1],cursor=cursor,cursor+box[3]+gap
            for k,(box,value,g) in enumerate(zip(boxes,values,groups)):
                s['elements'].append({'id':f'card{k}','type':'shape',**dict(zip(('x','y','w','h'),[round(v,2) for v in box])),'geometry':look['geometry'],
                    'fill':look['fill'],'opacity':look['opacity'],'fact_ids':[],'template_decoration':True,'locked':True,
                    'source_style':{'kind':'carrier-fallback-card','layout_part':layout['part'],'card_style':copy.deepcopy(look['source'])}})
                text_box=[box[0]+inset,box[1]+inset,box[2]-2*inset,max(size*line,box[3]-inset-bottom(size))]
                eid=g[0]['id'] if len(g)==1 else 'body'
                add_text(s,eid,value,{'box':text_box,'font':style['font'],'font_size':size,'color':look['text'],
                    'classification':{'role':'body','source':'carrier-fallback-card'}},[f['id'] for f in g])
            s['selection']['body_regions']=[{'source':{'slide':None,'part':layout['part'],'kind':'carrier-fallback-card','box':[round(v,2) for v in b]},
                                             'classification':{'role':'body','source':'carrier-fallback-card'}} for b in boxes]
            fallback_end(s,layout,index)
        def fallback_text(section,variant,index,reason):
            # A body placeholder of the reference (placeholder mode of A5b), otherwise native cards.
            facts=section['facts']
            for kind in FALLBACK_BODY_KINDS[variant]:
                found=[l for l in fallback.ranked(kind) if fallback.classes[l['part']]['plausibility']>=3]
                if fallback.foreign(found):continue
                layout=fallback.pick('text-'+kind,kind,candidates=found) if found else None
                if layout is None:continue
                cls=fallback.classes[layout['part']]
                if kind=='content_2' and variant=='columns' and len(facts)>=2:
                    half=math.ceil(len(facts)/2);groups=[facts[:half],facts[half:]];bodies=cls['texts'][:2];ids=['body1','body2']
                else:
                    groups=[facts];bodies=cls['texts'][:1];ids=['body']
                values=['\n'.join(f['text'] for f in g) for g in groups]
                sizes=[placeholder_text_size(v,ph['box'],ph['typography'],config['min_body_size_px']) for v,ph in zip(values,bodies)]
                if None in sizes:continue
                s,cls=fallback_begin(section['id'],'text',layout,reason,'placeholders',candidates=len(found),
                    body_regions=[{'source':{'slide':None,'part':layout['part'],'placeholder':[ph['type'],ph['idx']],'kind':'layout-placeholder'},
                                   'classification':{'role':'body','source':'layout-placeholder'}} for ph in bodies])
                fallback_title(s,layout,section['title'],ph=cls['title'])
                for eid,value,ph,g in zip(ids,values,bodies,groups):
                    placeholder_text(s,eid,value,ph['box'],ph,min(sizes),layout['part'],[f['id'] for f in g])
                fallback_end(s,layout,index);return
            fallback_cards(section,index,reason,FALLBACK_ARRANGEMENTS[variant])
        def fallback_table(section,index,reason):
            layout=fallback.pick('table','title_only','content_1','content_2','content_n') or fallback.cover_layout()
            s,cls=fallback_begin(section['id'],'table',layout,reason,'native-table')
            fallback_title(s,layout,section['title'])
            area=off_bands(own_patterns.get(layout['part']),list(cls['texts'][0]['box']) if cls['kind']=='content_1' else fallback.zone(layout))
            rows=[section['columns']]+[f['cells'] for f in section['facts']]
            size=min(20,style['body_size']);note=h*.07+10 if section.get('note') else 0
            box=[area[0],area[1],area[2],max(1,min(area[3]-note,max(len(rows)*(size*config['line_height']+16)*1.2,len(rows)*h*.07)))]
            background=s['background'];header=fallback.brand_fill(layout)
            ink=fallback.text_colour(layout);header_ink=fallback.text_colour(layout,header)
            s['elements'].append({'id':'table','type':'table',**dict(zip(('x','y','w','h'),[round(v,2) for v in box])),
                'rows':rows,'fact_ids':[f['id'] for f in section['facts']],
                'font':style['font'],'font_size':size,'color':ink,'accent':header_ink,
                'header_fill':header,'row_fills':[background,background],'border_color':ink,
                'column_widths':[.5]+[.5/(len(section['columns'])-1)]*(len(section['columns'])-1),
                'source_style':{'kind':'carrier-fallback-table','layout_part':layout['part'],'area':[round(v,2) for v in area]}})
            if section.get('note'):
                add_text(s,'note',section['note'],{'box':[box[0],box[1]+box[3]+10,box[2],h*.07],'font':style['font'],'font_size':on_scale(10.5),'color':ink})
            fallback_end(s,layout,index)
        if covers:
            p=covers[0];cover=begin('cover','cover',p)
            framed(cover,'title',brief['title'],p,p['title'])
            # Item 37, cause 8: a slot of the sample cover that is a plate (plate_slot) is no frame for the subtitle.
            slots=[q for q in p['slots'] if not plate_slot(cover,p,q)]
            free=free_subtitle_placeholder(cover,p) if p['slots'] and not slots else None
            if slots:
                sub=copy.deepcopy(max(slots,key=lambda s:s['box'][2]))
                # Extend the source subtitle region only into unused vertical space.
                extended=min(max(sub['box'][3],h*.09),h*.90-sub['box'][1])
                if extended>0:
                    sub['box'][3]=extended
                else:
                    # Holdout 25.09 (analysis/style-experiments/20260924-holdout, deck 34): the subtitle of the sample starts
                    # below 90 % of the slide (487 px of 540), the extension gave a height of -1 px and the model refused
                    # the whole deck. Such a subtitle keeps its own height, moved up only as far as it leaves the canvas.
                    sub['box'][3]=max(1,sub['box'][3])
                    sub['box'][1]=max(0,min(sub['box'][1],h-sub['box'][3]))
            else:
                tb=p['title']['box']
                # C4a, 2: a subtitle derived from the title follows the title frame moved onto the canvas.
                moved=[a['to'] for a in cover['elements'][-1]['style_adjustments'] if a['reason']=='frame-outside-canvas']
                if moved:tb=moved[-1]
                sub={'box':[tb[0],tb[1]+tb[3]+12,tb[2],h*.12], 'font':style['font'],'font_size':on_scale(18),'color':readable_ink(p)}
            solid=free or (solid_subtitle_placeholder(cover,p,sub) if slots else None)
            if solid is None:
                framed(cover,'subtitle',brief['subtitle'],p,sub)
            else:
                q,box=solid;size=placeholder_text_size(brief['subtitle'],box,q['typography'],12)
                if q['typography'].get('anchor','t')=='t':
                    # Top anchor: the frame ends below its lines and leaves the rest of the band to the footer.
                    room=placeholder_room(box,q['typography']);line,_=placeholder_metrics(q['typography'],size)
                    top,bottom=[v/EMU for v in (q['typography'].get('insets_emu') or [91440,45720,91440,45720])[1::2]]
                    box[3]=min(box[3],top+bottom+estimate_lines(brief['subtitle'],room[0],size)*size*line+1)
                placeholder_text(cover,'subtitle',brief['subtitle'],box,q,size,p['layout_part'])
                cover['elements'][-1]['style_adjustments'].append({'reason':'subtitle-frame-not-a-plate' if free else 'subtitle-frame-on-picture',
                    'from':copy.deepcopy(sub['box']),'to':box,'placeholder':[q['type'],q['idx']],'layout_part':p['layout_part']})
            end(cover,p,1)
        else:
            fallback_cover(global_reason)
        for index,section in enumerate(brief['sections'],2):
            p,slots=picks[section['id']]
            # G1 (docs/CHARTS.md): the columns variant compares, so a section with a chart shows it beside its facts there.
            if carrier and variant=='columns' and section['kind']=='text' and section.get('chart') and chart_section(section,index):continue
            # G2: blocks of the sample show key numbers on cards; the list shows steps of a process in a row with arrows.
            if carrier and variant=='split' and section['kind']=='text' and section.get('metrics') and metrics_section(section,index):continue
            if carrier and variant=='sequence' and section['kind']=='text' and section.get('process') and process_section(section,index):continue
            if forced and section['kind']=='text':
                # C4a, 3: variants with equal compositions are rebuilt with different card arrangements.
                fallback_cards(section,index,DIVERSITY_REASON,forced);continue
            # Owner request of 29.09.2026: the list variant takes the composition with a picture chosen for the section.
            pictured=pictured_choice.get(section['id']) if variant=='sequence' and section['kind']=='text' else None
            if pictured and pictured[0]=='pattern':p,slots=pictured[1]
            elif section['kind']=='text' and card_section(section,variant,index,pictured[1] if pictured else None):continue
            if p is None:
                (fallback_text(section,variant,index,slots) if section['kind']=='text' else fallback_table(section,index,slots));continue
            if section['kind']=='text' and placeholders and not pictured and placeholder_section(section,variant,index,p):continue
            s=begin(section['id'],section['kind'],p)
            if carrier and variant=='sequence' and section['kind']=='text':illustrate(s,p,slots);drop_row_markers(s,slots)
            s['selection']={'method':'roles-capacity-v1','pattern_intent':p.get('intent'),'body_regions':[{'source':slot['source'],'classification':slot['classification']} for slot in slots]}
            if section['kind']=='text':
                surfaces=[]
                for slot in slots:
                    parents=[slot['surface_override']] if slot.get('surface_override') else [f for f in p['surfaces'] if contains(f['box'],slot['box']) and f['box'][2]*f['box'][3]<=w*h*.7]
                    if slot.get('fill'):parents.append(slot)
                    if parents:
                        surface=min(parents,key=lambda f:f['box'][2]*f['box'][3])
                        if surface not in surfaces:surfaces.append(surface)
                for n,f in enumerate(surfaces):
                    s['elements'].append({'id':f'surface{n}','type':'shape',**dict(zip(('x','y','w','h'),f['box'])),
                        'geometry':f['geometry'] if f['geometry'] in ('rect','roundRect') else 'rect','fill':f['fill'],
                        **({'adj':f['adj']} if f.get('adj') is not None else {}),
                        'fact_ids':[],'template_decoration':True,'locked':True,'source_style':f['source']})
            framed(s,'title',section['title'],p,p['title'])
            if section['kind']=='text' and len(slots)==1 and slots[0].get('list'):
                # G2 for several facts: the facts are the paragraphs of one text with a gap before each, as in a body placeholder.
                sf=copy.deepcopy(slots[0]);sf['size_own']=sf['font_size'];sf['font_size']=max(config['min_body_size_px'],min(28,sf['font_size']))
                add_text(s,'body','\n'.join(f['text'] for f in section['facts']),sf,[f['id'] for f in section['facts']])
                s['elements'][-1]['paragraph_gap']=round(s['elements'][-1]['font_size']*.5,2)
            elif section['kind']=='text':
                for slot,f in zip(slots,section['facts']):
                    sf=copy.deepcopy(slot);sf['size_own']=sf['font_size'];sf['font_size']=max(config['min_body_size_px'],min(28,sf['font_size']))
                    if sf.get('fill'):
                        l,t,r,b=sf.get('inset',[10,5,10,5]);x,y,sw,sh=sf['box']
                        sf['box']=[x+l,y+t,sw-l-r,sh-t-b]
                    add_text(s,f['id'],f['text'],sf,[f['id']])
            else:
                native=p['native_table'];m=max(w*.05,p['title']['box'][0])
                title_bottom=p['title']['box'][1]+p['title']['box'][3]
                box=copy.deepcopy(native['box']) if native else [m,max(h*.30,title_bottom+18),w-2*m,h*.40]
                if box[2]<w*.7:
                    box[0]=m;box[2]=w-2*m
                box[3]=max(box[3],(len(section['facts'])+1)*h*.10)
                box[1]=max(box[1],title_bottom+12);box[3]=min(box[3],h*.79-box[1])
                box=off_bands(p,box)
                bg=native.get('body_fill') if native else None;bg=bg or p['background']
                header=native.get('header_fill') if native else None;header=header or style['accent']
                foreground=native.get('body_color') if native else None;foreground=foreground or ('FFFFFF' if contrast('FFFFFF',bg)>contrast('151515',bg) else '151515')
                header_ink=native.get('header_color') if native else None;header_ink=header_ink or ('FFFFFF' if contrast('FFFFFF',header)>contrast('151515',header) else '151515')
                s['elements'].append({'id':'table','type':'table',**dict(zip(('x','y','w','h'),box)),
                    'rows':[section['columns']]+[f['cells'] for f in section['facts']], 'fact_ids':[f['id'] for f in section['facts']],
                    'font':style['font'],'font_size':min(20,style['body_size']),'color':foreground,'accent':header_ink,
                    'header_fill':header,'row_fills':[bg,bg],'border_color':p['ink'],
                    'column_widths':[.5]+[.5/(len(section['columns'])-1)]*(len(section['columns'])-1),
                    'source_style':native['source'] if native else {'slide':p['source_slide'],'derivation':'table-area-from-title-and-margins'}})
                if section.get('note'):
                    note_box=[box[0],box[1]+box[3]+10,box[2],h*.07]
                    # On a picture of the reference the solid background says nothing about the colour under the note (deck 03:
                    # dark blue on a dark blue picture): as the footer (end), the note takes the colour of the title of the slide.
                    title=next((e for e in s['elements'] if e['id']=='title' and e['type']=='text'),None)
                    ink=title['color'] if carrier and title is not None and text_background(s,note_box)[1] else readable_ink(p)
                    add_text(s,'note',section['note'],{'box':note_box,'font':style['font'],'font_size':on_scale(10.5),'color':ink})
            end(s,p,index)
        return doc

    pictured_choice={}
    def choose_pictured(all_picks):
        """Owner request of 29.09.2026: per text section of the list variant whose slide shows no picture of the template, ('card',
        (sample, plan)) of card_pictures or ('pattern', (composition, slots)) of a choice on pictures after the other variants,
        whichever shows a picture and whose sample slide the variant used less so far (a card set first). A slide that already
        shows one keeps its composition (template B: the decor of every slide; a new choice there changed 8 of 11 slides and
        gave 12 warnings of contrast, rebuild set2 of 29.09.2026); a section without such a composition keeps its choice."""
        pictured_choice.clear()
        if not carrier or 'sequence' not in config['variants']:return
        vi=config['variants'].index('sequence')
        shown={x['id'] for x in compose(vi,'sequence',all_picks[vi],carrier)['slides']
               if any(e['type']=='image' and e.get('template_decoration') and not e.get('icon') and PICTURE_AREA[0]<=e['w']*e['h']/(w*h)<PICTURE_AREA[1]
                      for e in x['elements'])}
        for k in [k for k in variant_usage if k[0]=='sequence']:del variant_usage[k]
        used=Counter();heavy['left']=HEAVY_BUDGET;repeats=[]
        for section in brief['sections']:
            if section['kind']!='text' or section['id'] in shown:continue
            options=[]
            # The compositions the other variants may show for the section: their choices of compositions and of card sets.
            taken={composition_of(*picks[section['id']]) for v,picks in zip(config['variants'],all_picks) if v!='sequence' and picks[section['id']][0] is not None}
            taken|={card_signatures[(v,section['id'])] for v in config['variants'] if v!='sequence' and (v,section['id']) in card_signatures}
            card=card_pictures.get(section['id'])
            if card and card_heavy(*card)<=heavy['left']:
                options.append((used[card[0]['slide']],0,('card',card),card[0]['slide'],card_heavy(*card),picture_signatures[section['id']] in taken))
            p,slots=select(section,'sequence',vi,pictures=True,taken=taken)
            if p is not None and pattern_pictures(p,slots,budget=True):
                where=p.get('source_slide') or p['id']
                options.append((used[where],1,('pattern',(p,slots)),where,heavy_bytes(p,slots),composition_of(p,slots) in taken))
            if options:
                best=min(options,key=lambda o:o[:2]);pictured_choice[section['id']]=best[2];used[best[3]]+=1;heavy['left']-=best[4]
                if best[5]:repeats.append(section['id'])
        # The variants are compared as whole documents (diversity.audit_diversity, a coarse signature: any row of three cards is
        # one composition). A list variant that repeats another variant on every slide sent the columns to the fallback cards
        # (test_carrier_composer, template A); forbidding every repeat instead left only the spring of template A 7, on 7 of 11 slides.
        # So the choices that repeat a composition of another variant for their section go only when the documents stop differing.
        if repeats and not audit_diversity([compose(i,v,all_picks[i],carrier) for i,v in enumerate(config['variants'])])['passed']:
            for sid in repeats:pictured_choice.pop(sid,None)
    # Selection runs for every section in the order of the composition mode, so the
    # split variant and every fallback get exactly the compositions they got before A5b.
    all_picks=[]
    for vi,variant in enumerate(config['variants']):
        picks={section['id']:select(section,variant,vi) for section in brief['sections']}
        all_picks.append(picks)
    choose_pictured(all_picks)
    for vi,variant in enumerate(config['variants']):
        docs.append(compose(vi,variant,all_picks[vi],carrier))
    if carrier and repeat_columns['done'] and not audit_diversity(docs)['passed']:
        # Owner decision 31: where the repeats of columns made the variants stop differing, every pick is made again without
        # them (selection is deterministic, so sequence and split get the same compositions).
        repeat_columns.update(on=False,done=[]);selected_per_section.clear();variant_usage.clear();del docs[:];all_picks=[]
        for vi,variant in enumerate(config['variants']):
            picks={section['id']:select(section,variant,vi) for section in brief['sections']}
            all_picks.append(picks)
        choose_pictured(all_picks)
        for vi,variant in enumerate(config['variants']):
            docs.append(compose(vi,variant,all_picks[vi],carrier))
    if carrier and not audit_diversity(docs)['passed']:
        # Three distinct compositions stay mandatory: columns fall back to the composition mode.
        docs=[compose(vi,variant,all_picks[vi],variant!='columns') for vi,variant in enumerate(config['variants'])]
    if carrier and not audit_diversity(docs)['passed']:
        # C4a, 3: the duplicates are rebuilt by the fallback with different card arrangements of the spike
        # until the three variants differ; otherwise the pipeline issues them with the warning DIVERSITY_LOW.
        duplicates=[v for group in audit_diversity(docs)['duplicates'] for v in group[1:]]
        for combo in product(('single','columns','cards'),repeat=len(duplicates)):
            forced=dict(zip(duplicates,combo))
            attempt=[compose(vi,variant,all_picks[vi],variant!='columns',forced.get(variant)) for vi,variant in enumerate(config['variants'])]
            if audit_diversity(attempt)['passed']:
                docs=attempt;break
    # Session 15 (26.09.2026, deck 36): the dashes that mark the items of a sample slide go with their items (item_marks.py).
    table,wide=item_marks.widening(style)
    if table:item_marks.apply(docs,table,wide)
    return docs
