/* Product preflight in an isolated browser. Geometry patches only; no text edits. */
const Q={contrast:3,minBody:19,edgeGap:8};
function qOverlap(a,b,gap=0){return a.x+a.w+gap>b.x&&b.x+b.w+gap>a.x&&a.y+a.h+gap>b.y&&b.y+b.h+gap>a.y;}
function qContains(a,b){return a.x<=b.x+.1&&a.y<=b.y+.1&&a.x+a.w>=b.x+b.w-.1&&a.y+a.h>=b.y+b.h-.1;}
function qLuminance(rgb){const c=rgb.map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;});return c[0]*.2126+c[1]*.7152+c[2]*.0722;}
function qRatio(a,b){return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);}
function qRgb(color){return [0,2,4].map(i=>parseInt(color.slice(i,i+2),16));}
function qFontMin(e){return Math.min(e.font_size,e.fact_ids?.length?Q.minBody:e.id==='title'?28:e.id==='subtitle'?16:10);}
function qTextNode(canvas,id){return [...canvas.querySelectorAll('.object')].find(n=>n.dataset.element===id);}
function qAnchor(e){const a=e.reference_box||e.source_style?.original_box;return a?Object.fromEntries(['x','y','w','h'].map((k,i)=>[k,a[i]])):e;}
function qParent(slide,e){return slide.elements.filter(s=>s.type==='shape'&&!s.background_decoration&&!s.ghost&&qContains(s,qAnchor(e))).sort((a,b)=>a.w*a.h-b.w*b.h)[0];}
// A6b: a ghost (frame of undrawable inherited decor, opacity 0) counts as a logo and an obstacle, never as a backplate.
function qLogo(doc,e){return e.ghost||e.type==='image'&&(e.protected_asset||(e.w<doc.width*.35&&e.h<doc.height*.20&&(!e.template_decoration||e.w/e.h<3.5)));}
async function qBackplate(doc,slide){
  const c=document.createElement('canvas');c.width=doc.width;c.height=doc.height;
  const ctx=c.getContext('2d',{willReadFrequently:true});ctx.fillStyle='#'+slide.background;ctx.fillRect(0,0,c.width,c.height);
  for(const e of slide.elements){
    if(e.ghost)continue;
    if(e.type==='image'){
      const img=new Image();img.src=`data:${e.mime};base64,${e.data}`;await img.decode();const p=e.crop||{l:0,t:0,r:0,b:0};
      ctx.drawImage(img,img.width*p.l,img.height*p.t,img.width*(1-p.l-p.r),img.height*(1-p.t-p.b),e.x,e.y,e.w,e.h);
    }else if(e.type==='shape'){
      ctx.globalAlpha=e.opacity??1;ctx.fillStyle='#'+e.fill;ctx.beginPath();ctx.roundRect(e.x,e.y,e.w,e.h,e.geometry==='roundRect'?Math.min(e.w,e.h)*(e.adj??.1667):0);ctx.fill();ctx.globalAlpha=1;
    }
  }
  return {width:c.width,height:c.height,pixels:ctx.getImageData(0,0,c.width,c.height).data};
}
function qRasterParent(doc,slide,e,plate){
  if(!e.fact_ids?.length)return null;
  const anchor=qAnchor(e);
  if(!slide.elements.some(s=>s.type==='image'&&s.w*s.h>doc.width*doc.height*.12&&qContains(s,anchor)))return null;
  const cx=Math.floor(anchor.x+anchor.w/2),cy=Math.floor(anchor.y+anchor.h/2);
  if(cx<0||cy<0||cx>=plate.width||cy>=plate.height)return null;
  const color=plate.pixels.slice((cy*plate.width+cx)*4,(cy*plate.width+cx)*4+3);
  function same(x,y){const p=(y*plate.width+x)*4;return [0,1,2].every(k=>Math.abs(plate.pixels[p+k]-color[k])<=3);}
  let left=cx,right=cx,top=cy,bottom=cy;
  while(left>0&&same(left-1,cy))left--;
  while(right<plate.width-1&&same(right+1,cy))right++;
  while(top>0&&same(cx,top-1))top--;
  while(bottom<plate.height-1&&same(cx,bottom+1))bottom++;
  const box={id:'raster-surface',x:left+2,y:top+2,w:right-left-3,h:bottom-top-3,raster:true};
  if(box.w<doc.width*.12||box.h<doc.height*.10||box.w*box.h>doc.width*doc.height*.94||!qContains(box,anchor))return null;
  // Reject photographic patches and irregular regions. This detects only a
  // bounded, nearly uniform rectangular underlay in the actual composite.
  for(const fx of [.1,.5,.9])for(const fy of [.1,.5,.9])if(!same(Math.floor(box.x+box.w*fx),Math.floor(box.y+box.h*fy)))return null;
  return box;
}
function qGlyphs(canvas,node){
  // Every text node of the element: one for plain text, one per paragraph and marker for placeholder text (A5).
  const origin=canvas.getBoundingClientRect(),result=[],range=document.createRange();
  const walker=document.createTreeWalker(node,NodeFilter.SHOW_TEXT);
  for(let text=walker.nextNode();text;text=walker.nextNode()){
    let offset=0;
    for(const char of text.textContent){
      range.setStart(text,offset);offset+=char.length;range.setEnd(text,offset);
      if(!char.trim())continue;
      for(const r of range.getClientRects())if(r.width>0)result.push({x:r.x-origin.x,y:r.y-origin.y,w:r.width,h:r.height});
    }
  }
  return result;
}
function qInk(canvas,node){
  // What the glyphs draw: the box of every character (qGlyphs) narrowed to the ink of its font (canvas measureText).
  // The character box is the content area of the font and hangs below the line under tight spacing: deck 05 titles above
  // their rule reached it by 0.2 px in the preview, while PowerPoint draws them 6.7 px above it
  // (analysis/style-experiments/20260923-quality-tails, journal item 3).
  const origin=canvas.getBoundingClientRect(),result=[],range=document.createRange(),ctx=document.createElement('canvas').getContext('2d');
  const walker=document.createTreeWalker(node,NodeFilter.SHOW_TEXT);
  for(let text=walker.nextNode();text;text=walker.nextNode()){
    const cs=getComputedStyle(text.parentElement);ctx.font=`${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    let offset=0;
    for(const char of text.textContent){
      range.setStart(text,offset);offset+=char.length;range.setEnd(text,offset);
      if(!char.trim())continue;
      const m=ctx.measureText(char),area=m.fontBoundingBoxAscent+m.fontBoundingBoxDescent;
      for(const r of range.getClientRects())if(r.width>0){
        const base=r.y-origin.y+(r.height-area)/2+m.fontBoundingBoxAscent;
        result.push({x:r.x-origin.x,y:base-m.actualBoundingBoxAscent,w:r.width,h:m.actualBoundingBoxAscent+m.actualBoundingBoxDescent});
      }
    }
  }
  return result;
}
// Owner decision 46 (28.09.2026): two texts collide where the ink of their glyphs meets (qInk), not where their frames do.
// The frames compared by model.audit_document gave 36 overlaps in 21 of 48 issued variants (QS3 and the pitch, REP-168): a
// title frame of the layout reaches into the text under it while its lines end above it; and they said nothing of the title
// of the pitch cover, whose four lines in the real font ran into the subtitle.
const Q_INK_GAP=1;
function qInkBox(glyphs){
  if(!glyphs.length)return null;
  const x=Math.min(...glyphs.map(g=>g.x)),y=Math.min(...glyphs.map(g=>g.y));
  return {x,y,w:Math.max(...glyphs.map(g=>g.x+g.w))-x,h:Math.max(...glyphs.map(g=>g.y+g.h))-y};
}
function qTexts(slide,canvas){return slide.elements.filter(o=>o.type==='text'&&!o.ghost&&qTextNode(canvas,o.id));}
function qInkOf(canvas,e){const g=qInk(canvas,qTextNode(canvas,e.id));return {g,box:qInkBox(g)};}
function qMeets(a,b){return !!(a.box&&b.box&&qOverlap(a.box,b.box,Q_INK_GAP)&&a.g.some(g=>b.g.some(t=>qOverlap(g,t,Q_INK_GAP))));}
// The first text of the slide whose ink meets the ink of e, or null.
function qCollides(slide,e,canvas){
  const mine=qInkOf(canvas,e);if(!mine.box)return null;
  return qTexts(slide,canvas).find(o=>o!==e&&qMeets(mine,qInkOf(canvas,o)))||null;
}
// Every pair of texts of the slide whose ink meets, [a, b] with a the text a repair would change first (qMover).
function qCollisions(slide,canvas){
  const texts=qTexts(slide,canvas),ink=new Map(texts.map(e=>[e.id,qInkOf(canvas,e)])),pairs=[];
  for(let i=0;i<texts.length;i++)for(let j=i+1;j<texts.length;j++)
    if(qMeets(ink.get(texts[i].id),ink.get(texts[j].id)))pairs.push(qMover(slide,texts[i],texts[j]));
  return pairs;
}
// Which of two colliding texts gives way: the title, then the subtitle, then a text of the product that is neither locked,
// decor of the template nor the footer; of two such, the lower one (the text under a card label, the second of two columns).
function qRank(e){return e.locked||e.decor?9:e.id==='title'?0:e.id==='subtitle'?1:['disclosure','page'].includes(e.id)?5:2;}
function qMover(slide,a,b){const ra=qRank(a),rb=qRank(b);return ra<rb||ra===rb&&a.y>=b.y?[a,b]:[b,a];}
// Clear space between the ink of two texts a repair leaves: a quarter of the size of the text that moves, at least 4 px.
function qInkGap(e){return Math.max(4,e.font_size*.25);}
// Height a text above another may take from the top of its frame to clear the ink of the other (for title_fitter, titles.py).
function qRoomAbove(canvas,e,o){const theirs=qInkOf(canvas,o).box;return theirs&&e.y<theirs.y?+(theirs.y-qInkGap(e)-e.y).toFixed(1):null;}
// Owner decision 46, step 1: the whole frame of e moves off the ink of o, up when e stands above o (a title of the cover grows
// upward, as its template sets it over the subtitle), down when below, by what its own ink needs. The frame keeps its size
// and anchor, so PowerPoint sets the lines where the preview does. It may not reach an object it did not reach before (text,
// logo, chart, table, picture of the content; not the background decor of the template it already stood on), must stay on
// the slide above the footer, and every check of the text must pass at the new place with no other collision.
function qMoveOff(doc,slide,e,o,canvas,plate,parents){
  if(e.locked||e.decor||e.type!=='text')return null;
  const node=qTextNode(canvas,e.id),keep=qBox(e),mine=qInkOf(canvas,e).box,theirs=qInkOf(canvas,o).box;
  if(!mine||!theirs)return null;
  const gap=qInkGap(e),up=mine.y+mine.h/2<=theirs.y+theirs.h/2;
  const dy=up?theirs.y-gap-(mine.y+mine.h):theirs.y+theirs.h+gap-mine.y;
  if(up?dy>=0:dy<=0)return null;
  const box={...keep,y:keep.y+dy},inkTop=mine.y+dy,inkBottom=mine.y+mine.h+dy;
  const footer=Math.min(doc.height,...slide.elements.filter(x=>x!==e&&['disclosure','page'].includes(x.id)).map(x=>x.y));
  if(inkTop<Math.max(4,doc.height*.02)||inkBottom>(['disclosure','page'].includes(e.id)?doc.height:footer)-2)return null;
  const blocks=x=>x!==e&&x!==o&&!x.background_decoration&&!(x.template_decoration&&x.type==='shape')&&(x.type!=='image'||qLogo(doc,x)||!x.template_decoration)&&!qContains(x,keep);
  if(slide.elements.some(x=>blocks(x)&&!qOverlap(keep,x,0)&&qOverlap(box,x,0)))return null;
  qPlace(node,e,box);
  if(!qInspectText(doc,slide,e,canvas,plate,parents.get(e.id)).errors.length&&!qCollides(slide,e,canvas))return {before:keep,after:qBox(e)};
  qPlace(node,e,keep);return null;
}
// Step 2: a smaller size of the scale of the template, not below the smallest size of the scale nor the minimum of the repairs
// (qFontMin), first in its own frame, then moved off as in step 1. Step 3 (a shorter title) is title_fitter's (titles.py):
// a content title still colliding after the check gets a budget of the room above the text it meets.
function qShrinkOff(doc,slide,e,o,canvas,plate,parents,scale){
  if(e.locked||e.decor||e.type!=='text'||!scale.length)return null;
  const node=qTextNode(canvas,e.id),keep=qBox(e),floor=Math.max(scale[0],qFontMin(e));
  for(const size of scale.filter(v=>v<e.font_size-.01&&v>=floor-.01).reverse()){
    qPlace(node,e,{...keep,font_size:size});
    if(!qCollides(slide,e,canvas)&&!qInspectText(doc,slide,e,canvas,plate,parents.get(e.id)).errors.length)return {before:keep,after:qBox(e)};
    const moved=qMoveOff(doc,slide,e,o,canvas,plate,parents);
    if(moved)return {before:keep,after:moved.after};
  }
  qPlace(node,e,keep);return null;
}
// Owner decision 46: collisions are repaired before the deck is issued, in the order the owner set: the frame moves off the
// other text (step 1), a smaller size of the scale (step 2), a shorter title (step 3, titles.py), the subtitle of the cover
// moves down (step 4). A pair no step repairs stays a finding (TEXT_COLLISION).
const Q_COLLISION_ROUNDS=8;
function qResolveCollisions(doc,slide,canvas,plate,parents,scale,patches){
  const given=new Set();
  for(let round=0;round<Q_COLLISION_ROUNDS;round++){
    const pair=qCollisions(slide,canvas).find(([e,o])=>!given.has(e.id+'|'+o.id));
    if(!pair)return;
    const [e,o]=pair;let done=null,moved=e;
    done=qMoveOff(doc,slide,e,o,canvas,plate,parents)||qShrinkOff(doc,slide,e,o,canvas,plate,parents,scale);
    if(!done&&slide.role==='cover'&&e.id==='title'&&o.id==='subtitle'){done=qMoveOff(doc,slide,o,e,canvas,plate,parents);moved=o;}
    if(done)patches.push({slide:slide.id,element:moved.id,before:done.before,after:done.after,reasons:['TEXT_COLLISION'],candidates_tried:round+1});
    else given.add(e.id+'|'+o.id);
  }
}
function qInspectText(doc,slide,e,canvas,plate,parent=qParent(slide,e)){
  const node=qTextNode(canvas,e.id),glyphs=qGlyphs(canvas,node),errors=[];
  // textOverflow: renderer.js, loaded before this file.
  const fit=textOverflow(node);
  if(fit.overflow)errors.push({code:'TEXT_OVERFLOW',height:fit.height,box:e.h,width:node.scrollWidth});
  // Owner decision 48: the colours under the points that do not read, for the fixer readable-colour (their median).
  const ink=qLuminance(qRgb(e.color));let bad=0,min=21,samples=0,under2=0;const lows=[[],[],[]];
  for(const r of glyphs){
    let low=0,count=0;
    // Leave clear space for differences between browser and PPTX font metrics.
    const padX=e.font_size*.12,padY=e.font_size*.06;
    for(const dx of [0,.25,.5,.75,1])for(const dy of [.2,.5,.8]){
      const x=Math.floor(r.x-padX+(r.w+2*padX)*dx),y=Math.floor(r.y-padY+(r.h+2*padY)*dy);
      if(x<0||x>=plate.width||y<0||y>=plate.height)continue;
      const i=(y*plate.width+x)*4;const contrast=qRatio(ink,qLuminance([...plate.pixels.slice(i,i+3)]));
      min=Math.min(min,contrast);count++;if(contrast<Q.contrast){low++;for(let k=0;k<3;k++)lows[k].push(plate.pixels[i+k]);}
      samples++;if(contrast<2)under2++;
    }
    if(count&&low)bad++;
  }
  // Holdout 25.09 (H2b): a pair of colours of the template itself that the composer kept (template-colour-kept, at least
  // 2:1) is the reference's own colour (C3): reported as a warning and not repaired. Item 37, cause 3: on a picture that
  // fills the slide the composer cannot measure; the pair holds where at most 5 % of the points under the text are below 2:1.
  const own=(e.style_adjustments||[]).some(a=>a.reason==='template-colour-kept'&&(a.background!=='picture'||under2<=samples*.05));
  const median=a=>[...a].sort((x,y)=>x-y)[a.length>>1];
  const background=lows[0].length?[0,1,2].map(k=>median(lows[k]).toString(16).padStart(2,'0')).join('').toUpperCase():null;
  if(bad>=1)errors.push({code:'LOW_RENDERED_CONTRAST',bad_glyph_regions:bad,total_glyph_regions:glyphs.length,min_contrast:+min.toFixed(3),background,...(own?{severity:'warning'}:{})});
  const logos=slide.elements.filter(s=>qLogo(doc,s)),drawn=logos.length?qInk(canvas,node):[];
  const hit=logos.find(logo=>drawn.some(g=>qOverlap(g,logo,2)));
  if(hit)errors.push({code:'BRAND_ASSET_OVERLAP',asset:hit.id});
  // Long paragraphs need one spare line because renderer metrics can wrap earlier.
  const lineCount=new Set(glyphs.map(g=>Math.round(g.y))).size;
  const bottomGap=12+(lineCount>=3?e.font_size*1.35:0);
  if(parent&&glyphs.some(g=>g.x<parent.x+8||g.x+g.w>parent.x+parent.w-8||g.y<parent.y+4||g.y+g.h>parent.y+parent.h-bottomGap))errors.push({code:parent.raster?'RASTER_SURFACE_MARGIN':'CARD_TEXT_MARGIN',surface:parent.id,bounds:{x:parent.x,y:parent.y,w:parent.w,h:parent.h}});
  return {errors,glyph_count:glyphs.length,min_contrast:+min.toFixed(3)};
}
function qCanPlace(doc,slide,e,box){
  if(box.w<Math.min(30,e.w)||box.h<10||box.x<0||box.y<0||box.x+box.w>doc.width+.1||box.y+box.h>doc.height+.1)return false;
  return !slide.elements.some(other=>other.id!==e.id&&(['text','table','chart'].includes(other.type)||other.ghost)&&qOverlap(box,other,2));
}
const Q_FOOTER_TOP=.8;
// Whether the backplate under a box is one colour: a footer moved or recoloured must not stand over a line of the picture
// (the deck 29 address: black over blue letters passes the contrast sampling and still overprints them).
function qPlain(plate,box){
  const points=[];
  for(const fx of [.02,.15,.3,.45,.6,.75,.9,.98])for(const fy of [.1,.5,.9]){
    const x=Math.floor(box.x+box.w*fx),y=Math.floor(box.y+box.h*fy);
    if(x>=0&&y>=0&&x<plate.width&&y<plate.height){const i=(y*plate.width+x)*4;points.push([0,1,2].map(k=>plate.pixels[i+k]));}
  }
  if(!points.length)return false;
  const median=[0,1,2].map(k=>points.map(p=>p[k]).sort((a,b)=>a-b)[Math.floor(points.length/2)]);
  return points.every(p=>Math.max(...p.map((v,k)=>Math.abs(v-median[k])))<=30);
}
function qCandidates(doc,slide,e,parent=qParent(slide,e)){
  const original={x:e.x,y:e.y,w:e.w,h:e.h,font_size:e.font_size},candidates=[],scale=qScale(doc);
  // Smaller sizes of the scale of the template (without one, steps of the size), the minimum last.
  const min=qFontMin(e),sizes=scale.length?[e.font_size,...scale.filter(v=>v<e.font_size-.01&&v>=min-.01).reverse()]
    :[1,.94,.88,.80,.72,.64,.56].map(k=>Math.max(min,Math.round(e.font_size*k*2)/2));sizes.push(min);
  function add(box){for(const size of new Set(sizes)){
    const candidate={...box,font_size:size};if(!qCanPlace(doc,slide,e,candidate))continue;
    const cost=Math.abs(box.x-e.x)/doc.width+Math.abs(box.y-e.y)/doc.height+Math.abs(box.w-e.w)/doc.width+Math.abs(box.h-e.h)/doc.height*.15+(e.font_size-size)/e.font_size*1.4;
    candidates.push({candidate,cost});
  }}
  add(original);
  if(parent){
    for(const y of new Set([Math.max(e.y,parent.y+12),parent.y+12]))add({x:parent.x+12,y,w:parent.w-24,h:parent.y+parent.h-12-y});
    for(const n of [0,1,2,3,4]){
      const y=Math.max(parent.y+12,e.y-n*e.font_size*1.35);
      const x=Math.max(e.x,parent.x+12),w=Math.min(e.w,parent.x+parent.w-12-x);
      add({x,y,w,h:Math.min(e.h,parent.y+parent.h-12-y)});
      add({x,y,w,h:parent.y+parent.h-12-y});
    }
  }
  else{
    const coverText=slide.role==='cover'&&['title','subtitle'].includes(e.id);
    const widths=coverText?[1,.88,.75,.62,.50]:[1];
    const ys=coverText?[e.y,Math.max(doc.height*.18,e.y-doc.height*.08),Math.max(doc.height*.18,e.y-doc.height*.16),e.y+doc.height*.08]:[e.y];
    for(const factor of widths)for(const y of new Set(ys)){
      const box={x:e.x,y,w:e.w*factor,h:doc.height-26-y};
      for(const other of slide.elements){if(other.id!==e.id&&['text','table','chart'].includes(other.type)&&other.y>=y+2&&other.x<box.x+box.w&&other.x+other.w>box.x)box.h=Math.min(box.h,other.y-y-10);}
      add(box);
    }
  }
  if(e.id==='disclosure'||e.id==='page'){
    const logos=slide.elements.filter(s=>qLogo(doc,s));
    // M7 of holdout-next (МГПУ, 25.09.2026): the disclosure also tries the place right of the decor of the template it starts
    // on (the red stripe along the left edge, 103 px), not only right of a logo.
    const under=e.id==='disclosure'?slide.elements.filter(s=>s!==e&&s.template_decoration&&s.x<=e.x+1&&s.x+s.w>e.x&&s.x+s.w<doc.width*.3
      &&s.y<e.y+e.h&&s.y+s.h>e.y).map(s=>s.x+s.w+12):[];
    const xs=e.id==='page'?[e.x,doc.width-e.w-12,Math.max(0,e.x-36),...logos.map(s=>Math.max(8,s.x-e.w-12))]:[e.x,...logos.map(s=>s.x+s.w+12),...under];
    // Proposals of 24.09.2026: a footer a background picture draws its own line under (deck 29: the address baked into the
    // picture of the slide) also tries positions above it, step by step up to Q_FOOTER_TOP of the slide, nearest first.
    const ys=[e.y,doc.height-40,doc.height-58];
    for(let y=Math.min(e.y,doc.height-58)-(e.h+4);y>=doc.height*Q_FOOTER_TOP;y-=e.h+4)ys.push(y);
    // The footer is the product's own text: on a picture the colour of the title (composer, end) may not read where the
    // footer stands (deck 29: white as the title on the blue header, over the white field). It also tries the colours of
    // the other texts of the slide, then neutral ones; a changed colour costs more than a nearby place.
    const colours=[...new Set([e.color,...slide.elements.filter(o=>o.type==='text'&&o!==e&&!['disclosure','page'].includes(o.id)).map(o=>o.color),'151515','FFFFFF'])];
    for(const colour of colours)for(const x of xs)for(const y of ys){
      // A place right of decor keeps the right end of the disclosure (the page number stands beyond it).
      const box={x,y,w:under.includes(x)&&x>e.x?Math.min(e.w,e.x+e.w-x):Math.min(e.w,doc.width-x-(e.id==='page'?8:50)),h:e.h};
      if(colour===e.color){add(box);continue;}
      if(!qCanPlace(doc,slide,e,{...box,font_size:e.font_size}))continue;
      candidates.push({candidate:{...box,font_size:e.font_size,color:colour},cost:.6+Math.abs(x-e.x)/doc.width+Math.abs(y-e.y)/doc.height});
    }
  }
  const seen=new Set();return candidates.sort((a,b)=>a.cost-b.cost).map(c=>c.candidate).filter(c=>{const key=JSON.stringify(c);if(seen.has(key))return false;seen.add(key);return true;}).slice(0,160);
}
// Item 35, H2g (holdout 25.09.2026, deck 34): a title or subtitle of the product that reads at none of its places in its own
// colour (the white text of the frame of the sample cover, whose pictures are not carried: white on the white slide) takes a
// colour of the template that reads there: the colour most titles of the other slides have, the colours of the other texts
// of the slide, the palette of the template nearest to its own colour, then neutral ones. The colour of the template keeps
// priority: this is tried only after every place in the own colour failed.
// Item 37, cause 5: text that may take a colour of the template where it reads nowhere in its own — a title or subtitle
// (H2g), text in a layout placeholder and text the product sets itself; not the footer (qCandidates gives it colours), not
// text of decor or text cloned from a sample slide (the exporter keeps the colour of its runs). quality.recolourable agrees.
function qRecolourable(e){const kind=(e.binding||{}).kind;
  return ['title','subtitle'].includes(e.id)||(e.type==='text'&&!['disclosure','page'].includes(e.id)&&!e.decor&&(kind==='placeholder'||kind==='native'||kind===undefined));}
function qRecolours(doc,slide,e){
  const up=c=>String(c||'').toUpperCase(),counts=new Map();
  for(const s of doc.slides)if(s!==slide){const t=s.elements.find(x=>x.id==='title'&&x.type==='text');if(t)counts.set(up(t.color),(counts.get(up(t.color))||0)+1);}
  const titles=[...counts].sort((a,b)=>b[1]-a[1]||(a[0]<b[0]?-1:1)).map(c=>c[0]);
  const texts=slide.elements.filter(o=>o.type==='text'&&o!==e&&!o.decor&&!['disclosure','page'].includes(o.id)).map(o=>up(o.color));
  const own=qRgb(up(e.color)),distance=c=>qRgb(c).reduce((sum,v,i)=>sum+(v-own[i])**2,0);
  const palette=(doc.template?.colours||[]).map(up).filter(c=>/^[0-9A-F]{6}$/.test(c)).sort((a,b)=>distance(a)-distance(b)||(a<b?-1:1));
  const first=e.id==='title'?[...titles,...texts]:[...texts,...titles];
  return [...new Set([...first,...palette,'151515','FFFFFF'])].filter(c=>/^[0-9A-F]{6}$/.test(c)&&c!==up(e.color));
}
// Deck quality (23.09.2026, seventh session): the sizes the check sets come from the scale of the template, the sizes of
// its layout placeholders, card texts and slides (doc.template.sizes_pt; Appendix 1 of the case: "кегль не из
// типографической шкалы шаблона"). Without a scale (standalone export) the steps of 0.5 px of A8 stay.
function qScale(doc){return [...new Set((doc.template?.sizes_pt||[]).map(v=>+(v*4/3).toFixed(3)))].sort((a,b)=>a-b);}
function qOnScale(scale,size){return !scale.length||scale.some(v=>Math.abs(v-size)<=2/3+.01);}
// Own size of a text: of its layout placeholder, of the paragraph of the sample card it was cloned from, or of the frame the
// composer took it from (size_own; the size itself for older documents).
function qOwn(e){const band=(e.style_adjustments||[]).find(a=>a.reason==='title-into-band');if(band&&band.own_px)return band.own_px;const b=e.binding||{};return b.kind==='placeholder'&&b.typography?.size_pt?b.typography.size_pt*4/3:b.kind==='clone'&&b.size_pt?b.size_pt*4/3:e.size_own||e.font_size;}
function qPlace(node,e,box){Object.assign(e,box);Object.assign(node.style,{left:e.x+'px',top:e.y+'px',width:e.w+'px',height:e.h+'px',fontSize:e.font_size+'px'});}
function qBox(e){return {x:e.x,y:e.y,w:e.w,h:e.h,font_size:e.font_size};}
// Height the text of a node takes (placeholder text: its paragraph block and insets; plain text: its content).
function qNeed(node){
  if(node.dataset.layout==='paragraphs')return textOverflow(node).height;
  const h=node.style.height;node.style.height='auto';const need=node.offsetHeight;node.style.height=h;return need;
}
// The smallest frame within `room` that holds the text 4 % larger (headroom for the line breaks of PowerPoint), not lower
// than `least`. Top-anchored text keeps its top; bottom-anchored text keeps its bottom while the room above allows, then
// grows down (template A: a title of the sample in a narrow column beside cards). Leaves the node as it was.
function qTightBox(e,node,room,size,least){
  const keep=qBox(e);qPlace(node,e,{...room,font_size:size*Q_GROW_HEADROOM});
  const h=Math.min(room.h,Math.max(least,Math.ceil(qNeed(node))+1));qPlace(node,e,keep);
  const y=(e.anchor||'t')==='b'?Math.max(room.y,Math.min(keep.y+keep.h,room.y+room.h)-h):room.y;
  return {x:room.x,y,w:room.w,h};
}
// A8, Q2: text grows along the scale while the browser fits it 4 % larger and no check fails. Text of a layout placeholder
// or of a sample card grows back toward its own size (A8: the composer estimates lines conservatively); a fact grows further
// on a slide with room for it (the slides of a brief with few facts were three quarters empty, K3): up to Q_GROW of its own
// size, Q_GROW_TITLE of the title of its slide and Q_GROW_MAX_PT on a slide 1280 px wide, below the labels and key numbers
// of its slide. Texts of one slide with the same start size, own size, limit and role (two body columns, the cards of a
// slide) keep one size. A card text grows down to the lowest line its card planner allows (grow_bottom, sample_clone.py).
const Q_GROW_HEADROOM=1.04,Q_GROW=1.5,Q_GROW_TITLE=.75,Q_GROW_MAX_PT=24;
function qGrowCap(doc,slide,e,scale){
  const own=qOwn(e);if(!e.fact_ids?.length||!scale.length)return own;
  const title=slide.elements.find(x=>x.id==='title'&&x.type==='text');
  // Strictly below a label or key number: by more than the tolerance of the sizes of the scale (qGrowSizes, +0.01 px).
  const heads=slide.elements.filter(x=>x.type==='text'&&(x.label_of||x.metric_of)).map(x=>x.font_size-.5);
  return Math.max(own,Math.min(own*Q_GROW,title?title.font_size*Q_GROW_TITLE:Infinity,Q_GROW_MAX_PT*4/3*doc.width/1280,...heads));
}
function qGrowGroups(doc,slide,scale){
  const groups=new Map();
  for(const e of slide.elements){
    if(e.type!=='text'||e.locked||['disclosure','page'].includes(e.id)||scale.length&&e.id==='title'&&slide.role!=='cover')continue;
    const b=e.binding||{};
    if(!(b.kind==='placeholder'&&b.typography?.size_pt||scale.length&&(b.kind==='clone'&&b.size_pt||e.fact_ids?.length)))continue;
    const cap=qGrowCap(doc,slide,e,scale);if(e.font_size>=cap-.25)continue;
    const key=[e.font_size,qOwn(e),cap.toFixed(2),e.content_role?.role||'',b.kind||''].join('|');groups.set(key,[...(groups.get(key)||[]),e]);
  }
  return [...groups.values()];
}
function qGrowSizes(e,cap,scale){
  if(scale.length)return scale.filter(v=>v>e.font_size+.25&&v<=cap+.01).reverse();
  const sizes=[];for(let size=Math.floor(cap*2)/2;size>e.font_size+.25;size-=.5)sizes.push(size);return sizes;
}
function qRoom(e){return e.grow_bottom>e.y+e.h?{x:e.x,y:e.y,w:e.w,h:e.grow_bottom-e.y}:{x:e.x,y:e.y,w:e.w,h:e.h};}
function qSetSize(canvas,e,size){e.font_size=size;qTextNode(canvas,e.id).style.fontSize=size+'px';}
function qFitsAt(doc,slide,e,canvas,plate,parent,size,room=qRoom(e)){
  const node=qTextNode(canvas,e.id),keep=qBox(e);
  qPlace(node,e,{...room,font_size:size*Q_GROW_HEADROOM});const roomy=!textOverflow(node).overflow;
  qPlace(node,e,{...room,font_size:size});
  // Owner decision 46: a text grows only where its glyphs keep off every other text of the slide.
  const ok=roomy&&qUnitsFit(doc,node,e,size,room.w)&&!qInspectText(doc,slide,e,canvas,plate,parent).errors.length&&!qCollides(slide,e,canvas);
  qPlace(node,e,keep);return ok;
}
// Q1: the titles of the content slides that share an own size (of the placeholder or of the frame of the sample) and a
// frame (one title style of the reference) take one size of the scale: the largest at which Q_TITLE_SHARE of them fit 4 %
// larger with no finding, in their frames grown into free space (qTitleRoom). A longer title takes the largest size of the
// scale at which it fits (a title of 90 signs must not shrink the other ten; a narrow title beside cards must not shrink
// the wide ones of the deck, K3 template A "split"); a title that fits at no size keeps the size of the composer.
function qTitleRoom(doc,slide,e,size){
  // Down to the next object across its width (keeping a gap), within the slide margins; bottom-anchored text also up; a
  // frame of centred text keeps its place.
  const anchor=e.anchor||'t',gap=Math.max(8,size*.3),box={x:e.x,y:e.y,w:e.w,h:e.h};
  if(anchor==='ctr')return box;
  const across=slide.elements.filter(o=>o!==e&&o.x<e.x+e.w&&o.x+o.w>e.x&&!qContains(o,box));
  const bottom=Math.max(e.y+e.h,Math.min(doc.height*.9,qBandBottom(doc,slide,e,size),...across.filter(o=>o.y>=e.y+e.h-1).map(o=>o.y-gap)));
  const top=anchor==='b'?Math.min(e.y,Math.max(doc.height*.03,...across.filter(o=>o.y+o.h<=e.y+1).map(o=>o.y+o.h+gap))):e.y;
  return {...box,y:top,h:bottom-top};
}
const Q_TITLE_SHARE=.8,Q_TITLE_STEP=.8;
// Item 37, cause 1 (holdout-next 25.09.2026): a title that fits nowhere near its own size in qTitleRoom (УУНиТ: 12 pt of 28
// in a frame of 379 x 44 px, the line under it at 81 px, free space to the logo on its right; Indiana: 14 pt of 43 in the
// centred frame of the left column, empty below it) may also take the free space on its right when it is set flush left in a
// frame narrower than half the slide, and the space below when it is centred; the top stays, and qTightBox makes the frame
// only as tall as the text. The right edge: the nearest object beside the frame across its height, or the margin of its
// left edge mirrored.
const Q_TITLE_SHRUNK=.85;
function qTitleRoomWide(doc,slide,e,size){
  const gap=Math.max(8,size*.3),box=qTitleRoom(doc,slide,e,size);
  if((e.anchor||'t')==='ctr'){
    const across=slide.elements.filter(o=>o!==e&&o.x<e.x+e.w&&o.x+o.w>e.x&&!qContains(o,e));
    box.h=Math.max(e.y+e.h,Math.min(doc.height*.9,...across.filter(o=>o.y>=e.y+e.h-1).map(o=>o.y-gap)))-box.y;
  }
  // Only a frame narrower than half the slide grows right: a wider one is the width the template means for its titles (template
  // B: 822 of 1280 px, the rest of the header is its air, not room for a longer title).
  if((!e.align||e.align==='left')&&e.w<doc.width*.5){
    const beside=slide.elements.filter(o=>o!==e&&o.y<box.y+box.h&&o.y+o.h>box.y&&o.x>=e.x+e.w-1&&!qContains(o,box));
    // Session 16 (deck 20 slide 6): a title on a panel drawn on the slide grows right only up to the edge of the panel.
    const holder=slide.elements.filter(o=>o.type==='shape'&&!o.ghost&&(o.opacity??1)>=.9&&o.fill&&qContains(o,e)&&o.x+o.w<doc.width-2).sort((a,b)=>a.w*a.h-b.w*b.h)[0];
    box.w=Math.max(e.w,Math.min(doc.width-e.x,holder?holder.x+holder.w-gap:Infinity,...beside.map(o=>o.x-gap))-e.x);
  }
  return box;
}
// Item 37, cause 1: on a content slide whose title the template had to set below Q_TITLE_SHRUNK of its own size, the text of
// the facts is not set larger than the title (МГПУ: title 20 pt, text 28 pt): it takes the largest size of the scale not above
// the title, no smaller than the minimum of the repairs, where it shows no finding. A title in its own size keeps the
// hierarchy of the template (deck 22: titles of 20 pt over text of 28 pt by its own sizes).
function qHierarchy(doc,slide,canvas,plate,parents,scale,patches){
  const t=slide.role!=='cover'&&slide.elements.find(x=>x.id==='title'&&x.type==='text');
  if(!t||t.font_size>=qOwn(t)*Q_TITLE_SHRUNK)return;
  for(const e of slide.elements){
    if(e.type!=='text'||!e.fact_ids?.length||e.label_of||e.metric_of||e.locked||e.decor||e.font_size<=t.font_size+.25)continue;
    // Only where the template itself sets the title larger than this text (deck 22: titles of 20 pt over text of 28 pt by its
    // own sizes; a title of 16 pt there is no reason to set the text in 16 pt).
    if(qOwn(e)>=qOwn(t))continue;
    const size=scale.filter(v=>v<=t.font_size+.01&&v>=qFontMin(e)-.01).at(-1);
    if(size===undefined||size>=e.font_size-.01)continue;
    const node=qTextNode(canvas,e.id),before=qBox(e);
    qPlace(node,e,{...before,font_size:size});
    if(qInspectText(doc,slide,e,canvas,plate,parents.get(e.id)).errors.length){qPlace(node,e,before);continue;}
    patches.push({slide:slide.id,element:e.id,before,after:qBox(e),reasons:['TITLE_HIERARCHY'],candidates_tried:1});
  }
}
async function qTitles(doc,host,plates,parents,scale,patches){
  // {slide id: the largest size qTitlePush gives its title} for the titles left to it (item 40).
  const groups=new Map(),pushes=new Map();
  for(const slide of doc.slides){
    const e=slide.role!=='cover'&&slide.elements.find(x=>x.id==='title'&&x.type==='text'&&!x.locked);
    if(e){const key=[qOwn(e).toFixed(2),Math.round(e.w/8),Math.round(e.h/8),e.anchor||'t'].join('|');groups.set(key,[...(groups.get(key)||[]),slide]);}
  }
  for(const [key,slides] of groups){
    const items=slides.map(slide=>{const e=slide.elements.find(x=>x.id==='title');
      return {slide,e,canvas:[...host.children].find(s=>s.dataset.slide===slide.id),plate:plates.get(slide.id),parent:parents.get(slide.id).get('title'),fit:new Map()};});
    // Down to the title minimum of the repairs (28 px) or the size the composer already took, and one step of the scale below —
    // unless that step is far below (holdout-next2, Потсдам: the scale 8, 18, 20… pt; titles of 15-18 pt in the composer
    // fell to 8 pt): a step under Q_TITLE_STEP of that size is no floor, and a title that fits at no size of the scale above
    // it keeps the size of the composer.
    const least=Math.min(28,...items.map(i=>i.e.font_size)),step=Math.max(0,...scale.filter(v=>v<=least+.01));
    const floor=step>=least*Q_TITLE_STEP?step:least*Q_TITLE_STEP;
    const sizes=scale.filter(v=>v<=qOwn(items[0].e)+.01&&v>=floor-.01).reverse();
    if(!sizes.length)continue;
    function fits(item,size){
      // The frame the title takes at a size: the smallest one in its room that holds it 4 % larger, or null.
      if(!item.fit.has(size)){
        const {e,slide,canvas,plate,parent}=item,node=qTextNode(canvas,e.id),keep=qBox(e),room=(item.wide?qTitleRoomWide:qTitleRoom)(doc,slide,e,size);
        let box=qTightBox(e,node,room,size,keep.h);
        if(!qFitsAt(doc,slide,e,canvas,plate,parent,size,box))box=null;
        qPlace(node,e,keep);item.fit.set(size,box);
      }
      return item.fit.get(size);
    }
    for(const item of items){
      item.best=sizes.find(s=>fits(item,s));
      // Item 37, cause 1: a title that fits nowhere near its own size tries the wide room (qTitleRoomWide) and keeps it only
      // where it holds the title larger.
      if(item.best===undefined||item.best<qOwn(item.e)*Q_TITLE_SHRUNK){
        const narrow=item.fit;item.fit=new Map();item.wide=true;
        const best=sizes.find(s=>fits(item,s));
        if(best!==undefined&&(item.best===undefined||best>item.best))item.best=best;else{item.fit=narrow;item.wide=false;}
      }
      // Item 40: a title still far below its own size with content of its column under it tries qTitlePush without moving
      // anything; the size it would reach there counts in the shared size of the group instead of its size in its room (deck 11
      // split: 7 such titles of 11 held the 4 that fit at 30 pt down to 14 pt). A title that cannot push counts as before
      // (regression hr4: all titles with content under them left out of the share let the other titles of ТПУ, FNS, Office
      // Training grow; one title that could not push, left alone in the share, held deck 10 at 19 pt).
      item.push=null;
      if(item.best!==undefined&&item.best<qOwn(item.e)*Q_TITLE_SHRUNK&&qPushContent(doc,item.slide,item.e))
        item.push=await qTitlePush(doc,item.slide,item.canvas,item.plate,parents.get(item.slide.id),qOwn(item.e),null,item.best);
    }
    const bests=items.map(i=>i.push||i.best).filter(s=>s!==undefined).sort((a,b)=>b-a);
    if(!bests.length)continue;
    const shared=bests[Math.min(bests.length,Math.ceil(items.length*Q_TITLE_SHARE))-1];
    for(const item of items.filter(i=>i.push&&i.push>i.best))pushes.set(item.slide.id,Math.min(item.push,shared));
    for(const item of items.filter(i=>i.best!==undefined)){
      // A size under the best one fits as a rule; if the room check says otherwise, the best one stays.
      const {e,canvas}=item,size=fits(item,Math.min(shared,item.best))?Math.min(shared,item.best):item.best,before=qBox(e),box=fits(item,size),after={...box,font_size:size};
      if(Object.keys(after).every(k=>Math.abs(after[k]-before[k])<.01))continue;
      qPlace(qTextNode(canvas,e.id),e,after);
      patches.push({slide:item.slide.id,element:e.id,before,after:{...after},reasons:['TITLE_SCALE'],candidates_tried:item.fit.size});
    }
  }
  return pushes;
}
// Item 40 (holdout-next2 26.09.2026, deck 11 split and columns): the template sets its titles in a narrow frame with the text of
// its column right under it (slide 12: 408 x 76 px for 30 pt), and a title of 60-70 signs fell to 14 pt under text of 28 pt:
// qTitles has no free room there. Such a title — top-anchored, still below Q_TITLE_SHRUNK of its own size — takes up to
// Q_PUSH_LINES lines at the largest size of the scale up to the size qTitles shares in its group, and the content of its
// column moves down by what the frame grew: its bottom stays within the footer and the objects that stay. Every text of the
// slide it touches must then fit and read on a fresh backplate; otherwise nothing moves and a smaller size is tried.
const Q_PUSH_LINES=3;
// The content qTitlePush moves: the movable objects under the title across its width; null where there is none, or where
// one of them may not move (a cloned card keeps the frames of its sample parts, a locked text its place; a card of the
// product moves, as in G3c).
function qPushContent(doc,slide,e){
  const under=slide.elements.filter(o=>o!==e&&o.x<e.x+e.w-1&&o.x+o.w>e.x+1&&o.y>=e.y+e.h-1&&!['disclosure','page'].includes(o.id));
  const content=under.filter(o=>qMovable(doc,o));
  if(!content.length||content.some(o=>(o.binding||{}).kind==='clone'||o.type==='text'&&o.locked))return null;
  return content;
}
// patches null: a trial — the size it would give, with nothing moved; above: the size the title already has in its room.
async function qTitlePush(doc,slide,canvas,plate,parents,cap,patches,above=null){
  const e=slide.role!=='cover'&&slide.elements.find(x=>x.id==='title'&&x.type==='text'&&!x.locked),floor=above??e?.font_size;
  if(!e||cap===undefined||(e.anchor||'t')!=='t'||floor>=qOwn(e)*Q_TITLE_SHRUNK)return null;
  const content=qPushContent(doc,slide,e);
  if(!content)return null;
  const scale=qScale(doc),node=qTextNode(canvas,e.id),keep=qBox(e),[,top,,bottom]=e.inset||[0,0,0,0];
  // The lowest line each moved object may reach: the footer, the edge of the slide, an object that stays under it.
  const footer=slide.elements.filter(o=>['disclosure','page'].includes(o.id)).map(o=>o.y);
  const stay=slide.elements.filter(o=>o!==e&&!content.includes(o)&&!o.background_decoration&&!['disclosure','page'].includes(o.id));
  const limit=o=>Math.min(doc.height*.92,...footer,...stay.filter(s=>s.x<o.x+o.w&&s.x+s.w>o.x&&s.y>=o.y+o.h-1).map(s=>s.y))-6;
  const place=(o,box)=>{Object.assign(o,box);const n=qTextNode(canvas,o.id);if(n)Object.assign(n.style,{left:o.x+'px',top:o.y+'px',width:o.w+'px',height:o.h+'px'});};
  const parentOf=t=>slide.elements.filter(s=>s.type==='shape'&&!s.background_decoration&&!s.ghost&&qContains(s,t)).sort((a,b)=>a.w*a.h-b.w*b.h)[0];
  let tried=0;
  for(const size of scale.filter(v=>v<=Math.min(cap,qOwn(e))+.01&&v>floor+.01).reverse()){
    tried++;
    const room={x:keep.x,y:keep.y,w:keep.w,h:Math.max(keep.h,Math.ceil(Q_PUSH_LINES*size*(e.line_height||1.2)+top+bottom)+2)};
    // Session 16 (B2): a title in a filled band of the slide grows only within the band.
    const inBand=qBandBottom(doc,slide,e,size)-keep.y;
    if(room.y+room.h>keep.y+inBand){if(inBand<keep.h)continue;room.h=inBand;}
    const box=qTightBox(e,node,room,size,keep.h);
    // The grown frame keeps off what stays under the title (a line of the layout under it), except what holds the title.
    if(stay.some(s=>!qContains(s,keep)&&qOverlap(box,s,0))||!qFitsAt(doc,slide,e,canvas,plate,parents.get(e.id),size,box)){qPlace(node,e,keep);continue;}
    const delta=box.h-keep.h,moves=[];
    for(const o of content){
      const moved={x:o.x,y:o.y+delta,w:o.w,h:o.h},lowest=limit(o);
      if(moved.y+moved.h>lowest)moved.h=lowest-moved.y;
      moves.push([o,{x:o.x,y:o.y,w:o.w,h:o.h},moved]);
    }
    if(moves.some(([o,,m])=>m.h<Math.min(o.h,20))){qPlace(node,e,keep);continue;}
    qPlace(node,e,{...box,font_size:size});for(const [o,,m] of moves)place(o,m);
    const fresh=await qBackplate(doc,slide);
    const texts=moves.map(m=>m[0]).filter(o=>o.type==='text');
    const ok=!qInspectText(doc,slide,e,canvas,fresh,parents.get(e.id)).errors.length&&texts.every(t=>!qInspectText(doc,slide,t,canvas,fresh,parentOf(t)).errors.length);
    if(!ok||!patches){for(const [o,before] of moves)place(o,before);qPlace(node,e,keep);if(ok)return size;continue;}
    patches.push({slide:slide.id,element:e.id,before:keep,after:qBox(e),reasons:['TITLE_PUSH'],candidates_tried:tried});
    for(const [o,before,m] of moves)if(Object.keys(m).some(k=>Math.abs(m[k]-before[k])>=.01))
      patches.push({slide:slide.id,element:o.id,before:o.type==='text'?{...before,font_size:o.font_size}:before,after:o.type==='text'?{...m,font_size:o.font_size}:{...m},reasons:['TITLE_PUSH'],candidates_tried:0});
    return fresh;
  }
  return null;
}
// G3c (proposals of 24.09.2026): a title of a content slide that does not read on the picture of the reference or touches
// its logo (deck 30: the title placeholder of the layout lies over the header baked into the background picture) moves
// down to the first position where it reads and touches no logo; the content under it moves down by what the title needs,
// no lower than the footer, and a moved frame that would pass the footer ends at it. Every moved text must then read and fit
// on a fresh backplate (moved cards change what lies under their texts); otherwise nothing moves. Only a failing title moves:
// a title the reference sets on its own band (deck 29: white on the blue header) reads there and stays.
const Q_TITLE_DOWN=.45;
function qMovable(doc,o){
  return !['title','disclosure','page'].includes(o.id)&&!o.background_decoration&&!o.ghost&&!qLogo(doc,o)&&!['inherited','furniture'].includes((o.binding||{}).kind)
    &&(o.type==='text'||o.type==='table'||o.type==='chart'||o.type==='shape'||o.type==='image'&&!o.template_decoration);
}
async function qTitleOffRaster(doc,slide,canvas,plate,parents,patches){
  const e=slide.role!=='cover'&&slide.elements.find(x=>x.id==='title'&&x.type==='text'&&!x.locked);
  if(!e)return null;
  const failing=r=>r.errors.some(x=>['LOW_RENDERED_CONTRAST','BRAND_ASSET_OVERLAP'].includes(x.code)&&x.severity!=='warning');
  if(!failing(qInspectText(doc,slide,e,canvas,plate,parents.get(e.id))))return null;
  const node=qTextNode(canvas,e.id),keep=qBox(e);
  const content=slide.elements.filter(o=>o!==e&&qMovable(doc,o)&&o.y>=keep.y-1);
  // A cloned card keeps the frames of its sample parts in its binding; it is not moved here.
  if(content.some(o=>(o.binding||{}).kind==='clone'))return null;
  // Q1: a moved title takes the size most content titles of the deck have on the scale of the template, when smaller (cycle
  // p1b, deck 30: qTitles could not size the titles on the header; the scale check later snapped 48-56.5 px to 42.67 and
  // kept 58.67, which is on the scale — 44 pt beside 32 pt in one deck).
  const scale=qScale(doc),snap=v=>scale.length?Math.max(...scale.filter(x=>x<=v+.01),Math.min(...scale)):v,counts=new Map();
  for(const s of doc.slides)if(s.role!=='cover'){const t=s.elements.find(x=>x.id==='title'&&x.type==='text');if(t){const v=+snap(t.font_size).toFixed(2);counts.set(v,(counts.get(v)||0)+1);}}
  const shared=[...counts].sort((a,b)=>b[1]-a[1]||a[0]-b[0])[0]?.[0],size=shared&&shared<e.font_size?shared:e.font_size;
  qPlace(node,e,{...keep,font_size:size});
  const tight=Math.min(keep.h,Math.ceil(qNeed(node))+Math.ceil(size*.3));
  let target=null;
  for(let y=keep.y+4;y<=doc.height*Q_TITLE_DOWN;y+=4){
    qPlace(node,e,{...keep,y,h:tight,font_size:size});
    if(!qInspectText(doc,slide,e,canvas,plate,parents.get(e.id)).errors.length){target=y;break;}
  }
  if(target===null){qPlace(node,e,keep);return null;}
  const footer=slide.elements.filter(o=>['disclosure','page'].includes(o.id)).map(o=>o.y);
  const limit=Math.min(doc.height*.92,...footer)-6,gap=Math.max(12,e.font_size*.4);
  const top=content.length?Math.min(...content.map(o=>o.y)):Infinity,delta=Math.max(0,target+tight+gap-top);
  const moves=[];
  for(const o of content){
    const box={x:o.x,y:o.y+delta,w:o.w,h:o.h};
    if(box.y+box.h>limit)box.h=limit-box.y;
    if(box.h<Math.min(o.h,20)){qPlace(node,e,keep);return null;}
    moves.push([o,{x:o.x,y:o.y,w:o.w,h:o.h},box]);
  }
  const place=(o,box)=>{Object.assign(o,box);const n=qTextNode(canvas,o.id);if(n)Object.assign(n.style,{left:o.x+'px',top:o.y+'px',width:o.w+'px',height:o.h+'px'});};
  for(const [o,,box] of moves)place(o,box);
  const fresh=await qBackplate(doc,slide);
  const texts=[e,...moves.map(m=>m[0]).filter(o=>o.type==='text')];
  const ok=texts.every(t=>!qInspectText(doc,slide,t,canvas,fresh,t===e?parents.get(t.id):qParent(slide,t)).errors.length);
  if(!ok){for(const [o,before] of moves)place(o,before);qPlace(node,e,keep);return null;}
  patches.push({slide:slide.id,element:e.id,before:keep,after:qBox(e),reasons:['TITLE_OFF_RASTER'],candidates_tried:Math.round((target-keep.y)/4)});
  for(const [o,before,box] of moves)if(delta>0||box.h!==before.h)
    patches.push({slide:slide.id,element:o.id,before:o.type==='text'?{...before,font_size:o.font_size}:before,after:o.type==='text'?{...box,font_size:o.font_size}:{...box},reasons:['TITLE_OFF_RASTER'],candidates_tried:0});
  return fresh;
}
async function checkDocumentQuality(doc,{repair=false,minBody=19}={}){
  Q.minBody=minBody;
  const host=document.getElementById('quality-host');const fonts=await renderDocument(doc,host);
  const before=[],after=[],patches=[],measurements=[],scale=qScale(doc),plates=new Map(),parentsOf=new Map();
  for(const slide of doc.slides){
    for(const [e,o] of qCollisions(slide,[...host.children].find(s=>s.dataset.slide===slide.id)))before.push({slide:slide.id,element:e.id,code:'TEXT_COLLISION',other:o.id});
    const plate=await qBackplate(doc,slide);plates.set(slide.id,plate);
    parentsOf.set(slide.id,new Map(slide.elements.filter(e=>e.type==='text').map(e=>[e.id,qParent(slide,e)||qRasterParent(doc,slide,e,plate)])));
  }
  const pushes=repair&&scale.length?await qTitles(doc,host,plates,parentsOf,scale,patches):new Map();
  // Item 40: after the shared sizes, a title with no room of its own pushes the content of its column down (qTitlePush).
  for(const slide of doc.slides.filter(s=>pushes.has(s.id))){
    const fresh=await qTitlePush(doc,slide,[...host.children].find(s=>s.dataset.slide===slide.id),plates.get(slide.id),parentsOf.get(slide.id),pushes.get(slide.id),patches);
    if(fresh){plates.set(slide.id,fresh);parentsOf.set(slide.id,new Map(slide.elements.filter(e=>e.type==='text').map(e=>[e.id,qParent(slide,e)||qRasterParent(doc,slide,e,fresh)])));}
  }
  // G3c after the sizes of the titles: a title the shared size already frees from the picture or the logo stays (cycle p1:
  // a deck 22 title went 64 px down although qTitles, as in g2d, had cleared it off the roundel).
  if(repair)for(const slide of doc.slides){
    const fresh=await qTitleOffRaster(doc,slide,[...host.children].find(s=>s.dataset.slide===slide.id),plates.get(slide.id),parentsOf.get(slide.id),patches);
    if(fresh){plates.set(slide.id,fresh);parentsOf.set(slide.id,new Map(slide.elements.filter(e=>e.type==='text').map(e=>[e.id,qParent(slide,e)||qRasterParent(doc,slide,e,fresh)])));}
  }
  for(const slide of doc.slides){
    const canvas=[...host.children].find(s=>s.dataset.slide===slide.id),plate=plates.get(slide.id),parents=parentsOf.get(slide.id);
    // Empty parts of partition boxes are not occupied content. Free them before
    // moving lower paragraphs, retaining one extra line and the source anchor.
    if(repair)for(const e of slide.elements){
      if(e.type!=='text'||e.source_style?.derivation!=='partition-observed-text-region'||!parents.get(e.id))continue;
      const node=qTextNode(canvas,e.id),glyphs=qGlyphs(canvas,node);
      if(!glyphs.length)continue;
      const h=Math.ceil(Math.max(...glyphs.map(g=>g.y+g.h))-e.y+e.font_size*1.35);
      if(h>=e.h-4)continue;
      const original=Object.fromEntries(['x','y','w','h','font_size'].map(k=>[k,e[k]]));e.h=h;node.style.height=h+'px';
      patches.push({slide:slide.id,element:e.id,before:original,after:{...original,h},reasons:['TRIM_UNUSED_PARTITION_BOX'],candidates_tried:0});
    }
    if(repair)for(const group of qGrowGroups(doc,slide,scale)){
      const cap=qGrowCap(doc,slide,group[0],scale);let grown=null,tried=0;
      if(group.every(e=>!qInspectText(doc,slide,e,canvas,plate,parents.get(e.id)).errors.length))
        for(const size of qGrowSizes(group[0],cap,scale)){tried++;if(group.every(e=>qFitsAt(doc,slide,e,canvas,plate,parents.get(e.id),size))){grown=size;break;}}
      if(grown)for(const e of group){
        // A card text taller than its frame takes the frame that holds it (its shape in the PPTX follows the frame).
        const node=qTextNode(canvas,e.id),original=qBox(e),box=qTightBox(e,node,qRoom(e),grown,e.h);
        qPlace(node,e,{...box,font_size:grown});
        patches.push({slide:slide.id,element:e.id,before:original,after:qBox(e),reasons:['FIT_GROW'],candidates_tried:tried});
      }
    }
    if(repair&&scale.length)qHierarchy(doc,slide,canvas,plate,parents,scale,patches);
    // Owner decision 46: texts whose glyphs meet after the sizes are set are moved apart before the other checks of each text.
    if(repair)qResolveCollisions(doc,slide,canvas,plate,parents,scale,patches);
    // Item 31: text of mandatory decor copied from a sample slide is the author's; it is an obstacle (qCanPlace), not inspected.
    for(const e of slide.elements){if(e.type!=='text'||e.decor)continue;
      const parent=parents.get(e.id),initial=qInspectText(doc,slide,e,canvas,plate,parent);
      before.push(...initial.errors.map(error=>({slide:slide.id,element:e.id,...error})));
      if(repair&&initial.errors.length){
        const footer=['disclosure','page'].includes(e.id);
        const original=Object.fromEntries(['x','y','w','h','font_size'].map(k=>[k,e[k]]));let accepted=false,tried=0;
        const colour=e.color,show=()=>{const node=qTextNode(canvas,e.id);Object.assign(node.style,{left:e.x+'px',top:e.y+'px',width:e.w+'px',height:e.h+'px',fontSize:e.font_size+'px'});if(footer)node.style.color='#'+e.color;};
        for(const candidate of qCandidates(doc,slide,e,parent)){
          tried++;Object.assign(e,{color:colour},candidate);show();
          // Only a new colour needs a plain background: moved in its own colour the footer is judged as before (cycle p1: on the
          // photograph of the template B cover the page number took another place than in g2d).
          if(footer&&candidate.color&&!qPlain(plate,candidate))continue;
          // Item 37, cause 3: the pair of colours of the template on its full-slide picture (template-colour-kept, picture) that
          // reads at least 2:1 is reported, not repainted (deck 19: the white footer on the light blue, like its white text).
          if(candidate.color&&initial.errors.every(x=>x.severity==='warning')&&(e.style_adjustments||[]).some(a=>a.reason==='template-colour-kept'&&a.background==='picture'))continue;
          if(!qInspectText(doc,slide,e,canvas,plate,parent).errors.length){accepted=true;patches.push({slide:slide.id,element:e.id,before:candidate.color?{...original,color:colour}:original,after:candidate,reasons:initial.errors.map(x=>x.code),candidates_tried:tried});break;}
        }
        // H2g: a title or subtitle unreadable on the picture at every place tries the colours of the template (qRecolours).
        // Item 37, cause 5 (holdout-next 25.09.2026, BCNET): so does other text (qRecolourable; the white text of the layout
        // on the yellow panel under a picture), and first at its own place: a colour moves nothing, so an obstacle qCanPlace
        // finds there (BCNET: the white line under the title) is no reason to leave that place out, as qCandidates does.
        if(!accepted&&qRecolourable(e)&&!e.locked&&initial.errors.some(x=>x.code==='LOW_RENDERED_CONTRAST'&&x.severity!=='warning')){
          const same=c=>['x','y','w','h','font_size'].every(k=>c[k]===original[k]);
          const places=[{...original},...qCandidates(doc,slide,e,parent).filter(c=>!same(c))];
          for(const c of qRecolours(doc,slide,e)){
            for(const candidate of places){
              tried++;Object.assign(e,candidate,{color:c});show();
              if(!qInspectText(doc,slide,e,canvas,plate,parent).errors.length){accepted=true;
                patches.push({slide:slide.id,element:e.id,before:{...original,color:colour},after:{...candidate,color:c},reasons:initial.errors.map(x=>x.code),candidates_tried:tried,recolour:true});break;}
            }
            if(accepted)break;
          }
        }
        // Owner decision 46: text that fits at no place and no size down to the minimum of the repairs takes the fixer fit-text
        // before the deck is issued (the user no longer has to choose it): the largest smaller size of the scale of the
        // template, not below its smallest size nor 6 pt, at which it fits its own frame with no finding.
        if(!accepted&&!e.locked&&scale.length&&initial.errors.some(x=>x.code==='TEXT_OVERFLOW')){
          for(const size of scale.filter(v=>v<Math.min(original.font_size,qFontMin(e))-.01&&v>=8-.01).reverse()){
            tried++;Object.assign(e,original,{color:colour,font_size:size});show();
            if(!qInspectText(doc,slide,e,canvas,plate,parent).errors.length&&!qCollides(slide,e,canvas)){accepted=true;
              patches.push({slide:slide.id,element:e.id,before:original,after:qBox(e),reasons:['TEXT_OVERFLOW','FIT_TEXT'],candidates_tried:tried});break;}
          }
        }
        if(!accepted){Object.assign(e,original,{color:colour});show();}
      }
      if(repair&&scale.length&&!e.locked&&!['disclosure','page'].includes(e.id)&&!qOnScale(scale,e.font_size)){
        // Q1: a size off the scale (an estimate of the composer, a title outside the choice of qTitles) takes the next
        // smaller size of the scale at which the text fits with no finding, down to the minimum of the repairs or one step
        // of the scale below the size; otherwise it stays.
        const node=qTextNode(canvas,e.id),original=qBox(e),below=scale.filter(v=>v<e.font_size-.01);let tried=0;
        const least=Math.min(qFontMin(e),below.length?below.at(-1):qFontMin(e));
        for(const size of below.filter(v=>v>=least-.01).reverse()){
          tried++;qPlace(node,e,{...original,font_size:size});
          if(!qInspectText(doc,slide,e,canvas,plate,parent).errors.length){patches.push({slide:slide.id,element:e.id,before:original,after:qBox(e),reasons:['SCALE_SNAP'],candidates_tried:tried});break;}
          qPlace(node,e,original);
        }
      }
      const measured=qInspectText(doc,slide,e,canvas,plate,parent);
      measurements.push({slide:slide.id,element:e.id,glyph_count:measured.glyph_count,min_contrast:measured.min_contrast,surface:parent||null});
      after.push(...measured.errors.map(error=>({slide:slide.id,element:e.id,...error})));
    }
  }
  // Owner decision 46: texts whose glyphs meet, measured on the documents as issued.
  for(const slide of doc.slides){
    const canvas=[...host.children].find(s=>s.dataset.slide===slide.id);
    for(const [e,o] of qCollisions(slide,canvas))after.push({slide:slide.id,element:e.id,code:'TEXT_COLLISION',other:o.id,room:qRoomAbove(canvas,e,o)});
  }
  const tableIssues=measureFit(host).filter(i=>i.code==='TABLE_OVERFLOW');before.push(...tableIssues);after.push(...tableIssues);
  // G1: a chart is drawn by PowerPoint over whatever lies under it; it must keep off logos and undrawable decor frames.
  for(const slide of doc.slides)for(const chart of slide.elements.filter(e=>e.type==='chart')){
    const hit=slide.elements.find(s=>qLogo(doc,s)&&qOverlap(chart,s,2));
    if(hit){const issue={slide:slide.id,element:chart.id,code:'BRAND_ASSET_OVERLAP',asset:hit.id};before.push(issue);after.push(issue);}
  }
  const unavailable=fonts.fontStatus.filter(f=>!f.loaded);
  before.push(...unavailable.map(f=>({code:'FONT_UNAVAILABLE',family:f.family})));
  after.push(...unavailable.map(f=>({code:'FONT_UNAVAILABLE',family:f.family})));
  const warnings=fonts.fontStatus.filter(f=>!f.embedded).map(f=>({code:f.loaded?'FONT_NOT_EMBEDDED':'FALLBACK_FONT_METRICS',family:f.family,source:f.source}));
  return {variant:doc.variant,method:'browser-range-background-sampling-v3',passed:after.length===0,before,after,patches,measurements,font_status:fonts.fontStatus,warnings,
    scope:'Actual browser fit and sampled contrast with font-relative clear space. Conservative heuristic; does not certify PowerPoint rendering or full style fidelity.'};
}

// Session 16 (26.09.2026), M7 of holdout-next3 (analysis/style-experiments/20260926-hn3-m7/process/research-titleband.md).
// W (SAFHE): the widest unbreakable part of a title at a size, in the face of its frame, without wrapping. The text of the
// preview breaks a word that is wider than its frame (overflow-wrap), so the check saw no overflow while PowerPoint set
// "комплектац-ия" in the narrow column of the sample title. Measured only in a face the document serves (the embedded or
// server font: SAFHE's Cabin, whose missing Cyrillic both the preview and PowerPoint take from Arial); a face the preview
// replaces is measured wrong, and a turned frame (deck 02, 56 x 616 px) along the wrong axis. A narrow tall column is no
// turned frame (deck 16 split: 216 px wide; with the bound at twice the width its titles grew to 53 px with broken words).
function qWidestUnit(node,e,size){
  const cs=getComputedStyle(node),probe=document.createElement('span');
  Object.assign(probe.style,{position:'absolute',visibility:'hidden',whiteSpace:'nowrap',fontFamily:cs.fontFamily,fontWeight:cs.fontWeight,
    fontStyle:cs.fontStyle,fontSize:size+'px',textTransform:cs.textTransform,letterSpacing:cs.letterSpacing});
  document.body.append(probe);let widest=0;
  for(const unit of e.text.split(/\s+/).flatMap(w=>w.split(/(?<=[-\u2010\u2011\u2013\u2014\/])/)))if(unit){probe.textContent=unit;widest=Math.max(widest,probe.getBoundingClientRect().width);}
  probe.remove();return widest;
}
function qUnitsFit(doc,node,e,size,width){
  if(e.id!=='title'||e.h>e.w*5||!(doc.style?.fonts||[]).some(f=>f.family===e.font))return true;
  const [l,,r]=e.inset||[0,0,0,0],indent=e.bullet?e.bullet.indent:0;
  return qWidestUnit(node,e,size*Q_GROW_HEADROOM)<=width-l-r-indent+.5;
}
// B2 (deck 20 slides 3-5): the filled band of the slide that holds the frame of a title, and the lowest line its text may
// reach there. The check let the second line grow 20 px below the band (74 -> 94 px), and PowerPoint, which sets lines about
// 5 px lower than the preview, put it on white. The margin: the bottom inset, at least a fifth of the size.
function qBandOf(doc,slide,e){
  const box={x:e.x,y:e.y,w:e.w,h:e.h},bg=qLuminance(qRgb(slide.background||'FFFFFF'));
  return slide.elements.filter(o=>o.type==='shape'&&o.background_decoration&&!o.ghost&&(o.opacity??1)>=.9&&o.fill&&o.h<=doc.height*.35
    &&qContains(o,box)&&qRatio(qLuminance(qRgb(o.fill)),bg)>=3).sort((a,b)=>a.w*a.h-b.w*b.h)[0];
}
function qBandBottom(doc,slide,e,size){
  const band=e.id==='title'?qBandOf(doc,slide,e):null;if(!band)return Infinity;
  const [,,,b]=e.inset||[0,0,0,0];return band.y+band.h-Math.max(b,size*.2);
}
