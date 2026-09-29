const $=id=>document.getElementById(id);
// B4 (docs/VARIANT_AXIS.md): the three points of the variant axis.
const names={columns:'Вариант 1 · колонки',split:'Вариант 2 · блоки образца',sequence:'Вариант 3 · иллюстрации образца'};
const severities={error:'ошибка',warning:'предупреждение',exception:'исключение'};
let result,current,saved,selected,slideIndex=0,changed=false,audits={};
async function request(url,payload){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await r.json();if(!r.ok)throw Error(data.error);return data;}
function fileBase64(file){return new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=reject;r.readAsDataURL(file);});}
function link(label,url){const a=document.createElement('a');a.textContent=label;a.href=url;a.target='_blank';a.rel='noopener';return a;}
function links(doc,parent){parent.replaceChildren();const base=`/runs/${doc.run_id}/${doc.variant}/r${doc.revision}`;parent.append(link('PPTX',base+'/deck.pptx'),link('PDF',base+'/deck.pdf'),link('HTML',base+'/deck.html'),link('Документ JSON',base+'/document.json'),link('Аудит JSON',base+'/audit.json'));}
function scaleThumbnails(){for(const thumb of document.querySelectorAll('.thumb')){const s=thumb.firstElementChild;const scale=thumb.clientWidth/parseFloat(s.style.width);s.style.transform=`scale(${scale})`;thumb.style.height=parseFloat(s.style.height)*scale+'px';}}
function scaleEditor(){if(!current)return;const scale=Math.min(1,($('canvas-wrap').clientWidth-32)/current.width);const slide=$('canvas').firstElementChild;if(slide){slide.style.transform=`scale(${scale})`;$('canvas').style.width=current.width*scale+'px';$('canvas').style.height=current.height*scale+'px';}}
// C5: findings of a revision (audit.json "findings": model, checks of Appendix 1, finished PPTX, rendered check).
function findingsOf(variant){return (audits[variant]?.findings)||[];}
function countBySeverity(list){const c={error:0,warning:0,exception:0};for(const f of list)c[f.severity]=(c[f.severity]||0)+1;return c;}
// A-02 of the external review (24.09.2026): findings are not a verdict of a check that did not run. Slides the contextual
// audit left without an answer (no record, no answer of the model) are named instead of a plain "Находок нет".
function uncheckedSlides(){return new Set(((result?.run?.context_audit?.fallbacks)||[]).filter(f=>f.slide).map(f=>f.slide));}
function summary(list){const c=countBySeverity(list),n=uncheckedSlides().size;const gap=n?`контекстная проверка не выполнена на ${n} ${n%10===1&&n%100!==11?'слайде':'слайдах'}`:'';
  return list.length?`Находки — ошибки: ${c.error}, предупреждения: ${c.warning}${c.exception?`, исключения: ${c.exception}`:''}${gap?'; '+gap:''}`:(gap?'Находок проверок нет, но '+gap:'Находок нет');}
// Item 34: what the content package gave (materials read, fonts, what did not go into the request). Decision 37: PDF is
// read; a scan, a password or a broken file is named with its reason, pages without text are named as a partial read.
const PACKAGE_REASONS={'pdf-no-text':'скан без текстового слоя','pdf-encrypted':'защищён паролем','pdf-invalid':'файл повреждён',
  'pdf-reader-unavailable':'чтение PDF недоступно на сервере','pdf-pages-without-text':'страницы без текста пропущены',
  'pdf-pages-over-limit':'прочитаны первые 300 страниц','font-not-read':'шрифт не прочитан','type-not-supported':'тип файла не поддерживается'};
function withReasons(list){return (list||[]).map(u=>{const r=u.warnings.map(w=>PACKAGE_REASONS[w]).filter(Boolean);return r.length?`${u.name} (${r.join(', ')})`:u.name;}).join(', ');}
function packageNote(run){const p=run.content_package;if(!p)return '';
  const unread=withReasons(p.unread),partial=withReasons(p.partial),left=p.left_out.map(l=>l.share!=null?`${l.name} (${Math.round(l.share*100)} % текста, выбрано по всему тексту)`:`${l.name} (${l.taken} из ${l.paragraphs} абзацев)`).join(', ');
  return ` Контент-пакет: материалов ${p.materials}, абзацев ${p.paragraphs}${p.fonts.length?`, шрифты: ${p.fonts.join(', ')}`:''}${unread?`; не прочитаны: ${unread}`:''}${partial?`; прочитаны частично: ${partial}`:''}${left?`; вошли не целиком: ${left}`:''}.`;}
