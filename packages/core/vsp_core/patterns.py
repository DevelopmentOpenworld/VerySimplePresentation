"""Bounded extraction of reference compositions into our own data model.

No source slide XML is copied to the output. Coordinates, text roles, background
and permitted decorative assets keep their original provenance. Unsupported
features stay explicit; this is not a complete PowerPoint renderer.
"""
import base64
from collections import Counter
import colorsys
import math
import re
from .importer import NS, EMU, xml, relationships, bbox, placeholder, matching, pictures, digest
from .roles import infer_role, infer_pattern_intent
from .style_compatibility import filter_layouts
from . import typography
from .png import top_row_clear

# Owner request of 29.09.2026, late evening (the third variant, former first, on pictures of the template: «выкручиваем показатель
# страниц с изображениями на максимум»): a picture of a sample slide that is no furniture is kept apart as an illustration of its
# composition when it is a cut-out object of the template: a top-level PNG with an alpha channel whose first row is clear for at
# least ILLUSTRATION_CLEAR (template A: the spring of slides 7-8 1.0, the cube of 18 1.0, the 3D figures of 22-28 .55-.98; its
# screenshots .14-.29, the photograph of 43 0, the panels of 22 .02) and that covers ILLUSTRATION_AREA of the slide. Only the
# variant on pictures (composer, sequence) places them; the decor of every composition stays as before.
ILLUSTRATION_CLEAR = .5
ILLUSTRATION_AREA = (.03, .6)


def rgb(node, palette, mapping=None):
    if node is None:
        return None
    tag=node.tag.rsplit('}',1)[-1]
    value=node.get('val','')
    if tag=='schemeClr': value=palette.get((mapping or {}).get(value,value))
    elif tag=='sysClr': value=node.get('lastClr')
    elif tag!='srgbClr': return None
    if not value or len(value)!=6:
        return None
    try: channels=[v/255 for v in bytes.fromhex(value)]
    except ValueError: return None
    for transform in node:
        kind=transform.tag.rsplit('}',1)[-1];amount=int(transform.get('val','100000'))/100000
        if kind=='tint': channels=[v+(1-v)*amount for v in channels]
        elif kind=='shade': channels=[v*amount for v in channels]
        elif kind in ('lumMod','lumOff','satMod'):
            h,l,s=colorsys.rgb_to_hls(*channels)
            if kind=='lumMod': l*=amount
            elif kind=='lumOff': l+=amount
            else: s*=amount
            channels=list(colorsys.hls_to_rgb(h,min(1,max(0,l)),min(1,max(0,s))))
    return ''.join(f'{min(255,max(0,round(v*255))):02X}' for v in channels)


def mean_colour(fill, palette, mapping=None):
    """G2b: mean colour of a gradient (its stops) or a pattern (foreground and background) fill; None if unknown."""
    tag=fill.tag.rsplit('}',1)[-1]
    if tag=='gradFill':nodes=[gs[0] for gs in fill.findall('a:gsLst/a:gs',NS) if len(gs)]
    elif tag=='pattFill':nodes=[n[0] for n in (fill.find('a:fgClr',NS),fill.find('a:bgClr',NS)) if n is not None and len(n)]
    else:return None
    colours=[c for c in (rgb(n,palette,mapping) for n in nodes) if c]
    if not colours or len(colours)<len(nodes):return None
    return ''.join(f'{round(sum(int(c[i:i+2],16) for c in colours)/len(colours)):02X}' for i in (0,2,4))


def luminance(color):
    values=[v/255 for v in bytes.fromhex(color)]
    values=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in values]
    return sum(v*w for v,w in zip(values,(.2126,.7152,.0722)))


def contrast(a,b):
    x,y=sorted([luminance(a),luminance(b)])
    return (y+.05)/(x+.05)


def blend(foreground,background,opacity=1):
    return ''.join(f'{round(a*opacity+b*(1-opacity)):02X}' for a,b in zip(bytes.fromhex(foreground),bytes.fromhex(background)))


def contains(a,b,tolerance=2):
    return a[0]<=b[0]+tolerance and a[1]<=b[1]+tolerance and a[0]+a[2]>=b[0]+b[2]-tolerance and a[1]+a[3]>=b[1]+b[3]-tolerance


HOLDS_TEXT=.95


def holds_text(box,frame):
    """Item 35, H4 (holdout 25.09.2026, deck 33): a shape holds a text frame when the frame lies within it (2 px) or at
    least HOLDS_TEXT of the area of the frame lies on it (TextBox 17 of deck 33 slide 15 starts 2.5 px left of its card:
    99.2 %). A caption wider than a small frame is not its text (template A slide 2: "Вставить фото" over a photo frame, 78 %)."""
    f=frame['box']
    return contains(box,f) or f[2]*f[3]>0 and overlap(box,f)>=HOLDS_TEXT*f[2]*f[3]


def overlap(a,b):
    return max(0,min(a[0]+a[2],b[0]+b[2])-max(a[0],b[0]))*max(0,min(a[1]+a[3],b[1]+b[3])-max(a[1],b[1]))


# Session 16 (26.09.2026), M7 of holdout-next3, cause 3 (analysis/style-experiments/20260926-hn3-m7/process/research-catalogue.md):
# a catalogue slide carries at least CATALOGUE_OBJECTS small objects of its body, of at least CATALOGUE_KINDS kinds (size within
# CATALOGUE_SAME, and fill; a picture by its size), each kind at most CATALOGUE_REPEAT times on average. Small: at most
# CATALOGUE_AREA of the canvas; body: not touching the slide edge, the centre within 15..93 % of the height and 3..97 % of the
# width (bands, corner logos and footers are the frame of the slide).
CATALOGUE_OBJECTS,CATALOGUE_KINDS,CATALOGUE_REPEAT,CATALOGUE_SAME,CATALOGUE_AREA=6,4,3,.12,.02


