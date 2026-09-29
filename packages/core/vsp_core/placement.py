"""Content capacity and bounded reflow of observed regular card grids."""
import copy
import math
from .patterns import contains


def estimate_lines(text,width,size):
    # Keep words intact. A character-only estimate misses Russian word wraps.
    capacity=max(1,width/(size*.56));total=0
    for paragraph in text.split('\n'):
        used=0;lines=1
        for word in paragraph.split():
            length=len(word)
            if used and used+1+length>capacity:lines+=1;used=0
            if length>capacity:
                lines+=int((length-1)//capacity);used=length%capacity or capacity
            else:used+=length+(1 if used else 0)
        total+=lines
    return total


def body_box(slot):
    box=list(slot['box'])
    if slot.get('fill'):
        l,t,r,b=slot.get('inset',[10,5,10,5])
        box=[box[0]+l,box[1]+t,box[2]-l-r,box[3]-t-b]
    return box


def partition_region(pattern,slot,count,width,height,columns=1):
    """Split an observed plain text region; never invent a region on a picture."""
    if count<2 or slot.get('fill') or card_for(slot,pattern,width,height):return None
    x,y,w,h=body_box(slot)
    # Keep the footer band clear even when the source placeholder extends into it.
    h=min(h,height*.92-y)
    if w<width*.23 or h<height*.18:return None
    rows=math.ceil(count/columns);gap=max(12,min(28,slot['font_size']*.9))
    cellw=(w-gap*(columns-1))/columns;cellh=(h-gap*(rows-1))/rows
    if cellw<width*.16 or cellh<height*.06:return None
    result=[]
    for i in range(count):
        child=copy.deepcopy(slot);child['box']=[x+(i%columns)*(cellw+gap),y+(i//columns)*(cellh+gap),cellw,cellh]
        child['source']={**child['source'],'derivation':'partition-observed-text-region','original_box':slot['box'],'columns':columns,'index':i}
        result.append(child)
    return result


# G2 for several facts (proposals of 24.09.2026): the lowest point an observed plain text region may grow to when nothing
# of its composition stands below it. A background picture can carry a footer the parser does not see (deck 29: the address
# line is baked into the picture of the slide), so the bound is the one the units of pictograms use (UNIT_BOTTOM, .8).
GROW_BOTTOM=.8
GROW_GAP=12


def grow_region(pattern,slot,width,height):
    """The allowed region of an observed plain text region: the same columns grown down to the nearest shape of the
    composition below it that does not hold it (another text, the title, a decoration, a surface) or to GROW_BOTTOM of the
    slide. The sample text only shows where the text starts; its frame is often as small as one sample line (deck 29 2:
    68 px). None for a region on a card or a filled shape, a narrow one, or one with no room below."""
    if slot.get('fill') or card_for(slot,pattern,width,height):return None
    x,y,w,h=slot['box']
    if w<width*.23:return None
    limit=height*GROW_BOTTOM
    others=[s['box'] for s in pattern['slots'] if s is not slot]+[pattern['title']['box']]
    others+=[d['box'] for d in pattern['decorations']+pattern['surfaces'] if not contains(d['box'],slot['box'])]
    for b in others:
        if b[1]>=y+h-2 and b[0]<x+w and b[0]+b[2]>x:limit=min(limit,b[1]-GROW_GAP)
    if limit<=y+h+1:return None
    grown=copy.deepcopy(slot);grown['box']=[x,y,w,limit-y]
    grown['source']={**grown['source'],'derivation':'grown-observed-text-region','original_box':slot['box'],'bottom':round(limit,2),
                     'allowed_box':[x,y,w,limit-y]}
    return grown


def card_for(slot,pattern,width,height):
    # Session 15 (26.09.2026, deck 12): the empty panel a slot was made from (patterns.py, 'empty-panel') is the card of the whole
    # region, not of one fact; the region may be split or grown inside it (its box is already the panel less an inset). Two
    # facts took the plain Office layout on 7 slides of 11 because the panel held the slot.
    if (slot.get('classification') or {}).get('source')=='empty-panel':return None
    candidates=[s for s in pattern['surfaces'] if contains(s['box'],slot['box']) and s['box'][2]*s['box'][3]<=width*height*.5]
    return min(candidates,key=lambda s:s['box'][2]*s['box'][3]) if candidates else None


def reflow_grid(pattern,slots,count,width,height,column_count=None):
    """Only regular grids of native filled rectangles; never move raster assets."""
    if len(slots)<2:return None
    cards=[card_for(s,pattern,width,height) for s in slots]
    if any(c is None for c in cards):return None
    if len({tuple(c['box']) for c in cards})!=len(cards):return None
    cw,ch=cards[0]['box'][2:]
    if any(abs(c['box'][2]-cw)>cw*.08 or abs(c['box'][3]-ch)>ch*.08 for c in cards):return None
    xs=sorted({round(c['box'][0]/5)*5 for c in cards})
    ys=sorted({round(c['box'][1]/5)*5 for c in cards})
    if len(xs)*len(ys)!=len(cards):return None
    left=min(c['box'][0] for c in cards);top=min(c['box'][1] for c in cards)
    right=max(c['box'][0]+c['box'][2] for c in cards);bottom=max(c['box'][1]+c['box'][3] for c in cards)
    columns=min(count,column_count or len(xs));rows=math.ceil(count/columns)
    if rows>len(ys) and column_count is None:return None
    gap_x=max(8,(right-left-len(xs)*cw)/max(1,len(xs)-1));gap_y=max(8,(bottom-top-len(ys)*ch)/max(1,len(ys)-1))
    cellw=(right-left-(columns-1)*gap_x)/columns
    cellh=min(ch,(bottom-top-(rows-1)*gap_y)/rows)
    result=[]
    for i in range(count):
        original=slots[i];surface=copy.deepcopy(cards[i])
        box=[left+(i%columns)*(cellw+gap_x),top+(i//columns)*(cellh+gap_y),cellw,cellh]
        surface['box']=box
        # Original body inset, bounded so omitted small headings do not leave a hole.
        pad=max(10,min(24,original['box'][0]-cards[i]['box'][0]))
        slot=copy.deepcopy(original);slot['box']=[box[0]+pad,box[1]+pad,box[2]-2*pad,box[3]-2*pad]
        slot['fill']=None;slot['surface_override']=surface
        slot['source']={**slot['source'],'derivation':'regular-card-grid','original_box':original['box'],'original_card':cards[i]['box']}
        result.append(slot)
    return result