// A-01 of the external review: facts over the capacity of the deck are left out on the record, not silently.
// Owner decision 47 (28.09.2026): how many statements came from the text of the user and how many content_writer added to a
// short request (the page says "the service", decision 38); a deck shorter than asked is told so, with what to do.
function contentNote(run){
  const s=run.planning?.supplement;if(!s)return '';
  const slides=result.documents[0]?.slides.length||0;let note='';
  if(s.added)note+=` Из вашего текста — утверждений: ${s.from_text}; дописано сервисом по теме: ${s.added} (общие сведения без чисел не из вашего текста; отмечены в находках — проверьте их перед показом).`;
  // Only a lack of statements is told (C2, 29.09: 22 statements of 22 needed laid out on 11 of 12 slides by the plan).
  if(slides&&s.requested_slides&&slides<s.requested_slides&&s.from_text+s.added<s.need)note+=` Слайдов ${slides} из запрошенных ${s.requested_slides}: в тексте мало утверждений. Добавьте тезисы или приложите материалы.`;
  return note;
}
function omittedNote(run){const n=(run.planning?.omitted||[]).length;return n?` Фактов сверх вместимости колоды, не вошедших в неё: ${n}.`:'';}
async function showVariants(){
  $('variants').replaceChildren();const all=[];
  for(const doc of result.documents){
    const card=document.createElement('article');card.className='variant';
    const heading=document.createElement('h3');heading.textContent=names[doc.variant];card.append(heading);
    const grid=document.createElement('div');grid.className='thumbnails';card.append(grid);
    const fontStatus=await loadDocumentFonts(doc);
    for(const slide of doc.slides){const thumb=document.createElement('div');thumb.className='thumb';thumb.append(slideElement(doc,slide));grid.append(thumb);}
    const found=document.createElement('p');found.className='muted';found.textContent=summary(findingsOf(doc.variant));card.append(found);
    const open=document.createElement('button');open.textContent='Открыть в редакторе';open.onclick=()=>openEditor(doc);card.append(open);
    const download=document.createElement('div');links(doc,download);card.append(download);$('variants').append(card);
    all.push({variant:doc.variant,fontStatus});
  }
  $('results').hidden=false;scaleThumbnails();
  await Promise.all([...$('variants').querySelectorAll('img')].map(i=>i.decode()));await document.fonts.ready;
  await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
  return {fit:measureFit($('variants')),font_status:all};
}
// Owner decisions 27-28: the preparation stage of the chosen template (scene markup by the model), outside the time of a
// generation; a generation of the same template reads it back.
$('template').onchange=async()=>{
  const file=$('template').files[0];$('prepared').textContent='';
  if(!file||file.size>40*1024**2)return;
  $('prepared').textContent='Образец разбирается: смотрим его слайды…';
  // Decision 55: the steps of the work are shown as the server goes through them (progress.js).
  const watch=Process.watch($('prepare-process'),'prepare','Разбор образца');
  try{
    watch.local('read','began');const data=await fileBase64(file);watch.local('read','ended',{files:1,bytes:file.size});
    const r=await Process.post('/api/prepare',{template:data,template_name:file.name},watch);
    watch.close(true,`Образец разобран: ${r.seconds} с на сервере`);
    const names={title_band:'полоса заголовка',no_text_zones:'места без текста'};
    $('prepared').textContent=r.accepted.length?`Образец разобран за ${r.seconds} с: ${r.accepted.map(k=>names[k]||k).join(', ')}.`:
      `Образец прочитан за ${r.seconds} с; дополнительная разметка ${r.fallback?'не выполнена':'не понадобилась'}.`;
  }catch(error){watch.close(false,'Разбор образца не выполнен: '+error.message);$('prepared').textContent='Разбор образца не выполнен: '+error.message+'. Генерация всё равно возможна.';}
};
// Decision 38: the experts describe the task in plain fields; the request object (schemas/free_brief.json) is built here.
function taskOf(){
  const text=$('task-text').value.trim();
  if(text.length<20)throw Error('Опишите, о чём презентация: не короче 20 знаков');
  // Owner decision 52 (29.09.2026): a long text (a Habr article) is taken; over 20 000 signs the service reads it over its
  // whole length and the page says how much of it went in.
  if(text.length>200000)throw Error('Текст длиннее 200 000 знаков');
  const slides=Number($('task-slides').value);
  if(!Number.isInteger(slides)||slides<3||slides>15)throw Error('Число слайдов — от 3 до 15');
  const task={schema:'vsp.free-brief/1',text,slides,purpose:$('task-purpose').value,language:'ru'};
  const audience=$('task-audience').value.trim();if(audience)task.audience=audience.slice(0,300);
  const minutes=$('task-minutes').value.trim();
  if(minutes){const m=Number(minutes);if(!Number.isInteger(m)||m<1||m>60)throw Error('Длительность выступления — от 1 до 60 минут');task.talk_minutes=m;}
  return task;
}
$('generate').onclick=async()=>{
  let brief;try{brief=taskOf();}catch(error){$('status').textContent=error.message;return;}
  const start=performance.now();$('generate').disabled=true;$('status').textContent='Читаем образец, создаем и проверяем три варианта…';$('editor').hidden=true;$('results').hidden=true;$('variants').replaceChildren();result=null;
  const watch=Process.watch($('process'),'generate','Ход работы');$('process').scrollIntoView({behavior:'smooth',block:'start'});
  try{
    const file=$('template').files[0];
    if(file&&file.size>40*1024**2)throw Error('Размер образца превышает 40 МБ');
    const files=[...$('materials').files];
    if(files.reduce((s,f)=>s+f.size,0)>30*1024**2)throw Error('Контент-пакет больше 30 МБ');
    if(file||files.length)watch.local('read','began');
    const materials=await Promise.all(files.map(async f=>({name:f.name,data:await fileBase64(f)})));
    const template=file?await fileBase64(file):null;
    if(file||files.length)watch.local('read','ended',{files:files.length+(file?1:0),bytes:files.reduce((s,f)=>s+f.size,file?file.size:0)});
    result=await Process.post('/api/generate',{brief,template,template_name:file?file.name:null,materials},watch);
    audits={};result.documents.forEach((doc,n)=>{audits[doc.variant]=result.audits[n];});
    watch.local('show','began');
    const display=await showVariants();const elapsed=performance.now()-start;
    watch.local('show','ended',{thumbnails:$('variants').querySelectorAll('.thumb').length,fonts:display.font_status.reduce((n,v)=>n+v.fontStatus.filter(f=>f.loaded).length,0)});
    watch.close(true,`Три варианта показаны: ${(elapsed/1000).toFixed(2).replace('.',',')} с от нажатия`);
    $('timing').textContent=`${(elapsed/1000).toFixed(2)} с от нажатия до показа всех вариантов`;
    if(display.fit.length)throw Error('Отрисовка на этом устройстве обнаружила переполнение; результаты скрыты.');
    if(display.font_status.some(v=>v.fontStatus.some(f=>!f.loaded)))throw Error('На этом устройстве не загрузился нужный шрифт; результаты скрыты.');
    $('status').textContent=(result.run.quality?.warnings.length?'Созданы три варианта. Шрифт доступен на этом компьютере, но не встроен в PPTX; проверьте его на устройстве показа.':'Готовы три варианта. Откройте один в редакторе: там находки проверок и исправления на выбор.')+packageNote(result.run)+contentNote(result.run)+omittedNote(result.run);
    $('report').textContent=JSON.stringify({diagnostics:result.diagnostics,audits:result.audits,browser:display,run:result.run.id,stages:result.run.timings},null,2);
    await request('/api/display',{run_id:result.run.id,click_to_display_ms:elapsed,...display});
  }catch(error){watch.close(false,'Остановлено: '+error.message);$('results').hidden=true;$('variants').replaceChildren();$('status').textContent='Ошибка: '+error.message;}
  finally{$('generate').disabled=false;}
};
async function openEditor(doc){current=structuredClone(doc);saved=structuredClone(doc);selected=null;slideIndex=0;changed=false;$('editor').hidden=false;$('inspector').hidden=true;$('fix-report').hidden=true;await renderEditor();$('editor').scrollIntoView({behavior:'smooth'});}
function markDirty(){changed=true;$('dirty').textContent='Есть несохраненные правки';$('downloads').replaceChildren();renderFindings();}
function select(slide,e){selected=e;document.querySelectorAll('.selected').forEach(n=>n.classList.remove('selected'));for(const node of $('canvas').querySelectorAll('.object'))if(node.dataset.element===e.id)node.classList.add('selected');$('selection-label').textContent=`${e.id} · ${e.type}`;$('inspector').hidden=false;
  $('object-text').disabled=e.type==='image'||e.type==='chart';$('object-text').value=e.type==='table'?e.rows.map(row=>row.join('\t')).join('\n'):e.type==='chart'?[e.chart.value_label,['',...e.chart.categories].join('\t'),...e.chart.series.map(s=>[s.name,...s.values].join('\t'))].join('\n'):e.text||'';
  for(const k of ['x','y','w','h'])$(k).value=e[k];$('font-size').value=e.font_size||20;$('font-size').disabled=e.type==='image'||e.type==='chart';
}
function highlight(f){
  document.querySelectorAll('.finding-target').forEach(n=>n.classList.remove('finding-target'));
  const slide=current.slides[slideIndex];const e=slide.elements.find(x=>x.id===f.element);
  if(e){for(const node of $('canvas').querySelectorAll('.object'))if(node.dataset.element===e.id)node.classList.add('finding-target');select(slide,e);}
  else $('canvas').firstElementChild?.classList.add('finding-target');
}
function renderFindings(){
  const slide=current.slides[slideIndex];const all=findingsOf(current.variant);
  const here=all.filter(f=>f.slide===slide.id),deck=all.filter(f=>!f.slide);
  $('findings-note').textContent=`${summary(all)} во всём варианте; на этом слайде — ${here.length}${uncheckedSlides().has(slide.id)?', контекстная проверка этого слайда не выполнена':''}.`+(changed?' Исправления применяются к сохранённой версии: сохраните или отмените ручные правки.':'');
  $('finding-list').replaceChildren();
  for(const f of [...here,...deck]){
    const row=document.createElement('div');row.className='finding';
    const box=document.createElement('input');box.type='checkbox';box.value=f.id;box.disabled=!f.fixer||f.severity==='exception'||changed;box.title=f.fixer?`Исправитель: ${f.fixer}`:'Автоматического исправления нет';
    const badge=document.createElement('span');badge.className='sev '+f.severity;badge.textContent=severities[f.severity]||f.severity;
    // C7: a contextual finding is a "no" of the model to a question of Appendix 1; its reason is shown with it.
    const label=f.check==='context'?`Смысл: не выполнено «${f.title}» — ${f.message}`:`${f.title||f.code}${f.element?' · '+f.element:''}${f.slide?'':' · вся колода'}`;
    box.onchange=countFixes;
    const text=document.createElement('button');text.type='button';text.className='link';text.textContent=label;text.title=f.code+(f.message?': '+f.message:'')+(f.fixer_planned?` (исправитель ${f.fixer_planned} запланирован)`:'');text.onclick=()=>highlight(f);
    row.append(box,badge,text);
    // Owner decision 48: a finding without an automatic fix says so, and how it is fixed.
    if(!f.fixer&&f.severity!=='exception'){const hint=document.createElement('span');hint.className='muted finding-hint';hint.textContent='автоматического исправления нет — поправьте текст или блок в редакторе';row.append(hint);}
    $('finding-list').append(row);
  }
  countFixes();
}
// Owner decision 48: the button says how many findings are ticked and is enabled only then.
function countFixes(){
  const ticked=[...$('finding-list').querySelectorAll('input:checked')].length;
  $('apply-fixes').textContent=ticked?`Применить выбранные исправления (${ticked})`:'Отметьте находки с исправлением';
  $('apply-fixes').disabled=changed||!ticked;
}
// Text of every slide (owner decision 21, 1a): what the speaker says on this slide; edits are saved with the version.
const noteSources={agent:'составлен автоматически',fallback:'составлен по фактам слайда',user:'изменён вручную'};
function renderNotes(){
  const notes=current.speaker_notes,note=notes?.slides?.[current.slides[slideIndex].id];
  $('notes-panel').hidden=!note;if(!note)return;
  $('slide-notes').value=note.text;
  const count=note.text.split(/\s+/).filter(Boolean).length;
  $('notes-meta').textContent=`Текст ${noteSources[note.source]||note.source}; слов на слайде: ${count}. Всё выступление: ${notes.words} слов, около ${(notes.words/(notes.words_per_minute||120)).toFixed(1)} мин из ${notes.minutes}.`;
}
$('slide-notes').oninput=()=>{const note=current?.speaker_notes?.slides?.[current.slides[slideIndex].id];if(!note)return;note.text=$('slide-notes').value;markDirty();};
async function renderEditor(){
  current.slides.forEach((slide,n)=>{const page=slide.elements.find(e=>e.id==='page');if(page)page.text=String(n+1);});
  $('editor-title').textContent=`${names[current.variant]} · версия ${current.revision}`;$('dirty').textContent=changed?'Есть несохраненные правки':'';
  if(!changed)links(current,$('downloads'));
  $('slides-nav').replaceChildren();
  current.slides.forEach((slide,n)=>{const row=document.createElement('div');const b=document.createElement('button');const count=findingsOf(current.variant).filter(f=>f.slide===slide.id&&f.severity!=='exception').length;b.className=n===slideIndex?'':'secondary';b.textContent=`${n+1}. ${slide.id}${count?` · ${count}`:''}`;b.onclick=async()=>{slideIndex=n;selected=null;$('inspector').hidden=true;await renderEditor();};row.append(b);
    const actions=document.createElement('div');actions.className='reorder';for(const [step,label] of [[-1,'↑'],[1,'↓']]){const move=document.createElement('button');move.className='secondary';move.textContent=label;move.title='Переместить слайд';move.disabled=n+step<0||n+step>=current.slides.length;move.onclick=async()=>{[current.slides[n],current.slides[n+step]]=[current.slides[n+step],current.slides[n]];slideIndex=n+step;markDirty();await renderEditor();};actions.append(move);}row.append(actions);$('slides-nav').append(row);
  });
  await loadDocumentFonts(current);$('canvas').replaceChildren(slideElement(current,current.slides[slideIndex],select));scaleEditor();
  renderNotes();renderFindings();
  await document.fonts.ready;await new Promise(r=>requestAnimationFrame(r));
  const fit=measureFit($('canvas'));$('edit-report').textContent=fit.length?JSON.stringify(fit,null,2):'Переполнений текста в текущем слайде не обнаружено.';
}
// The thumbnails of the variants are refreshed without waiting: a hidden tab draws no frames.
async function adopt(document_,audit){current=document_;audits[current.variant]=audit;selected=null;$('inspector').hidden=true;saved=structuredClone(current);result.documents[result.documents.findIndex(d=>d.variant===current.variant)]=structuredClone(current);changed=false;showVariants();await renderEditor();}
$('inspector').onsubmit=async event=>{event.preventDefault();if(!selected)return;for(const k of ['x','y','w','h'])selected[k]=Number($(k).value);if(selected.type==='text'||selected.type==='table')selected.font_size=Number($('font-size').value);if(selected.type==='text')selected.text=$('object-text').value;if(selected.type==='table')selected.rows=$('object-text').value.split('\n').map(r=>r.split('\t'));markDirty();await renderEditor();};
$('save').onclick=async()=>{if(!current)return;$('save').disabled=true;try{const probe=document.createElement('div');probe.style.cssText='position:absolute;left:-10000px;top:0';document.body.append(probe);let fit;try{fit=(await renderDocument(current,probe)).fit;}finally{probe.remove();}if(fit.length)throw Error('Есть переполнение: '+JSON.stringify(fit));const response=await request('/api/revise',{run_id:current.run_id,document:current});await adopt(response.document,response.audit);$('edit-report').textContent='Версия сохранена. PPTX и HTML обновлены.';}catch(error){$('edit-report').textContent=error.message;}finally{$('save').disabled=false;}};
// C5: chosen findings -> fixers on the server -> new revision rN -> audit again -> which findings went away.
$('apply-fixes').onclick=async()=>{
  const ids=[...$('finding-list').querySelectorAll('input:checked')].map(i=>i.value);
  if(!ids.length){$('fix-report').hidden=false;$('fix-report').textContent='Отметьте находки, которые нужно исправить.';return;}
  $('apply-fixes').disabled=true;
  const watch=Process.watch($('fix-process'),'fix','Исправление');
  try{
    const response=await Process.post('/api/fix',{run_id:current.run_id,variant:current.variant,revision:current.revision,findings:ids},watch);
    watch.close(true,`Новая версия ${response.document.revision}`);
    const lines=[`Новая версия ${response.document.revision}: применено ${response.fixed.length}, не применено ${response.skipped.length}.`,
      `Ушли: ${response.resolved.length?response.resolved.join(', '):'—'}`,`Остались: ${response.remaining.filter(id=>ids.includes(id)).join(', ')||'—'}`,`Новые: ${response.new.join(', ')||'—'}`];
    for(const s of response.skipped)lines.push(`Не применено ${s.finding}: ${s.reason}`);
    $('fix-report').hidden=false;$('fix-report').textContent=lines.join('\n');
    await adopt(response.document,response.audit);
  }catch(error){watch.close(false,'Исправление не применено');$('fix-report').hidden=false;$('fix-report').textContent=error.message;}
  finally{renderFindings();}
};
$('revert').onclick=async()=>{current=structuredClone(saved);selected=null;changed=false;$('inspector').hidden=true;await renderEditor();};
window.addEventListener('resize',()=>{scaleThumbnails();scaleEditor();});