def round_adj(shape):
    """Corner radius of a roundRect shape as a share of its shorter side (DrawingML: adj/100000, 16667 by default); None
    for any other geometry — the rule of sample_clone._round_adj (which imports this module). The preview draws a card or a
    band of a composition with the radius PowerPoint gives it: owner, 28.09.2026 — the rounded cards of template D
    (adj 9821) drew with the default 16667 in the PDF, and the text at their corner stood on the curve."""
    geometry=shape.find('p:spPr/a:prstGeom',NS)
    if geometry is None or geometry.get('prst')!='roundRect':return None
    gd=geometry.find("a:avLst/a:gd[@name='adj']",NS)
    value=re.match(r'val\s+(-?\d+)$',(gd.get('fmla') or '').strip()) if gd is not None else None
    return min(50000,max(0,int(value.group(1))))/100000 if value else .16667


def catalogue_objects(own,width,height,decor_ids=()):
    """own: (box, fill) of the decor a composition takes from its sample slide, and (box, fill, id) of the decor nodes of
    the card sample of that slide (sample_clone: cloned in place); a node that is also a decoration counts once."""
    kinds=[];count=0
    for item in own:
        box,fill=item[0],item[1]
        if len(item)>2 and item[2] in decor_ids:continue
        x,y,w,h=box
        if w*h>width*height*CATALOGUE_AREA or x<=2 or y<=2 or x+w>=width-2 or y+h>=height-2:continue
        if not (height*.15<=y+h/2<=height*.93 and width*.03<=x+w/2<=width*.97):continue
        count+=1
        key='picture' if fill in (None,'picture','pic') else fill
        if not any(k==key and abs(a-w)<=CATALOGUE_SAME*max(a,w,1) and abs(b-h)<=CATALOGUE_SAME*max(b,h,1) for a,b,k in kinds):
            kinds.append((w,h,key))
    return count>=CATALOGUE_OBJECTS and len(kinds)>=CATALOGUE_KINDS and count<=CATALOGUE_REPEAT*len(kinds)


