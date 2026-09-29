/* Shared by editor and self-contained HTML export. Never inserts user HTML. */
async function loadDocumentFonts(doc) {
  const result=[];
  for(const f of doc.style.fonts){
    // Item 38: a variable face of the fonts of the server carries its weight range, or the fixed weight of a family
    // written with its weight ("Montserrat ExtraBold").
    const face=new FontFace(f.family,`url(data:font/ttf;base64,${f.data})`,{weight:f.weight_range||(f.weight==='bold'?'700':'400')});
    try {await face.load();document.fonts.add(face);result.push({family:f.family,weight:f.weight,loaded:true,embedded:true,source:f.origin||'reference',version:f.version});}
    catch(error){result.push({family:f.family,weight:f.weight,loaded:false,embedded:true,error:String(error)});}
  }
  if(!doc.style.fonts.length){
    // fonts.check can be true when an unknown family silently falls back.
    // An explicit local() face must actually resolve, or loading rejects.
    const family=doc.style.font;
    try {const face=new FontFace('__VSPLocalProbe',`local(${JSON.stringify(family)})`);await face.load();result.push({family,loaded:true,embedded:false,source:'system-local',warning:'Local font only; portability and synthetic bold are not verified'});}
    catch(error){result.push({family,loaded:false,embedded:false,source:'fallback',warning:'Required font unavailable; preview uses fallback',error:String(error)});}
  }
  return result;
}
// Placeholder text of vsp.document/2 (A5): frame insets, vertical anchor, one block of paragraphs
// (one div per paragraph, optional hanging marker). `safe` keeps an overflowing block at the top.
// paragraph_gap is the space before every paragraph, as a:spcBef in PowerPoint (A5b); before the first one only when
// space_first_last is not false (bodyPr spcFirstLastPara: PowerPoint skips the gap of the first paragraph without it).
// It is padding, not margin: a collapsed top margin of the first child would stay out of the block's offsetHeight.
function textLayout(el,e){
  const [l,t,r,b]=e.inset||[0,0,0,0];
  Object.assign(el.style,{padding:`${t}px ${r}px ${b}px ${l}px`,display:'flex',flexDirection:'column',justifyContent:{t:'flex-start',ctr:'safe center',b:'safe flex-end'}[e.anchor||'t']});
  el.dataset.layout='paragraphs';
  const block=document.createElement('div');el.append(block);
  // Percentage spacing under single (deck 09: 90 %): PowerPoint takes the whole reduction of the line height from above the
  // first line, CSS half of it from above and half from below, so the glyphs stand higher by the other half (probe, 23.09).
  // Only the drawing moves: the block keeps its height (lines x pitch), as the text height in PowerPoint does, so the fit
  // check and the growth of A8 see the same room as before (a margin shrank the block: 21 titles overflowed, cycle rw).
  const spacing=((e.binding||{}).typography||{}).line_spacing;
  if((e.binding||{}).kind==='placeholder'&&!(spacing&&'pt' in spacing)&&e.line_height<1.2)
    Object.assign(block.style,{position:'relative',top:-((1.2-e.line_height)/2*e.font_size)+'px'});
  for(const [n,line] of e.text.split('\n').entries()){
    // An empty paragraph keeps one line, as in PowerPoint.
    const p=document.createElement('div');p.style.minHeight=e.line_height+'em';
    p.style.paddingTop=(n===0&&e.space_first_last===false?0:e.paragraph_gap||0)+'px';
    if(e.bullet){
      Object.assign(p.style,{paddingLeft:e.bullet.indent+'px',textIndent:-e.bullet.hanging+'px'});
      const mark=document.createElement('span');mark.textContent=e.bullet.char;
      Object.assign(mark.style,{display:'inline-block',width:e.bullet.hanging+'px',textIndent:'0'});p.append(mark);
    }
    p.append(capsText(line,e.caps));block.append(p);
  }
}
// Item 37, cause 9: small capitals (cap="small") drawn as PowerPoint draws them, whatever the font: a lowercase letter as a
// capital at CAPS_SMALL of the size (PowerPoint 2013, office-training cover at full size: small capitals 28 px of 36 px capitals).
// CSS small-caps takes the smcp feature of the font where it has one and synthesises narrower capitals where it has none
// (Carlito for Calibri on the server): the title fitted 40 pt and overflowed its frame in PowerPoint.
const CAPS_SMALL=.78;
function capsText(text,caps){
  if(caps!=='small')return document.createTextNode(text);
  const out=document.createDocumentFragment();let run='',low=false;
  const flush=()=>{if(!run)return;if(low){const s=document.createElement('span');s.style.fontSize=CAPS_SMALL+'em';s.textContent=run.toUpperCase();out.append(s);}else out.append(document.createTextNode(run));run='';};
  for(const ch of text){const lower=ch!==ch.toUpperCase();if(lower!==low&&run)flush();low=lower;run+=ch;}
  flush();return out;
}
// Text overflow of a text element, shared by measureFit and the quality check (quality.js).
// Plain text: scrollHeight/scrollWidth against the box, as before A5.
// Placeholder text: the height of the paragraph block (line boxes and paragraph gaps) against the box
// minus top and bottom inset. PowerPoint lays lines out by line spacing; with spacing tighter than the
// natural line height of the font (Play: 1.1625 of the size against 1.08) the glyphs stick out of their
// line boxes, which is not an overflow of the frame, while scrollHeight counts that overhang.
function textOverflow(node){
  if(node.dataset.layout!=='paragraphs'){
    return {overflow:node.scrollHeight>node.clientHeight+2||node.scrollWidth>node.clientWidth+2,height:node.scrollHeight};
  }
  const style=getComputedStyle(node),insets=parseFloat(style.paddingTop)+parseFloat(style.paddingBottom),height=node.firstChild.offsetHeight;
  return {overflow:height>node.clientHeight-insets+2||node.scrollWidth>node.clientWidth+2,height:height+insets};
}
// G1: the preview of a native chart. Same data, number formats and axis scale as charts.py writes to the PPTX;
// PowerPoint lays the plot area out itself, so positions here are an approximation, not a measurement.
const SVG='http://www.w3.org/2000/svg';
function chartKey(v){return v.toFixed(6).replace(/0+$/,'').replace(/\.$/,'');}
function chartDecimals(values){return Math.max(0,...values.map(v=>(chartKey(v).split('.')[1]||'').length));}
function chartNumber(v,places,lang){
  const english=(lang||'').startsWith('en');const [whole,part]=v.toFixed(places).split('.');
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g,english?',':' ')+(part?(english?'.':',')+part:'');
}
function chartScale(values){
  const top=Math.max(...values)||1,raw=top/5,magnitude=10**Math.floor(Math.log10(raw));
  const step=[1,2,2.5,5,10].map(k=>k*magnitude).find(s=>s>=raw-1e-12);
  let max=Math.ceil(top/step-1e-9)*step;if(top>max-step*.15)max+=step;
  return {max:+max.toFixed(10),step:+step.toFixed(10)};
}
function chartText(svg,value,x,y,e,{anchor='middle',size=e.font_size,rotate=0,baseline='auto'}={}){
  const t=document.createElementNS(SVG,'text');t.textContent=value;
  for(const [k,v] of Object.entries({x,y,'text-anchor':anchor,'dominant-baseline':baseline,fill:'#'+e.color,'font-family':`"${e.font}", Arial, sans-serif`,'font-size':size}))t.setAttribute(k,v);
  if(rotate)t.setAttribute('transform',`rotate(${rotate} ${x} ${y})`);svg.append(t);return t;
}
function chartShape(svg,tag,attributes){const n=document.createElementNS(SVG,tag);for(const [k,v] of Object.entries(attributes))n.setAttribute(k,v);svg.append(n);return n;}
function chartWrap(value,width,size){
  const lines=[''];for(const word of value.split(/\s+/)){const next=lines.at(-1)?lines.at(-1)+' '+word:word;if(lines.at(-1)&&next.length*size*.55>width)lines.push(word);else lines[lines.length-1]=next;}
  return lines.slice(0,3);
}
function chartSvg(e){
  const c=e.chart,fs=e.font_size,svg=document.createElementNS(SVG,'svg');
  svg.setAttribute('viewBox',`0 0 ${e.w} ${e.h}`);Object.assign(svg.style,{width:'100%',height:'100%',overflow:'visible'});
  const values=c.series.flatMap(s=>s.values),places=chartDecimals(values),unit=c.unit.trim().length<=4?c.unit.trim():'';
  const label=v=>chartNumber(v,places,e.lang)+(unit?' '+unit:'');
  const legend=c.kind==='pie'||c.series.length>1,names=c.kind==='pie'?c.categories:c.series.map(s=>s.name);
  const legendH=legend?fs*2:0;
  if(legend){
    const widths=names.map(n=>fs*1.1+n.length*fs*.55+fs*1.2),total=widths.reduce((a,b)=>a+b,0);let x=Math.max(0,(e.w-total)/2);
    for(const [n,name] of names.entries()){
      chartShape(svg,'rect',{x,y:e.h-fs*1.35,width:fs*.7,height:fs*.7,fill:'#'+e.series_colors[n]});
      chartText(svg,name,x+fs*1.05,e.h-fs*1.0,e,{anchor:'start',baseline:'middle'});x+=widths[n];
    }
  }
  if(c.kind==='pie'){
    // A pie has no value axis: the label of the values with the unit is its title (charts.py writes c:title).
    const titleH=fs*1.8;chartText(svg,c.value_label,e.w/2,fs,e,{baseline:'middle'});
    const sum=values.reduce((a,b)=>a+b,0),r=Math.max(10,Math.min(e.w,e.h-legendH-titleH)/2-fs*2.4),cx=e.w/2,cy=titleH+(e.h-legendH-titleH)/2;let angle=-Math.PI/2;
    for(const [i,v] of c.series[0].values.entries()){
      const sweep=v/sum*Math.PI*2,end=angle+sweep,large=sweep>Math.PI?1:0;
      const d=values.length===1?`M ${cx} ${cy-r} A ${r} ${r} 0 1 1 ${cx-.01} ${cy-r} Z`:`M ${cx} ${cy} L ${cx+r*Math.cos(angle)} ${cy+r*Math.sin(angle)} A ${r} ${r} 0 ${large} 1 ${cx+r*Math.cos(end)} ${cy+r*Math.sin(end)} Z`;
      chartShape(svg,'path',{d,fill:'#'+e.series_colors[i],...(e.background?{stroke:'#'+e.background,'stroke-width':1.5}:{})});
      const mid=angle+sweep/2,lx=cx+(r+fs*1.1)*Math.cos(mid),ly=cy+(r+fs*1.1)*Math.sin(mid);
      chartText(svg,label(v),lx,ly,e,{anchor:Math.abs(Math.cos(mid))<.2?'middle':Math.cos(mid)>0?'start':'end',baseline:'middle'});
      angle=end;
    }
    return svg;
  }
  const {max,step}=chartScale(values),ticks=[];for(let t=0;t<=max+step*.001;t+=step)ticks.push(+t.toFixed(10));
  // No tick numbers: every bar and point carries its value (charts.py, value axis).
  const x0=fs*1.8+fs*.5,x1=e.w-fs*.5,group=(x1-x0)/c.categories.length;
  const wrapped=c.categories.map(v=>chartWrap(v,group*.95,fs)),lines=Math.max(...wrapped.map(w=>w.length));
  const y0=fs*1.6,y1=e.h-legendH-lines*fs*1.2-fs*.6,y=v=>y1-v/max*(y1-y0);
  chartText(svg,c.value_label,fs*.8,(y0+y1)/2,e,{rotate:-90,baseline:'middle'});
  for(const t of ticks){
    chartShape(svg,'line',{x1:x0,x2:x1,y1:y(t),y2:y(t),stroke:'#'+e.grid_color,'stroke-opacity':e.grid_alpha??1,'stroke-width':1});
  }
  for(const [i,parts] of wrapped.entries())for(const [n,line] of parts.entries())chartText(svg,line,x0+group*(i+.5),y1+fs*(1.1+n*1.2),e,{baseline:'auto'});
  if(c.kind==='bar'){
    const area=group/1.8,bar=area/c.series.length;
    for(const [n,s] of c.series.entries())for(const [i,v] of s.values.entries()){
      const x=x0+group*i+(group-area)/2+bar*n+bar*.05;
      chartShape(svg,'rect',{x,y:y(v),width:bar*.9,height:Math.max(0,y1-y(v)),fill:'#'+e.series_colors[n]});
      chartText(svg,label(v),x+bar*.45,y(v)-fs*.35,e);
    }
  }else{
    for(const [n,s] of c.series.entries()){
      const points=s.values.map((v,i)=>[x0+group*(i+.5),y(v)]);
      chartShape(svg,'polyline',{points:points.map(p=>p.join(',')).join(' '),fill:'none',stroke:'#'+e.series_colors[n],'stroke-width':Math.max(2,fs*.16),'stroke-linejoin':'round','stroke-linecap':'round'});
      for(const [i,[px,py]] of points.entries()){chartShape(svg,'circle',{cx:px,cy:py,r:Math.max(3,fs*.26),fill:'#'+e.series_colors[n]});chartText(svg,label(s.values[i]),px,py-fs*.6,e);}
    }
  }
  chartShape(svg,'line',{x1:x0,x2:x1,y1:y1,y2:y1,stroke:'#'+e.grid_color,'stroke-opacity':e.grid_alpha??1,'stroke-width':1});
  return svg;
}
function slideElement(doc,slide,onSelect){
  const canvas=document.createElement('div');
  canvas.className='slide';canvas.dataset.slide=slide.id;
  Object.assign(canvas.style,{position:'relative',width:doc.width+'px',height:doc.height+'px',background:slide.background?'#'+slide.background:'#fff',flexShrink:'0'});
  for(const e of slide.elements){
    const el=document.createElement('div');el.className='object';el.dataset.element=e.id;
    Object.assign(el.style,{position:'absolute',left:e.x+'px',top:e.y+'px',width:e.w+'px',height:e.h+'px',boxSizing:'border-box'});
    if(e.type==='text'){
      // Item 31: text of mandatory decor copied from a sample slide is the author's, not measured for fit; a turned one is
      // drawn in its own frame turned around the centre of the box it covers, as PowerPoint draws it.
      let box=el;
      if(!e.decor)el.dataset.fit='text';
      if(e.rotation&&e.frame){
        box=document.createElement('div');
        Object.assign(box.style,{position:'absolute',left:(e.w-e.frame[2])/2+'px',top:(e.h-e.frame[3])/2+'px',width:e.frame[2]+'px',height:e.frame[3]+'px',boxSizing:'border-box',transform:`rotate(${e.rotation}deg)`});
        el.append(box);
      }
      Object.assign(box.style,{fontFamily:`"${e.font}", Arial, sans-serif`,fontSize:e.font_size+'px',lineHeight:String(e.line_height),fontWeight:e.bold?'700':'400',color:'#'+e.color,textAlign:e.align||'left',whiteSpace:'pre-wrap',overflowWrap:'break-word'});
      // Item 37, cause 9: capitals the placeholder sets (cap="all"/"small"), as PowerPoint draws them, drawn and measured here.
      if(e.caps==='all')box.style.textTransform='uppercase';
      if(['inset','anchor','bullet','paragraph_gap'].some(k=>e[k]!==undefined))textLayout(box,e);else box.append(capsText(e.text,e.caps));
    } else if(e.type==='image'){
      const img=document.createElement('img');img.src=`data:${e.mime};base64,${e.data}`;img.alt='Повторяющийся элемент образца';
      if(e.crop){
        const c=e.crop,vw=1-c.l-c.r,vh=1-c.t-c.b;el.style.overflow='hidden';
        Object.assign(img.style,{position:'absolute',width:100/vw+'%',height:100/vh+'%',left:-100*c.l/vw+'%',top:-100*c.t/vh+'%'});
      }else Object.assign(img.style,{width:'100%',height:'100%',objectFit:'contain'});
      el.append(img);
    } else if(e.type==='shape'){
      // roundRect: radius adj of the shorter side, as PowerPoint draws it (a cloned card keeps the adj of its sample).
      Object.assign(el.style,{background:'#'+e.fill,opacity:e.opacity??1,borderRadius:e.geometry==='roundRect'?Math.min(e.w,e.h)*(e.adj??.1667)+'px':e.geometry==='ellipse'?'50%':'0'});
      // G2: rightArrow with the default adjustments of PowerPoint: shaft half the height, head as long as half the shorter side.
      if(e.geometry==='rightArrow'){const head=Math.min(e.w,e.h)/2;el.style.clipPath=`polygon(0 25%,${e.w-head}px 25%,${e.w-head}px 0,100% 50%,${e.w-head}px 100%,${e.w-head}px 75%,0 75%)`;}
    } else if(e.type==='table'){
      const table=document.createElement('table');Object.assign(table.style,{width:'100%',height:'100%',borderCollapse:'collapse',tableLayout:'fixed',fontFamily:`"${e.font}",Arial,sans-serif`,fontSize:e.font_size+'px',lineHeight:'1.18',color:'#'+e.color});
      const cols=document.createElement('colgroup');for(const w of e.column_widths){const col=document.createElement('col');col.style.width=w*100+'%';cols.append(col);}table.append(cols);
      for(const [r,row] of e.rows.entries()){
        const tr=document.createElement('tr');tr.style.height=e.h/e.rows.length+'px';
        for(const [c,value] of row.entries()){
          const td=document.createElement('td');td.textContent=value;td.dataset.row=r;td.dataset.col=c;td.dataset.fit='cell';
          Object.assign(td.style,{border:'1px solid #'+(e.border_color||'000000'),padding:'8px',verticalAlign:'top',fontWeight:r===0?'700':'400',background:'#'+(r===0?(e.header_fill||'F3F6FA'):(e.row_fills||['F3F6FA','FFFFFF'])[r%2]),color:r===0?'#'+e.accent:'#'+e.color,overflowWrap:'break-word'});tr.append(td);
        }table.append(tr);
      }el.append(table);
    } else if(e.type==='chart'){
      el.dataset.chart=e.chart.kind;el.append(chartSvg(e));
    }
    if(onSelect&&!e.locked){el.tabIndex=0;el.title='Нажмите, чтобы редактировать';el.onclick=()=>onSelect(slide,e);el.onkeydown=event=>{if(event.key==='Enter')onSelect(slide,e);};}
    canvas.append(el);
  }return canvas;
}
function measureFit(container){
  const failures=[];
  for(const el of container.querySelectorAll('[data-fit="text"]')){
    const fit=textOverflow(el);
    if(fit.overflow)failures.push({slide:el.closest('.slide').dataset.slide,element:el.dataset.element,code:'TEXT_OVERFLOW',height:fit.height,box:el.clientHeight});
  }
  for(const el of container.querySelectorAll('.object')){
    const table=el.querySelector('table');
    if(table && (table.offsetHeight>parseFloat(el.style.height)+2||table.offsetWidth>parseFloat(el.style.width)+2))failures.push({slide:el.closest('.slide').dataset.slide,element:el.dataset.element,code:'TABLE_OVERFLOW',height:table.offsetHeight,box:parseFloat(el.style.height)});
  }return failures;
}
async function renderDocument(doc,container,onSelect){
  const fontStatus=await loadDocumentFonts(doc);container.replaceChildren();
  for(const slide of doc.slides)container.append(slideElement(doc,slide,onSelect));
  await Promise.all([...container.querySelectorAll('img')].map(img=>img.decode().catch(()=>{})));
  await document.fonts.ready;
  await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  return {fit:measureFit(container),fontStatus};
}
