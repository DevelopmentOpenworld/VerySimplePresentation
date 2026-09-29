// Owner decision 55 (29.09.2026): the page shows the work as it goes. Nothing here moves by a timer or by an expected
// duration: a step lights up when the server says it began (POST /api/progress, a long poll), a request appears when it was
// sent and closes with the time the server measured, the bars of sending and receiving are the bytes of this browser. The
// only clocks are the stopwatch from the click and the wait of an open request, both read from the clock of this device.
// Decision 38 stands: steps and requests are named by what they do.
const Process=(()=>{
  const STEPS={
    read:'Чтение файлов на этом устройстве',send:'Отправка на сервер',queue:'Очередь',package:'Чтение материалов',content:'Содержание: утверждения, план, тексты',
    template:'Разбор образца',scenes:'Разметка слайдов образца',icons:'Пиктограммы образца',layout:'Вёрстка трёх вариантов',
    titles:'Подгонка заголовков',notes:'Текст выступления',meaning:'Проверка смысла',render:'Проверка отрисовки',
    pictures:'Проверка картинок',export:'Аудит и экспорт',pdf:'Печать PDF',receive:'Получение результата',show:'Показ на этом устройстве',
    fixers:'Исправления по правилам',title:'Новый заголовок'};
  const REQUESTS={
    brief_analyst:['content','Выделяем утверждения из текста'],content_writer:['content','Дополняем короткий текст по теме'],
    outline_planner:['content','Составляем план слайдов'],slide_writer:['content','Пишем текст слайда'],
    chart_planner:['content','Подбираем графики и ключевые числа'],scene_marker:['scenes','Размечаем слайды образца'],
    icon_tagger:['icons','Описываем пиктограммы образца'],icon_picker:['icons','Подбираем пиктограммы к утверждениям'],
    title_fitter:['titles','Сокращаем заголовки под рамки'],context_auditor:['meaning','Проверяем слайд по смыслу'],
    speaker_notes:['notes','Пишем текст выступления'],speaker_notes_sources:['notes','Пишем текст выступления по материалам'],
    image_auditor:['pictures','Сверяем картинки с утверждениями'],title_writer:['title','Переписываем заголовок']};
  const REASONS={schema:'ответ не по форме','no-json':'ответ не по форме',truncated:'ответ обрезан',http:'отказ сервиса',network:'нет связи',
    'bad-answer':'ответ не прочитан','no-key':'нет ключа','no-record':'нет ответа','record-mismatch':'нет ответа'};
  const VARIANTS={columns:'колонки',split:'блоки образца',sequence:'иллюстрации образца'};
  const MARKS={title_band:'полоса заголовка',no_text_zones:'места без текста'};
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!=null)n.textContent=text;return n;};
  const seconds=v=>v.toFixed(v<10?1:0).replace('.',',')+' с';
  const clock=ms=>{const s=ms/1000;return String(Math.floor(s/60)).padStart(2,'0')+':'+(s%60).toFixed(1).padStart(4,'0').replace('.',',');};
  const megabytes=b=>b<1048576?Math.max(1,Math.round(b/1024))+' КБ':(b/1048576).toFixed(1).replace('.',',')+' МБ';
  const number=v=>String(v).replace(/\B(?=(\d{3})+(?!\d))/g,' ');
  const perVariant=v=>Object.entries(v||{}).map(([k,n])=>`${VARIANTS[k]||k} — ${number(n)}`).join(', ');
  // What a step gave, in words; only what the server measured and sent.
  const FACTS={
    package:f=>`материалов ${f.materials}, абзацев ${number(f.paragraphs)}, знаков ${number(f.chars)}`,
    content:f=>`слайдов ${f.slides}, утверждений ${f.statements}${f.added?` (из них дописано по теме ${f.added})`:''}${f.charts?`, графиков ${f.charts}`:''}${f.numbers?`, ключевых чисел ${f.numbers}`:''}${f.refused?`; ответов, не принятых проверкой, ${f.refused}`:''}`,
    template:f=>`слайдов образца ${f.slides}, шрифт ${f.font}${f.typefaces?`, гарнитур ${f.typefaces}`:''}, цветов палитры ${f.colours}, наблюдений ${number(f.observations)}`,
    scenes:f=>(f.source==='kept'?'разметка взята из разбора этого образца':f.source==='asked'?'размечено сейчас':'без разметки')+(f.marked?.length?': '+f.marked.map(k=>MARKS[k]||k).join(', '):''),
    icons:f=>f.found?`в образце ${f.found}, описано ${f.described}, слайдов с пиктограммами ${f.slides}`:'в образце нет мест под пиктограммы',
    layout:f=>`${f.variants.length} варианта по ${f.slides} слайдов, объектов ${number(f.objects)}`,
    titles:f=>`мелких заголовков ${f.small}, переписано ${f.rewritten}${f.laid_out_again?', вёрстка повторена':''}`,
    notes:f=>f.broken?'не написан':`слов ${number(f.words)}, слайдов ${f.slides}, выступление на ${f.minutes} мин`,
    meaning:f=>`находок ${f.findings}${f.unanswered?`, слайдов без ответа ${f.unanswered}`:''}`,
    render:f=>`измерено текстов ${number(f.texts)}${f.repairs!=null?`, подгонок ${f.repairs}`:''}, находок ${f.findings}${f.chromium?`; Chromium ${f.chromium}`:''}`,
    pictures:f=>f.asked?`запросов ${f.asked}, находок ${f.findings}`:'картинок у утверждений нет',
    export:f=>`действующих проверок ${f.checks}; находок: ${perVariant(f.findings)}`,
    pdf:f=>`страниц: ${perVariant(f.pages)}`,
    fixers:f=>`исправлено ${f.fixed}, не исправлено ${f.not_fixed}`,
    title:f=>`переписано ${f.rewritten}${f.not_fixed?`, без изменения ${f.not_fixed}`:''}`,
    queue:f=>f.waited!=null?`ожидание ${seconds(f.waited)}`:'идёт другая генерация',
    read:f=>`файлов ${f.files}, ${megabytes(f.bytes)}`,send:f=>megabytes(f.bytes),receive:f=>megabytes(f.bytes),
    show:f=>`миниатюр ${f.thumbnails}, шрифтов загружено ${f.fonts}`};
  const CHECK='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
  const CROSS='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7l10 10M17 7L7 17"/></svg>';

  class Watch{
    constructor(root,work,title){
      this.root=root;this.work=work;this.steps=new Map();this.requests=new Map();this.counts={};this.open=true;this.pinned=null;this.offset=null;
      this.number=[...crypto.getRandomValues(new Uint8Array(16))].map(b=>b.toString(16).padStart(2,'0')).join('');
      this.started=performance.now();
      root.hidden=false;root.className='process '+(work==='generate'?'full':'compact')+' running';root.replaceChildren();
      const head=el('div','process-head');this.heading=el(work==='generate'?'h2':'h3',null,title);
      this.live=el('span','live');this.live.append(el('i'),el('b',null,'идёт'));
      this.clock=el('span','clock','00:00,0');this.clock.title='Время от нажатия, по часам этого устройства';
      head.append(this.heading,this.live,this.clock);
      this.track=el('ol','track');this.track.setAttribute('aria-label','Шаги');
      this.now=el('div','now');this.nowTitle=el('h3');this.nowFacts=el('p','facts');this.nowRequests=el('div','requests');
      this.follow=el('button','secondary follow','Следить за текущим шагом');this.follow.type='button';this.follow.hidden=true;
      this.follow.onclick=()=>{this.pinned=null;this.follow.hidden=true;this.showNow();};
      this.now.append(this.nowTitle,this.nowFacts,this.nowRequests,this.follow);
      this.feed=el('ol','feed');this.feed.setAttribute('aria-label','События');
      const body=el('div','process-body');body.append(this.now,this.feed);
      root.append(head,this.track,body);
      this.tick=()=>{if(!this.open)return;const t=performance.now();if(!this.ended)this.clock.textContent=clock(t-this.started);
        for(const r of this.requests.values())if(r.waiting)r.time.textContent=seconds((t-r.sent)/1000);
        this.frame=requestAnimationFrame(this.tick);};
      this.frame=requestAnimationFrame(this.tick);
      this.poll();
    }
    // ---- the track
    step(id){
      let s=this.steps.get(id);if(s)return s;
      const node=el('li','step waiting');node.dataset.step=id;node.style.setProperty('--n',this.steps.size);
      const button=el('button');button.type='button';const mark=el('span','mark');mark.innerHTML=CHECK;
      const name=el('span','name',STEPS[id]||id),time=el('span','time','ждёт'),bar=el('span','bar');bar.append(el('i'));
      button.append(mark,name,time,bar);node.append(button);
      button.onclick=()=>{this.pinned=id;this.follow.hidden=false;this.showNow();};
      s={id,node,name,time,bar:bar.firstChild,mark,state:'waiting',facts:null,requests:[]};this.steps.set(id,s);
      // Steps of this device stand around the steps of the server: sending before them, receiving and showing after.
      const tail=['receive','show'];const before=tail.includes(id)?null:[...this.track.children].find(n=>tail.includes(n.dataset.step));
      this.track.insertBefore(node,before||null);
      return s;
    }
    set(id,state,facts,took){
      const s=this.step(id);s.state=state;s.node.classList.remove('waiting','running','done','failed');s.node.classList.add(state);
      if(facts)s.facts={...(s.facts||{}),...facts};
      if(state==='running'){s.time.textContent='идёт';s.began=performance.now();}
      if(state==='done')s.time.textContent=took!=null?seconds(took):'готово';
      s.mark.innerHTML=state==='failed'?CROSS:CHECK;
      if(state==='failed')s.time.textContent='остановлен';
      this.showNow();return s;
    }
    share(id,loaded,total){const s=this.step(id);if(total){s.node.classList.add('measured');s.bar.style.width=(100*loaded/total).toFixed(1)+'%';s.time.textContent=`${megabytes(loaded)} из ${megabytes(total)}`;}else s.time.textContent=megabytes(loaded);}
    current(){if(this.pinned)return this.steps.get(this.pinned);const all=[...this.steps.values()];
      return all.filter(s=>s.state==='running').pop()||all.filter(s=>s.state==='done'||s.state==='failed').pop()||all[0];}
    showNow(){
      const s=this.current();if(!s)return;
      for(const x of this.steps.values())x.node.classList.toggle('shown',x===s);
      this.nowTitle.textContent=(s.state==='running'?'Сейчас: ':s.state==='waiting'?'Ждёт: ':'')+(STEPS[s.id]||s.id);
      const told=s.facts&&FACTS[s.id]&&(s.state==='done'||s.id==='queue'||s.id==='send'||s.id==='receive')?safe(()=>FACTS[s.id](s.facts)):'';
      this.nowFacts.textContent=told||(s.state==='running'?'шаг идёт; его итог появится, когда сервер его закончит':s.state==='waiting'?'ещё не начат':'');
      if(this.nowRequests.dataset.step!==s.id){this.nowRequests.dataset.step=s.id;this.nowRequests.replaceChildren(...s.requests.map(r=>r.node));}
    }
    // ---- requests
    request(e){
      if(e.state==='sent'){
        const [home,label]=REQUESTS[e.agent]||[null,e.agent];
        const s=this.steps.get(home)||[...this.steps.values()].filter(x=>x.state==='running').pop();if(!s)return;
        this.counts[e.agent]=(this.counts[e.agent]||0)+1;
        const node=el('span','request waiting');const dot=el('i'),name=el('b',null,label+(e.again?' · повтор с замечаниями проверки':'')),order=el('span','order','№ '+this.counts[e.agent]),time=el('span','time','0,0 с');
        node.append(dot,name,order,time);
        const r={node,time,waiting:true,sent:performance.now(),step:s,label};this.requests.set(e.id,r);s.requests.push(r);
        if(this.current()===s)this.nowRequests.append(node);
        this.badge(s);this.line(e,`${label} — запрос № ${this.counts[e.agent]} отправлен${e.again?' повторно, с замечаниями проверки':''}`);
      }else{
        const r=this.requests.get(e.id);if(!r)return;
        r.waiting=false;r.node.className='request '+(e.ok?'answered':'refused');r.time.textContent=seconds(e.milliseconds/1000);
        const why=e.ok?'':(REASONS[e.reason]||'ответ не получен');
        r.node.title=(e.ok?'Ответ получен':'Ответ не принят: '+why)+(e.answer_tokens?`, ${number(e.answer_tokens)} токенов ответа`:'')+(e.second_request?'; был отправлен второй запрос':'');
        if(!e.ok)r.node.append(el('span','why',why));
        this.badge(r.step);this.line(e,`${r.label} — ${e.ok?'ответ получен':'ответ не принят ('+why+')'} за ${seconds(e.milliseconds/1000)}`,e.ok?'':'bad');
      }
    }
    badge(s){const open=s.requests.filter(r=>r.waiting).length;if(s.state==='running')s.time.textContent=s.requests.length?`запросов ${s.requests.length}${open?`, ждут ответа ${open}`:''}`:'идёт';}
    // ---- the feed
    line(e,text,cls){
      const at=e&&e.t!=null&&this.offset!=null?this.offset+e.t*1000:performance.now()-this.started;
      const row=el('li',cls||'');row.append(el('time',null,clock(Math.max(0,at))),el('span',null,text));
      this.feed.append(row);while(this.feed.children.length>400)this.feed.firstChild.remove();
      this.feed.scrollTop=this.feed.scrollHeight;
    }
    // ---- events of the server
    take(e){
      if(!this.open)return;
      if(this.offset==null)this.offset=performance.now()-this.started-e.t*1000;
      if(e.event==='plan'){e.steps.forEach(id=>this.step(id));if(this.work==='generate'){this.step('receive');this.step('show');}
        this.line(e,`Сервер начал работу: шагов ${e.steps.length}`);this.showNow();}
      else if(e.event==='queue'){if(e.state==='waiting'){this.set('queue','running',{});this.line(e,'Сервер занят другой генерацией: ждём');}
        else if(this.steps.has('queue')){this.set('queue','done',{waited:e.waited},e.waited);this.line(e,`Очередь подошла за ${seconds(e.waited)}`);}}
      else if(e.event==='step'){
        if(e.state==='began'){this.set(e.step,'running',e);this.line(e,`${STEPS[e.step]||e.step} — начало`);}
        else{const s=this.set(e.step,e.broken?'failed':'done',e,e.seconds);const told=FACTS[e.step]?safe(()=>FACTS[e.step](s.facts)):'';
          this.line(e,`${STEPS[e.step]||e.step} — ${e.seconds!=null?'готово за '+seconds(e.seconds):'готово'}${told?': '+told:''}`,'good');}
      }
      else if(e.event==='request')this.request(e);
      else if(e.event==='done')this.line(e,this.work==='generate'?`Сервер закончил за ${seconds(e.seconds)}: запросов ${e.requests}, находок проверок ${e.findings}`:
        this.work==='fix'?`Новая версия ${e.revision}: исправлено ${e.fixed}, находок ушло ${e.gone}, новых ${e.new}`:`Образец разобран за ${seconds(e.seconds)}`,'good');
      else if(e.event==='failed'){for(const id of e.running||[])this.set(id,'failed');this.line(e,'Работа остановлена: '+e.error,'bad');}
    }
    async poll(){
      let after=0;
      while(this.open){
        try{
          const r=await fetch('/api/progress',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({watch:this.number,after})});
          if(!r.ok){await new Promise(f=>setTimeout(f,1000));continue;}
          const data=await r.json();
          for(const e of data.events){if(e.n>after){after=e.n;try{this.take(e);}catch(error){console.error(error);}}}
          if(data.closed){this.serverClosed=true;if(this.ended)this.finish();break;}
        }catch(error){await new Promise(f=>setTimeout(f,1000));}
      }
    }
    // ---- steps of this device
    sent(loaded,total){if(!this.steps.has('send')){this.set('send','running',{bytes:total});this.line(null,`Отправка на сервер: ${megabytes(total)}`);}
      this.share('send',loaded,total);const s=this.steps.get('send');s.facts={bytes:total};
      if(loaded>=total&&s.state!=='done'){this.set('send','done',{bytes:total},(performance.now()-s.began)/1000);this.line(null,`Отправлено ${megabytes(total)}`,'good');}}
    received(loaded,total){if(this.work!=='generate')return;this.got=loaded;if(!this.steps.has('receive')||this.steps.get('receive').state==='waiting'){this.set('receive','running',{bytes:total||loaded});this.line(null,'Получение результата'+(total?`: ${megabytes(total)}`:''));}
      this.share('receive',loaded,total);this.steps.get('receive').facts={bytes:total||loaded};}
    receivedAll(){const s=this.steps.get('receive'),bytes=this.got;if(!s||s.state==='done'||!bytes)return;s.node.classList.add('measured');s.bar.style.width='100%';this.set('receive','done',{bytes},s.began?(performance.now()-s.began)/1000:null);this.line(null,`Получено ${megabytes(bytes)}`,'good');}
    local(id,state,facts){if(state==='began'){this.set(id,'running',facts);this.line(null,`${STEPS[id]} — начало`);}
      else{const s=this.steps.get(id);const took=s?.began?(performance.now()-s.began)/1000:null;this.set(id,'done',facts,took);
        this.line(null,`${STEPS[id]} — готово${took!=null?' за '+seconds(took):''}${facts&&FACTS[id]?': '+FACTS[id](s.facts):''}`,'good');}}
    // The answer of a short work may come before its last events: the view waits for them (the server closes the list of
    // events itself) and only then shows the work as finished; a step the server did not end is never shown as done.
    close(ok,message){
      if(this.ended)return this.ended.total;
      const total=performance.now()-this.started;this.ended={ok,message,total};this.clock.textContent=clock(total);
      if(this.serverClosed)this.finish();else this.last=setTimeout(()=>this.finish(),5000);
      return total;
    }
    finish(){
      if(!this.open)return;this.open=false;clearTimeout(this.last);cancelAnimationFrame(this.frame);
      const {ok,message,total}=this.ended;
      for(const r of this.requests.values())if(r.waiting){r.waiting=false;r.node.className='request refused';r.time.textContent='нет ответа';}
      for(const s of this.steps.values())if(s.state==='running'){this.set(s.id,'failed');s.time.textContent=ok?'конец шага не получен':'остановлен';}
      this.root.classList.remove('running');this.root.classList.add(ok?'finished':'stopped');
      this.live.lastChild.textContent=ok?'готово':'остановлено';
      this.line(null,message||(ok?`Готово за ${seconds(total/1000)} от нажатия`:'Остановлено'),ok?'good':'bad');
      this.pinned=null;this.follow.hidden=true;this.showNow();
    }
  }
  function safe(f){try{return f();}catch(error){return '';}}
  // One request with the bytes sent and received told to the watch (fetch does not tell the bytes it sent).
  function post(url,payload,watch){
    return new Promise((resolve,reject)=>{
      const x=new XMLHttpRequest();x.open('POST',url);x.setRequestHeader('Content-Type','application/json');
      const body=new Blob([JSON.stringify(watch?{...payload,watch:watch.number}:payload)]);
      if(watch){x.upload.onprogress=e=>{if(e.lengthComputable)watch.sent(e.loaded,e.total);};x.upload.onload=()=>watch.sent(body.size,body.size);
        x.onprogress=e=>watch.received(e.loaded,e.lengthComputable?e.total:null);}
      x.onload=()=>{let data;try{data=JSON.parse(x.responseText);}catch(error){return reject(Error('Ответ сервера не прочитан'));}
        if(watch&&x.status>=200&&x.status<300)watch.receivedAll();
        x.status>=200&&x.status<300?resolve(data):reject(Error(data.error||'Сервер отказал: '+x.status));};
      x.onerror=()=>reject(Error('Нет связи с сервером'));
      x.send(body);
    });
  }
  return {watch:(root,work,title)=>new Watch(root,work,title),post};
})();