def read_patterns(z, presentation, parts, width, height, font, skip=(), card_decor=None):
    """skip: numbers of the slides of instructions at the start of the sample (reference.read_inventory); they give no
    composition, and the first slide after them is the sample of the cover. card_decor: {sample slide number: [{'box',
    'fill', 'id'}]} — the decor nodes of the card sample of the slide (sample_clone.read_card_samples, role 'decor'), for the
    catalogue rule."""
    cache={};assets={};patterns=[];unsupported=Counter();unanchored=[];unrendered=[]
    catalogue=[]
    opening=next((n for n in range(1,len(parts)+1) if n not in skip),1)
    def root(p):
        if p not in cache: cache[p]=xml(z.read(p))
        return cache[p]
    def related(part,kind):
        return next((r['part'] for r in relationships(z,part).values() if r['type']==kind),None)
    # After instruction slides the author's cover is known: its layout is the layout of the cover, and the composition of that
    # layout is a cover too (deck 28: the black "Presentation Title" gave content slides of key numbers, with a chart whose
    # labels vanished on black, once the instruction slide no longer counted as a use of the white "Content Slide").
    opening_layout=related(parts[opening-1],'slideLayout') if skip else None
    # Repeated small edge images are evidence of furniture, not a license to
    # copy every source illustration into new content.
    image_occurrences=Counter()
    for part in parts:
        rr=relationships(z,part);seen=set()
        for pic,_,_ in pictures(root(part).find('p:cSld/p:spTree',NS)):
            blip=pic.find('p:blipFill/a:blip',NS)
            ref=rr.get(blip.get('{'+NS['r']+'}embed')) if blip is not None else None
            # Item 37, cause 3: pictures the preview cannot draw (EMF logos of deck 19) count as well; their digests never
            # equal those of raster pictures, so the evidence for raster furniture stays as it was.
            if ref and ref['part'] in z.namelist():seen.add(digest(z.read(ref['part'])))
        image_occurrences.update(seen)
    records=[(p,'slide',i) for i,p in enumerate(parts,1) if i not in skip]
    if skip:unsupported['instruction_slide']+=len(skip)
    layout_parts=[]
    for rel in relationships(z,'ppt/presentation.xml').values():
        if rel['type']=='slideMaster':
            layout_parts.extend(r['part'] for r in relationships(z,rel['part']).values() if r['type']=='slideLayout')
    records.extend((p,'layout',i) for i,p in enumerate(dict.fromkeys(layout_parts),len(parts)+1))
    for part,source_kind,number in records:
        slide=root(part);lp=related(part,'slideLayout') if source_kind=='slide' else part;layout=root(lp) if lp else None
        # Vertical writing is outside the current renderer's contract.
        if source_kind=='layout' and slide.get('type') in ('vertTx','vertTitleAndTx'):continue
        mp=related(lp,'slideMaster') if lp else None;master=root(mp) if mp else None
        tp=related(mp,'theme') if mp else None;theme=root(tp) if tp else None
        palette,mapping=typography.color_context(theme,master,(layout,slide))
        theme_fonts=typography.theme_fonts(theme,font)
        owners=[(slide,part),(layout,lp),(master,mp)]
        background='FFFFFF';background_origin='fallback';bg_image=None;background_supported=True;approx_background=None
        for owner,owner_part in owners:
            if owner is None: continue
            bg=owner.find('p:cSld/p:bg',NS)
            if bg is None: continue
            fill_owner=owner_part;fill=bg.find('p:bgPr',NS)
            fill=next((n for n in fill if n.tag.endswith(('}solidFill','}blipFill','}gradFill','}pattFill'))),None) if fill is not None else None
            ref=bg.find('p:bgRef',NS)
            fill_palette=dict(palette)
            if fill is None and ref is not None and theme is not None:
                index=int(ref.get('idx','0'))
                choices=theme.findall('a:themeElements/a:fmtScheme/a:bgFillStyleLst/*',NS) if index>=1001 else theme.findall('a:themeElements/a:fmtScheme/a:fillStyleLst/*',NS)
                index-=1001 if index>=1001 else 1
                if 0<=index<len(choices):fill=choices[index];fill_owner=tp
                if len(ref):fill_palette['phClr']=rgb(ref[0],palette,mapping)
            if fill is not None and fill.tag.endswith('}solidFill'):
                value=rgb(fill[0],fill_palette,mapping) if len(fill) else None
                if value:background=value;background_origin=owner_part;break
            if fill is not None and fill.tag.endswith('}blipFill'):
                bg_image=(fill,fill_owner);background_origin=owner_part;break
            # G2b (blind test of 23.09, analysis/style-experiments/20260923-generalization): a gradient or pattern the slide
            # inherits from its layout or master is drawn by PowerPoint on the carrier; its mean colour stands for it in
            # contrast decisions, and the preview is marked incomplete. A slide's own gradient stays unsupported: the
            # carrier writes a slide background only as a solid colour.
            approx=mean_colour(fill,fill_palette,mapping) if fill is not None and (owner_part!=part or source_kind=='layout') else None
            if approx:
                background=approx;background_origin=owner_part;approx_background=(owner_part,fill.tag.rsplit('}',1)[-1]);break
            unsupported['complex_background']+=1
            background_supported=False
            break
        if not background_supported:continue
        default_ink='FFFFFF' if luminance(background)<.2 else '151515'

        def frame(shape,transformed=None):
            def paragraph_text(para):
                return ''.join('\n' if child.tag=='{'+NS['a']+'}br' else ''.join(n.text or '' for n in child.findall('a:t',NS)) for child in para)
            ph=placeholder(shape)
            chain=[(shape,part),(matching(layout,ph),lp),(matching(master,ph,True),mp)]
            box=transformed or next((bbox(n) for n,_ in chain if n is not None and bbox(n)),None)
            if box is None: return None
            props,paras=typography.run_candidates([n for n,_ in chain],master,presentation,ph)
            size=next((int(n.get('sz'))/75 for n in props if n.get('sz')),24)
            color=next((rgb(n.find('a:solidFill/*',NS),palette,mapping) for n in props if rgb(n.find('a:solidFill/*',NS),palette,mapping)),None)
            # Font/colour references stored on p:style apply before theme fallback.
            reference=shape.find('p:style/a:fontRef/*',NS)
            color=color or rgb(reference,palette,mapping) or default_ink
            family=next((n.find('a:latin',NS).get('typeface') for n in props if n.find('a:latin',NS)is not None),font)
            if family.startswith('+'): family=theme_fonts.get('major' if family.startswith('+mj') else 'minor',font)
            align=next((n.get('algn') for n in paras if n.get('algn')), 'l')
            val='\n'.join(paragraph_text(p) for p in shape.findall('p:txBody/a:p',NS))
            paragraphs=[]
            for para in shape.findall('p:txBody/a:p',NS):
                value=paragraph_text(para).strip()
                if not value:continue
                props_here=para.findall('a:r/a:rPr',NS)+para.findall('a:pPr/a:defRPr',NS)
                paragraphs.append({'text':value,
                    'font_size':next((int(n.get('sz'))/75 for n in props_here if n.get('sz')),size),
                    'color':next((rgb(n.find('a:solidFill/*',NS),palette,mapping) for n in props_here if rgb(n.find('a:solidFill/*',NS),palette,mapping)),color),
                    'bold':next((n.get('b')=='1' for n in props_here if n.get('b') is not None),False)})
            body_samples=[p for p in paragraphs if p['text'].lower() in ('текст','text','body text') or len(p['text'])>50]
            sample=max(body_samples,key=lambda p:len(p['text'])) if body_samples and len(paragraphs)>1 and not (ph and ph[0] in ('title','ctrTitle')) else None
            if sample:size=sample['font_size'];color=sample['color']
            sid=shape.find('p:nvSpPr/p:cNvPr',NS).get('id')
            fill=rgb(shape.find('p:spPr/a:solidFill/*',NS),palette,mapping)
            body=shape.find('p:txBody/a:bodyPr',NS)
            inset=[int(body.get(k,str(default)))/EMU if body is not None else default/EMU for k,default in (('lIns',91440),('tIns',45720),('rIns',91440),('bIns',45720))]
            return {'box':box,'font':family,'font_size':size,'color':color,'bold':sample['bold'] if sample else next((n.get('b')=='1' for n in props if n.get('b') is not None),False),
                'align':{'ctr':'center','r':'right'}.get(align,'left'),'text_length':len(val.strip()),'placeholder':ph,
                'source_text':val,'body_sample':bool(sample),
                'source':{'slide':number if source_kind=='slide' else None,'part':part,'shape':sid,'kind':source_kind},'fill':fill,'inset':inset,
                'geometry':(shape.find('p:spPr/a:prstGeom',NS).get('prst') if shape.find('p:spPr/a:prstGeom',NS)is not None else 'rect'),
                'adj':round_adj(shape)}

        frames=[];surfaces=[]
        for shape,transformed,_ in pictures(slide.find('p:cSld/p:spTree',NS),object_type='sp'):
            nv=shape.find('p:nvSpPr/p:cNvPr',NS)
            if nv is not None and nv.get('hidden') in ('1','true'): continue
            f=frame(shape,transformed)
            if f is None: continue
            if f['text_length'] or f['placeholder']: frames.append(f)
            if f['fill'] and f['geometry'] in ('rect','roundRect'):
                surfaces.append(f)
        titles=[f for f in frames if f['placeholder'] and f['placeholder'][0] in ('title','ctrTitle')]
        if not titles:
            titles=[f for f in frames if f['box'][1]<height*.22 and f['font_size']>=28 and f['box'][2]>width*.28]
        if not titles:
            # G2a (blind test of 23.09): a template of small type has no 28 pt frame (deck 29: every text is 18 pt,
            # the title is a short white line over the band of a background picture). The largest frame of the top band is
            # the title when it is clearly larger than the other text of the slide, or when it is as large, short, above all
            # other text and a longer text follows; a small label above larger text (a deck name in a corner) is neither.
            top=[f for f in frames if f['text_length'] and f['box'][1]<height*.22 and f['box'][2]>width*.28]
            if top:
                best=max(top,key=lambda f:(f['font_size'],f['box'][2]))
                others=[f for f in frames if f is not best and f['text_length']]
                rest=sorted(f['font_size'] for f in others)
                median=rest[len(rest)//2] if rest else None
                leading=(median is not None and best['font_size']>=median and best['text_length']<=80
                         and all(f['box'][1]>=best['box'][1]+best['box'][3]*.5 for f in others)
                         and any(f['text_length']>best['text_length'] for f in others))
                if median is not None and (best['font_size']>=1.15*median or leading):titles=[best]
        layout_name=layout.find('p:cSld',NS).get('name','') if layout is not None and layout.find('p:cSld',NS) is not None else ''
        cover=(source_kind=='slide' and number==opening) or (source_kind=='layout' and part==opening_layout) or (source_kind=='layout' and (slide.get('type')=='title' or re.fullmatch(r'title slide|presentation opener(?: option)?',layout_name,re.I)))
        if not titles and cover:
            candidates=[f for f in frames if f['text_length'] and f['box'][2]>=width*.25 and f['box'][3]<height*.5 and f['box'][1]<height*.85]
            if candidates:titles=[max(candidates,key=lambda f:(f['font_size'],f['box'][2]))]
        if not titles: continue
        title=min(titles,key=lambda f:f['box'][1])
        visuals=[box for _,box,_ in pictures(slide.find('p:cSld/p:spTree',NS)) if box[0]>=0 and box[1]>=0 and box[0]+box[2]<=width+2]
        if source_kind=='slide' and not any(f['text_length'] for f in frames) and any(b[2]*b[3]>=width*height*.8 for b in visuals):
            unsupported['raster_without_observed_text_region']+=1
            unanchored.append({'source_part':part,'reason':'large-slide-raster-with-only-empty-placeholders'})
            continue
        for f in frames:
            f['classification']=infer_role(f,width,height,visuals)
        slots=[f for f in frames if f is not title and f['box'][2]>=width*.13 and f['box'][3]>=height*.025
               and f['box'][1]+f['box'][3]<height*.97 and f['box'][1]>=min(height*.12,title['box'][1])
               and not (f['placeholder'] and f['placeholder'][0] in ('sldNum','ftr','dt','pic'))
               and overlap(f['box'],title['box'])<4]
        # Prefer real content regions over short labels; nested text boxes are not
        # independent slots. This is geometric inference, not semantic planning.
        slots.sort(key=lambda f:(f['classification']['role']!='body',-f['box'][2]*f['box'][3]))
        chosen=[]
        for f in slots:
            if not any(overlap(f['box'],g['box'])>min(f['box'][2]*f['box'][3],g['box'][2]*g['box'][3])*.2 for g in chosen): chosen.append(f)
        slots=sorted(chosen,key=lambda f:(round(f['box'][1]/20),f['box'][0]))[:12]
        if not slots and source_kind=='slide' and not cover:
            # Holdout-next2 26.09.2026 (deck 12/deck 13-2026): a content sample slide of text boxes on a Blank layout is a title and
            # an empty white card for the content; without a text frame it gave no composition, and the whole deck fell back
            # to the plain Office layouts (no logos, no cream background, no title style). The largest empty filled panel
            # under the title is the body region: its box less an inset, in a colour that reads on the panel.
            tb=title['box']
            panels=[s for s in surfaces if not s['text_length'] and s['box'][1]>=tb[1]+tb[3]-4 and s['box'][2]>=width*.4
                    and s['box'][3]>=height*.3 and s['box'][1]+s['box'][3]<=height*.97]
            if panels:
                panel=max(panels,key=lambda s:s['box'][2]*s['box'][3]);pad=max(12,width*.02);b=panel['box']
                ink=panel['color'] if contrast(panel['color'],panel['fill'])>=4.5 else ('FFFFFF' if luminance(panel['fill'])<.2 else '151515')
                # Session 15 (26.09.2026): the empty paragraph of the panel carries what PowerPoint writes into every inserted
                # shape (18 pt, centred), not a choice of the author about text; deck 12 set its facts in 17 pt centred in a card
                # of 1107 x 413 px. The text starts flush left at the size the composer allows body text (28 px).
                slots=[{**panel,'box':[b[0]+pad,b[1]+pad,b[2]-2*pad,b[3]-2*pad],'color':ink,'fill':None,'source_text':'',
                        'align':'left','font_size':28,'classification':{'role':'body','source':'empty-panel'}}]
        native_table=None
        for graphic in slide.findall('p:cSld/p:spTree/p:graphicFrame',NS):
            tbl=graphic.find('.//a:tbl',NS);transform=graphic.find('p:xfrm',NS)
            if tbl is None or transform is None:continue
            off,ext=transform.find('a:off',NS),transform.find('a:ext',NS)
            if off is None or ext is None:continue
            box=[int(off.get('x'))/EMU,int(off.get('y'))/EMU,int(ext.get('cx'))/EMU,int(ext.get('cy'))/EMU]
            rows=tbl.findall('a:tr',NS)
            fills=[rgb(row.find('a:tc/a:tcPr/a:solidFill/*',NS),palette,mapping) for row in rows[:3]]
            inks=[rgb(row.find('a:tc/a:txBody/a:p/a:r/a:rPr/a:solidFill/*',NS),palette,mapping) for row in rows[:2]]
            native_table={'box':box,'header_fill':fills[0] if fills else None,'body_fill':fills[1] if len(fills)>1 else None,
                'header_color':inks[0] if inks else None,'body_color':inks[1] if len(inks)>1 else None,
                'source':{'slide':number,'part':part,'shape':graphic.find('p:nvGraphicFramePr/p:cNvPr',NS).get('id')}}
            break
        # Holdout-next2 26.09.2026 (deck 32, deck 36): the brand scene of a sample slide is a raster picture over the whole
        # slide on the slide itself (the frame with the logo, the blue waves), on a plain Blank or "Title and Content" layout.
        # Only small pictures at the edge were furniture, so the scene was dropped and the derived slides stood on white. A
        # picture of the slide (not a placeholder, not in a group) that covers at least 90 % of the canvas and lies under all
        # text of the slide is the backplate of its composition.
        tree=slide.find('p:cSld/p:spTree',NS)
        # Holdout-next2 26.09.2026 (МИФИ): the brand book slides "icon library" (43 icons, a note) gave compositions, and one
        # icon drawn as a plain filled square became decor: small blue squares floated over the text of derived slides. A
        # sample slide of at least 30 small shapes of its own and at most 4 texts is a catalogue, not a composition.
        if source_kind=='slide' and sum(1 for _,b,_ in pictures(tree,object_type=('sp','pic')) if b and b[2]*b[3]<width*height*.004)>=30 \
                and sum(1 for f in frames if f['text_length'])<=4:
            unsupported['catalogue_slide']+=1;continue
        order={id(s):i for i,(s,_,_) in enumerate(pictures(tree,object_type=('sp','pic','cxnSp')))}
        first_text=min((order[id(s)] for s,_,_ in pictures(tree,object_type='sp') if id(s) in order and any((n.text or '').strip() for n in s.findall('.//a:t',NS))),default=None)
        def backplate(pic,box,cluster):
            # Raster or not: an EMF backplate (the frames of the content slides of deck 32) is carried as a picture of the
            # sample slide in the PPTX and is invisible in the preview (decor kind 'backplate', composer.begin).
            if source_kind!='slide' or cluster is not None or box is None or pic.find('p:nvPicPr/p:nvPr/p:ph',NS) is not None:return False
            blip=pic.find('p:blipFill/a:blip',NS);ref=relationships(z,part).get(blip.get('{'+NS['r']+'}embed')) if blip is not None else None
            if not ref:return False
            x,y,pw,ph=box
            seen=max(0,min(width,x+pw)-max(0,x))*max(0,min(height,y+ph)-max(0,y))
            return seen>=width*height*.9 and (first_text is None or order[id(pic)]<first_text)
        if not cover and not slots and not native_table and layout is not None and any(backplate(*p) for p in pictures(tree)):
            # A sample slide of a title alone on such a backplate: the body placeholder of its layout is the text region.
            for shape,_,_ in pictures(layout.find('p:cSld/p:spTree',NS),object_type='sp'):
                ph=placeholder(shape)
                if ph and ph[0] in ('body','obj'):
                    f=frame(shape)
                    if f and overlap(f['box'],title['box'])<4 and f['box'][1]>=title['box'][1]:
                        f['source']={'slide':number,'part':lp,'shape':f['source']['shape'],'kind':'layout-body-for-title-only-slide'}
                        f['classification']={**infer_role(f,width,height,visuals),'role':'body'};slots=[f];break
        if not cover and not slots and not native_table: continue
        decor=[];pattern_supported=True;pattern_unrendered=[];illustrations=[];slide_pictures=[]
        if bg_image:
            fill,fill_owner=bg_image;blip=fill.find('a:blip',NS)
            relation=relationships(z,fill_owner).get(blip.get('{'+NS['r']+'}embed')) if blip is not None else None
            if relation and relation['part'].lower().endswith(('.png','.jpg','.jpeg')):
                raw=z.read(relation['part']);key=digest(raw)
                assets.setdefault(key,{'mime':'image/png' if relation['part'].lower().endswith('.png') else 'image/jpeg','data':base64.b64encode(raw).decode(),'sha256':key})
                cropnode=fill.find('a:srcRect',NS);crop={k:int(cropnode.get(k,'0'))/100000 if cropnode is not None else 0 for k in ('l','t','r','b')}
                if min(crop.values())<0 or crop['l']+crop['r']>=1 or crop['t']+crop['b']>=1:unsupported['invalid_crop']+=1;continue
                decor.append({'asset':key,'box':[0,0,width,height],'crop':crop,'purpose':'background','source':{'slide':number if source_kind=='slide' else None,'part':fill_owner,'media':relation['part'],'kind':'background-fill'}})
            else:unsupported['non_raster_background']+=1;continue
        show_master=slide.get('showMasterSp','1') not in ('0','false') and (layout is None or layout.get('showMasterSp','1') not in ('0','false'))
        layers=([(master,mp)] if show_master else [])+[(layout,lp)]
        if source_kind=='slide':layers.append((slide,part))
        for layer_order,(owner,owner_part) in enumerate(layers):
            if owner is None:continue
            rr=relationships(z,owner_part)
            ranks={id(shape):i for i,(shape,_,_) in enumerate(pictures(owner.find('p:cSld/p:spTree',NS),object_type=('sp','pic','cxnSp')))}
            for shape,box,_ in pictures(owner.find('p:cSld/p:spTree',NS),object_type='sp'):
                nv=shape.find('p:nvSpPr/p:cNvPr',NS)
                if nv is not None and nv.get('hidden') in ('1','true'):continue
                if box is None or placeholder(shape) or any((n.text or '').strip() for n in shape.findall('.//a:t',NS)):continue
                geometry=shape.find('p:spPr/a:prstGeom',NS);fill=rgb(shape.find('p:spPr/a:solidFill/*',NS),palette,mapping)
                if geometry is None or geometry.get('prst') not in ('rect','roundRect') or not fill:continue
                alpha=shape.find('p:spPr/a:solidFill/*/a:alpha',NS)
                opacity=int(alpha.get('val'))/100000 if alpha is not None else 1
                if not 0<=opacity<=1:unsupported['invalid_opacity']+=1;continue
                if not contains([0,0,width,height],box):
                    # Holdout 25.09 (analysis/style-experiments/20260925-holdout-fixes, H2a): the band under the titles of
                    # deck 01 bleeds 3 pt over the top edge; left out, the title was coloured for a white slide. Like the
                    # pictures below, a shape that overlaps the canvas is clipped to it; one outside the canvas stays out.
                    nx,ny=max(0,box[0]),max(0,box[1]);nr,nb=min(width,box[0]+box[2]),min(height,box[1]+box[3])
                    if nr<=nx or nb<=ny:continue
                    box=[round(v,2) for v in (nx,ny,nr-nx,nb-ny)]
                # A filled shape of the sample slide with a text frame on it is the card of that text (a surface): it comes
                # with the text, not as decor of every slide on this composition (H4: the empty dark cards of deck 33). A
                # band under the title stays decor as before: every derived slide sets its title there (WHO slide 13: the
                # title overhangs its brown band by 11.7 pt).
                if owner_part==part and source_kind=='slide' and box[2]*box[3]<width*height*.7 and any(contains(box,f['box']) or f is not title and holds_text(box,f) for f in frames):continue
                decor.append({'kind':'shape','geometry':geometry.get('prst'),'adj':round_adj(shape),'fill':fill,'opacity':opacity,'box':box,'purpose':'background',
                    'layer_order':layer_order,'z_order':ranks[id(shape)],
                    'source':{'slide':number if source_kind=='slide' else None,'part':owner_part,'shape':shape.find('p:nvSpPr/p:cNvPr',NS).get('id')}})
            if owner_part==part and source_kind=='slide':
                # Item 37, cause 3 (holdout-next 25.09.2026, deck 19): a straight line the sample slide draws itself (the white
                # rule under its title) is decor of the composition; the preview draws it as a thin bar, the PPTX gets the
                # line of the sample slide itself (a copy of its shape, carrier_export.bind_carrier: furniture).
                for line,box,cluster in pictures(owner.find('p:cSld/p:spTree',NS),object_type='cxnSp'):
                    nv=line.find('p:nvCxnSpPr/p:cNvPr',NS);xfrm=line.find('p:spPr/a:xfrm',NS)
                    if cluster is not None or nv is None or nv.get('hidden') in ('1','true') or box is None or (xfrm is not None and xfrm.get('rot','0')!='0'):continue
                    geometry=line.find('p:spPr/a:prstGeom',NS);ln=line.find('p:spPr/a:ln',NS)
                    if geometry is None or geometry.get('prst') not in ('line','straightConnector1') or ln is None or ln.find('a:noFill',NS) is not None:continue
                    colour=rgb(ln.find('a:solidFill/*',NS),palette,mapping) or rgb(line.find('p:style/a:lnRef/*',NS),palette,mapping)
                    weight=max(1.0,int(ln.get('w','9525'))/EMU)
                    x,y,lw,lh=box
                    if not colour or min(lw,lh)>1 or max(lw,lh)<width*.05:continue
                    # Only a rule of the frame of the slide or of its composition: in the band of the title (the top quarter), just
                    # under the title, at the bottom or along a side edge, or a vertical divider between the title column and the text
                    # (deck 16 split). A line in the middle belongs to the content of that sample (the row rules of deck 37) and would cross the
                    # facts of a derived slide.
                    tx,ty,tw,th=title['box'];left=min((f['box'][0] for f in slots),default=width)
                    if lh<=1:frame_rule=y<=height*.25 or y>=height*.85 or 0<=y-(ty+th)<=height*.06
                    else:frame_rule=x<=width*.15 or x>=width*.85 or tx+tw-2<=x<=left+2
                    if not frame_rule:continue
                    bar=[x,y-weight/2,lw,weight] if lh<=1 else [x-weight/2,y,weight,lh]
                    nx,ny=max(0,bar[0]),max(0,bar[1]);nr,nb=min(width,bar[0]+bar[2]),min(height,bar[1]+bar[3])
                    if nr<=nx or nb<=ny:continue
                    decor.append({'kind':'shape','geometry':'rect','fill':colour,'opacity':1,'box':[round(v,2) for v in (nx,ny,nr-nx,nb-ny)],'purpose':'background',
                        'layer_order':layer_order,'z_order':ranks[id(line)],
                        'source':{'slide':number,'part':owner_part,'shape':nv.get('id'),'kind':'slide-line'}})
            for pic,box,cluster in pictures(owner.find('p:cSld/p:spTree',NS)):
                nv=pic.find('p:nvPicPr/p:cNvPr',NS)
                if nv is not None and nv.get('hidden') in ('1','true'):continue
                blip=pic.find('p:blipFill/a:blip',NS);ref=rr.get(blip.get('{'+NS['r']+'}embed')) if blip is not None else None
                if not ref or not ref['part'].lower().endswith(('.png','.jpg','.jpeg')):
                    unsupported['non_raster_decoration']+=1
                    if ref and (owner_part!=part or source_kind=='layout'):
                        pattern_supported=False
                        unrendered.append({'pattern':f'{source_kind}-{number}','source_part':owner_part,'media':ref['part'],'box':box,'reason':'unsupported-inherited-image-format'})
                        pattern_unrendered.append(unrendered[-1])
                    elif ref and owner_part==part and nv is not None and ref['part'] in z.namelist() and backplate(pic,box,cluster):
                        decor.append({'kind':'backplate','box':[0,0,width,height],'purpose':'background','layer_order':layer_order,'z_order':ranks[id(pic)],
                            'source':{'slide':number,'part':owner_part,'media':ref['part'],'shape':nv.get('id'),'kind':'unrendered-slide-picture'}})
                    elif ref and cluster is None and nv is not None and ref['part'] in z.namelist():
                        # Item 37, cause 3 (holdout-next 25.09.2026, deck 19): a logo of the sample slide in a format the preview
                        # cannot draw (EMF) was dropped, and no derived slide had it. By the rule of raster furniture below (small,
                        # at the edge of a cover or repeated at the edge of the slides) it is decor of the composition: an
                        # invisible frame in the preview, the picture of the sample slide itself in the PPTX.
                        x,y,pw,ph=box
                        edge=x<width*.15 or y<height*.15 or x+pw>width*.85 or y+ph>height*.85
                        peripheral=x+pw<width*.08 or y+ph<height*.12 or x>width*.92 or y>height*.88
                        nx,ny=max(0,x),max(0,y);nr,nb=min(width,x+pw),min(height,y+ph)
                        # Unlike raster furniture, a single picture on the cover is no evidence here: the preview cannot show it and
                        # the audit cannot tell a logo from an illustration of the sample talk (RPN: the round "Утилизация отходов"
                        # picture of its cover). The picture has to repeat on the sample slides (deck 19: its logo on all three).
                        if pw*ph<=width*height*.12 and nr>nx and nb>ny and image_occurrences[digest(z.read(ref['part']))]>=2 and ((cover and edge) or peripheral):
                            decor.append({'kind':'ghost','box':[round(v,2) for v in (nx,ny,nr-nx,nb-ny)],'purpose':'furniture',
                                'layer_order':layer_order,'z_order':ranks[id(pic)],
                                'source':{'slide':number,'part':owner_part,'media':ref['part'],'shape':nv.get('id'),'kind':'unrendered-slide-picture'}})
                    continue
                raw=z.read(ref['part']);key=digest(raw)
                if owner_part==part and source_kind=='slide' and cluster is None:slide_pictures.append((box,raw))
                if owner_part==part and source_kind=='slide':
                    x,y,pw,ph=box
                    edge=x<width*.15 or y<height*.15 or x+pw>width*.85 or y+ph>height*.85
                    peripheral=x+pw<width*.08 or y+ph<height*.12 or x>width*.92 or y>height*.88
                    inside_card=any(contains(s['box'],box) and s['box'][2]<width*.9 and s['box'][3]>height*.18 for s in surfaces)
                    if inside_card and not cover:peripheral=False
                    # Session 15 (26.09.2026, deck 12): a logo of the sample slides in the top band at a side edge (deck 12 and deck 13 end
                    # at 104 and 115 px of 720, below the peripheral 12 %) — small, not a line, the same bytes on two slides.
                    corner=(pw*ph<=width*height*.02 and min(pw,ph)>=height*.03 and max(pw,ph)<=4*min(pw,ph) and y+ph<=height*.2
                            and (x<width*.15 or x+pw>width*.85) and image_occurrences[key]>=2 and not inside_card)
                    if not backplate(pic,box,cluster) and not (pw*ph<=width*height*.12 and ((cover and edge) or ((peripheral or corner) and image_occurrences[key]>=2))):
                        unsupported['slide_image_not_classified_as_furniture']+=1
                        # Owner request of 29.09.2026: a cut-out illustration of the sample slide (ILLUSTRATION_CLEAR), kept apart.
                        nx,ny=max(0,x),max(0,y);nr,nb=min(width,x+pw),min(height,y+ph)
                        cropnode=pic.find('p:blipFill/a:srcRect',NS)
                        crop={k:int(cropnode.get(k,'0'))/100000 if cropnode is not None else 0 for k in ('l','t','r','b')}
                        if (not cover and cluster is None and nv is not None and ref['part'].lower().endswith('.png') and not any(crop.values())
                                and nr>nx and nb>ny and ILLUSTRATION_AREA[0]<=(nr-nx)*(nb-ny)/(width*height)<=ILLUSTRATION_AREA[1]
                                and (top_row_clear(raw) or 0)>=ILLUSTRATION_CLEAR):
                            assets.setdefault(key,{'mime':'image/png','data':base64.b64encode(raw).decode(),'sha256':key})
                            illustrations.append({'asset':key,'box':[round(v,2) for v in (nx,ny,nr-nx,nb-ny)],
                                'crop':{'l':(nx-x)/pw,'t':(ny-y)/ph,'r':(x+pw-nr)/pw,'b':(y+ph-nb)/ph},'z_order':ranks[id(pic)],
                                'source':{'slide':number,'part':owner_part,'media':ref['part'],'shape':nv.get('id'),'kind':'slide-illustration'}})
                        continue
                assets.setdefault(key,{'mime':'image/png' if ref['part'].lower().endswith('.png') else 'image/jpeg','data':base64.b64encode(raw).decode(),'sha256':key})
                cropnode=pic.find('p:blipFill/a:srcRect',NS)
                crop={k:int(cropnode.get(k,'0'))/100000 if cropnode is not None else 0 for k in ('l','t','r','b')}
                if min(crop.values())<0 or crop['l']+crop['r']>=1 or crop['t']+crop['b']>=1:unsupported['invalid_crop']+=1;continue
                x,y,w,h=box
                if w<=0 or h<=0:continue
                # Clip legitimate bleed at canvas edges, preserving the image crop.
                nx,ny=max(0,x),max(0,y);nr,nb=min(width,x+w),min(height,y+h)
                if nr<=nx or nb<=ny:continue
                visible_w,visible_h=1-crop['l']-crop['r'],1-crop['t']-crop['b']
                crop={**crop,'l':crop['l']+(nx-x)/w*visible_w,'r':crop['r']+(x+w-nr)/w*visible_w,
                      't':crop['t']+(ny-y)/h*visible_h,'b':crop['b']+(y+h-nb)/h*visible_h}
                box=[round(v,2) for v in (nx,ny,nr-nx,nb-ny)]
                entry={'asset':key,'box':box,'crop':crop,'source':{'slide':number if source_kind=='slide' else None,'part':owner_part,'media':ref['part'],'shape':nv.get('id') if nv is not None else None,
                    'classification':'small-edge-cover-or-repeated-image' if owner_part==part and source_kind=='slide' else 'inherited-decoration'},
                    'layer_order':layer_order,'z_order':ranks[id(pic)],
                    'purpose':'background' if box[2]*box[3]>width*height*.12 or box[2]>.45*width or box[3]>.45*height or (w/visible_w>width*.9 and h/visible_h>height*.9) or nx<=2 or ny<=2 or nr>=width-2 or nb>=height-2 else 'furniture'}
                # Later layout pictures cover master logos at the same position.
                decor=[d for d in decor if not (d['purpose']=='furniture' and entry['purpose']=='furniture' and contains(box,d['box']))]
                if not any(d.get('asset')==key and d['box']==box and d.get('crop')==crop for d in decor):decor.append(entry)
        # A6 (part): inherited decor we cannot draw no longer drops the composition. PowerPoint draws it
        # from the layout or master on the carrier; the standalone composer filters these patterns out.
        decor.sort(key=lambda d:(d.get('layer_order',-1),d.get('z_order',0)))
        # Session 16 (ГУАП, MINVU; M7 of holdout-next3, cause 3): a slide of a palette of shapes (ГУАП "Базовый набор фигур":
        # the grey arrows of its arrow styles came as clones beside the facts), of a specimen drawing (MINVU "Tipología": the
        # plans and the house of rectangles came as decor) or of colours (СамГМУ) is a catalogue, not a composition: it would
        # carry onto our slides many small objects of its body of different kinds. A repeated marker (template A: one 8 px square
        # x 8) is a composition.
        if source_kind=='slide' and not cover and catalogue_objects(
                [(d['box'],d.get('fill') or 'picture') for d in decor if d['source'].get('part')==part
                 and d['source'].get('kind')!='background-fill' and d.get('kind') not in ('ghost','backplate')]
                +[(n['box'],n['fill'],n['id']) for n in (card_decor or {}).get(number,[])],width,height,
                [d['source'].get('shape') for d in decor if d['source'].get('part')==part]):
            # Its pictures the preview cannot draw were noted for this composition above; there is no composition now.
            unrendered[:]=[u for u in unrendered if u['pattern']!=f'{source_kind}-{number}']
            unsupported['catalogue_slide']+=1;catalogue.append(number);continue
        if not title['placeholder']:
            # Item 37, cause 3 (holdout-next 25.09.2026, deck 19): a title text box lower than one line of its own text (24 px
            # for 28 pt, the text overflows it without autofit) holds a long title only at 12 px. The frame takes the height
            # of one line of its size with its insets, down to the nearest shape of the composition below it (the white rule
            # of deck 19 at 65 px).
            x,y,fw,fh=title['box'];line=title['font_size']*1.2+title['inset'][1]+title['inset'][3]
            if fh<line:
                limit=min([b[1] for b in [f['box'] for f in slots]+[d['box'] for d in decor if d['purpose']!='background' or d['source'].get('kind')=='slide-line']
                           if b[1]>=y+fh-2 and b[0]<x+fw and b[0]+b[2]>x]+[height*.3])
                grown=min(line,limit-2-y)
                if grown>fh:title['box']=[x,y,fw,round(grown,2)];title['box_own']=[x,y,fw,fh]
        # Item 37, cause 4 (holdout-next 25.09.2026, deck 23): a text frame that runs across the inner edge of a decorative band
        # along the left or right edge of the slide (the strip with the Bronze Horseman, 144 of 960 px, drawn by the layout)
        # ends 12 px before it, when at least half of it stays; text set on the band itself (a frame wholly inside) stays.
        bands=[d['box'] for d in decor if d.get('kind')!='ghost' and d['box'][3]>=height*.6 and d['box'][2]<=width*.3
               and (d['box'][0]<=2 or d['box'][0]+d['box'][2]>=width-2)]
        for f in [title]+slots:
            for bx,by,bw,bh in bands:
                x,y,fw,fh=f['box']
                if by>=y+fh or by+bh<=y:continue
                if bx+bw>=width-2 and x<bx<x+fw:nx,nw=x,bx-12-x
                elif bx<=2 and x<bx+bw<x+fw:nx,nw=bx+bw+12,x+fw-bx-bw-12
                else:continue
                if nw>=fw*.5:f.setdefault('box_own',list(f['box']));f['box']=[round(nx,2),y,round(nw,2),fh]
        # Item 37, cause 4: where the sample expects a photograph (an empty picture placeholder of the slide or its layout: the
        # grey half of deck 23 slide 13) the product has none; the composer keeps text off such compositions (select).
        picture_frames=[f['box'] for f in frames if f['placeholder'] and f['placeholder'][0]=='pic' and f['box']]
        # Owner request of 29.09.2026: a cut-out that the text of its sample stands on (template A 45, 46: the rings of "10%") is a
        # figure of the sample talk, one that holds a smaller opaque picture is a device with a screen (43: the laptop with a
        # video call), and one that holds an empty picture placeholder is a device waiting for a screenshot (template D 26: a tablet with
        # a white screen on five slides of the list variant of pitch P6); none is an illustration of the template. The title is
        # not counted: titles often run over the art (template A 7).
        illustrations=[i for i in illustrations
                       if not any(overlap(f['box'],i['box'])>=f['box'][2]*f['box'][3]*.2 for f in slots)
                       and not any(b[2]*b[3]<i['box'][2]*i['box'][3]*.6 and overlap(b,i['box'])>=b[2]*b[3]*.5
                                   and (top_row_clear(r) or 0)<ILLUSTRATION_CLEAR for b,r in slide_pictures)
                       and not any(overlap(b,i['box'])>=b[2]*b[3]*.5 for b in picture_frames)]
        columns=len({round(f['box'][0]/(width*.12)) for f in slots})
        family='split' if title['box'][2]<width*.48 and slots and min(f['box'][0] for f in slots)>width*.4 else 'columns' if columns>=2 else 'sequence'
        patterns.append({'id':f'{source_kind}-{number}','source_slide':number if source_kind=='slide' else None,'source_kind':source_kind,'sort_order':number,'source_part':part,'layout_part':lp,'master_part':mp,
            'role':'cover' if cover else 'table' if native_table else 'content','family':family,'background':background,'background_origin':background_origin,
            'intent':infer_pattern_intent(title),'visual_count':len(visuals),
            'ink':default_ink,'title':title,'slots':slots,'decorations':decor,'surfaces':surfaces,'palette':palette,'native_table':native_table,
            **({'picture_frames':picture_frames} if picture_frames else {}),
            **({'illustrations':illustrations} if illustrations else {}),
            'show_master_shapes':slide.get('showMasterSp','1') not in ('0','false') if source_kind=='slide' else True,
            'color_map_override':dict(slide_override.attrib) if source_kind=='slide' and (slide_override:=slide.find('p:clrMapOvr/a:overrideClrMapping',NS)) is not None else None})
        if approx_background:
            # G2b: the preview draws the mean colour; PowerPoint draws the gradient or pattern of the layout or master.
            pattern_supported=False
            unrendered.append({'pattern':f'{source_kind}-{number}','source_part':approx_background[0],'media':'','box':[0,0,width,height],
                               'reason':'approximated-'+{'gradFill':'gradient','pattFill':'pattern'}.get(approx_background[1],'fill')+'-background'})
            pattern_unrendered.append(unrendered[-1])
        if not pattern_supported:
            patterns[-1].update({'preview_complete':False,'unrendered':pattern_unrendered})
    # G3 (blind test of 23.09): a layout is brand evidence when sample slides stand on it and none of them gives a composition
    # of its own (deck 30: every sample slide is empty, all seven layouts were rejected, and the header of the master left
    # the preview). A sample slide with its own composition is the evidence itself: cycle g2c showed that accepting its plain
    # layout as well took the deck 18 footer band off the slides, the band being a shape of the sample slides.
    composed={related(p['source_part'],'slideLayout') for p in patterns if p['source_kind']=='slide'}
    used={related(p,'slideLayout') for p in parts}-composed
    patterns,rejected=filter_layouts(patterns,used)
    unsupported['incompatible_unused_layouts']=len(rejected)
    return {'schema':'vsp.reference-patterns/1','patterns':patterns,'assets':assets,'unsupported':dict(unsupported),
            'rejected_layouts':rejected,'unanchored_raster_slides':unanchored,'unrendered_decorations':unrendered,
            **({'catalogue_slides':catalogue} if catalogue else {})}
