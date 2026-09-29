from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import re
import time
import uuid
from .importer import read_style, digest
from .model import build_documents, audit_document
from .exporter import export_pptx, export_html
from .carrier_export import export_carrier_pptx
from .audit import package_findings, checks, READABLE, READABLE_LARGE, LARGE_PT, LARGE_BOLD_PT, PT
from . import fixers
from .quality import inspect_documents, apply_repairs, failure_message
from .diversity import audit_diversity
from .font_assets import resolve_fonts
from .planner import structure
from .context import review, review_images, rewrite_title, title_payload, title_findings
from .pdf import export_pdfs, inspect_pdf
from .icons import choose as choose_icons
from .notes import write_notes
from .titles import fit_titles
from .materials import read_package, brief_text, package_fonts, slide_sources, request_material, BRIEF_LIMIT, REQUEST_MAX
from .font_catalog import catalog_fonts, serve_fonts, subset_gaps
from . import llm
from .typography import EM_DASH, EN_DASH, template_dash, with_dash
from . import scenes
from .progress import Quiet

ROOT = Path(__file__).resolve().parents[3]
# C4b: on the carrier only these audit errors block the output; the rest is issued as findings.
# G1: a chart that differs from the chart of its brief section shows other numbers than the facts, as a changed fact would.
FACTUAL_ERRORS = ('FACT_CHANGED','FACT_COVERAGE','SLIDE_SET','DISCLOSURE_CHANGED','COLUMN_HEADERS_CHANGED','SOURCE_NOTE_CHANGED','CHART_DATA_CHANGED','METRIC_CHANGED')
FINDING_MESSAGES = {
    'TEXT_OVERFLOW':'Текст не помещается в выбранную область',
    'TABLE_OVERFLOW':'Таблица не помещается в выбранную область',
    'LOW_RENDERED_CONTRAST':'Текст плохо читается на оформлении образца',
    'BRAND_ASSET_OVERLAP':'Текст пересекается с защищенным элементом оформления',
    'CARD_TEXT_MARGIN':'Недостаточны отступы текста внутри карточки',
    'RASTER_SURFACE_MARGIN':'Текст выходит за безопасную область визуальной подложки',
    'FONT_AVAILABILITY':'Шрифт образца недоступен: предпросмотр с подменой, PPTX ссылается на шрифт образца',
    'OUT_OF_BOUNDS':'Объект выходит за границы слайда',
    'OBJECT_OVERLAP':'Объекты пересекаются',
    'TEXT_COLLISION':'Текст наезжает на другой текст',
    'LOW_SOLID_CONTRAST':'Текст плохо читается на сплошном фоне',
    'BINDING_INVALID':'Некорректная привязка объекта к образцу',
    'STYLE_BACKGROUND_CHANGED':'Фон слайда отличается от образца',
    'STYLE_DECORATION_CHANGED':'Оформление слайда отличается от образца',
    'DUPLICATE_ID':'Повторяется идентификатор объекта',
    'CHART_DATA_CHANGED':'Данные диаграммы отличаются от фактов брифа',
    'METRIC_CHANGED':'Ключевое число карточки отличается от брифа',
    'PDF_INVALID':'PDF не создан или не читается',
    'PDF_PAGE_COUNT':'Страниц PDF не столько, сколько слайдов',
    'PDF_FONT_NOT_EMBEDDED':'Шрифт не встроен в PDF',
    'PDF_EMPTY_PAGE':'Пустая страница PDF',
    'PDF_FONT_SUBSTITUTED':'PDF нарисован не гарнитурой шаблона',
}


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')


def write_json(path, value):
    path.write_bytes(json_bytes(value))


def journal(output_root, event, **fields):
    """One line of the upload journal (owner decision 24, plan item 38): every upload with its file name and SHA-256,
    the fonts the template names with the source of each, and how the run ended. Kept on the server with the uploads
    (var/generator is outside Git); the evidence that a font was neither embedded nor openly available."""
    record={'event':event,'at':datetime.now(timezone.utc).isoformat(),**fields}
    with open(Path(output_root)/'uploads.jsonl','ab') as f:
        f.write((json.dumps(record,ensure_ascii=False,separators=(',',':'))+'\n').encode('utf-8'))


def verify_release(root=ROOT):
    release = json.loads((root/'profiles/mvp/release.json').read_text('utf-8'))
    if not re.fullmatch(r'structured-mvp-[a-f0-9]{12}',release['id']):
        raise ValueError('Некорректный идентификатор релиза')
    registered=json.loads((root/'profiles/mvp/releases'/(release['id']+'.json')).read_text('utf-8'))
    if registered!=release:
        raise ValueError('Активный состав отличается от зарегистрированного')
    for name, expected in release['files'].items():
        path=(root/name).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file() or digest(path.read_bytes()) != expected:
            raise ValueError(f'Состав релиза изменен: {name}. Создайте проверенный новый состав перед запуском.')
    return release


# Owner decision 52: the request that stands for a long text given as the material of the request.
LONG_REQUEST='Презентация по приложенному тексту.'


def issue_order(documents, config):
    """Owner request of 29.09.2026, late evening, for every generation: the columns variant first, the list variant (on pictures of
    the template now, composer.PICTURE_AREA) last. The variants are composed in the order of config['variants'], so the choices of
    compositions stay as before; the documents are issued, checked, exported and shown in config['variant_order']."""
    order=config.get('variant_order') or config['variants']
    return sorted(documents,key=lambda d:order.index(d['variant']) if d['variant'] in order else len(order))


def finding(issue, variant, check):
    """C4b: one finding of an issued deck: code, variant, slide, element, message; check is 'audit' or 'render'.
    C3: severity, title and fixer come from the registry audit/checks.json."""
    code='FONT_AVAILABILITY' if issue['code']=='FONT_UNAVAILABLE' else issue['code']
    message=FINDING_MESSAGES.get(code,'Находка проверки: '+code)
    if code=='FONT_AVAILABILITY':message+=': '+issue.get('family','')
    entry=checks().get(code,{})
    # Holdout 25.09 (H2b): a low contrast of a pair of colours of the template itself is its own colour (C3), a warning.
    own=issue.get('severity')=='warning' and code in ('LOW_RENDERED_CONTRAST','LOW_SOLID_CONTRAST')
    if own:message+=' — цвет и фон самого образца'
    out={'code':code,'variant':variant,'slide':issue.get('slide'),'element':issue.get('element'),'message':message,'check':check,
         'severity':'warning' if own else entry.get('severity','error'),'title':entry.get('title',code)}
    if entry.get('fixer') and not own:out['fixer']=entry['fixer']
    # Owner decision 48: the background the check of the rendering measured under a text that does not read (readable-colour).
    if issue.get('background'):out['background']=issue['background']
    return out


# Owner decision 49 (29.09.2026), limitation 2: the checks of contrast of the model of the document compare the colour of a text
# with the solid fill under its whole frame; the check of the rendering measures the pixels under its glyphs (quality.js,
# qInspectText: min_contrast). They disagree where a frame crosses the edge of a surface and its letters stand on one side
# (deck 20: the disclosure over the edge of the white panel; after the fixer readable-colour made it grey on the white, the
# model reported 8 new findings for the 4 it removed). What the viewer sees decides: a finding of the model on a text the
# rendering measured is kept only when the measured contrast is below the threshold of that same check.
MODEL_CONTRAST=('LOW_SOLID_CONTRAST','CONTRAST_BELOW_AA')


def render_decides(doc, audit, measurements):
    """Drop the findings of contrast of the model (errors, warnings and the Appendix 1 findings of audit_document) on texts whose
    glyphs the check of the rendering measured at least at the threshold of that check (LOW_SOLID_CONTRAST 3:1; CONTRAST_BELOW_AA
    4.5:1, large text 3:1); what was dropped is kept in audit['decided_by_render']. Returns the dropped items."""
    measured={(m['slide'],m['element']):m['min_contrast'] for m in measurements or [] if m.get('glyph_count') and m.get('min_contrast') is not None}
    if not measured:return []
    elements={(s['id'],e['id']):e for s in doc['slides'] for e in s['elements']}
    def readable(item):
        ratio=measured.get((item.get('slide'),item.get('element')))
        e=elements.get((item.get('slide'),item.get('element')))
        if item.get('code') not in MODEL_CONTRAST or ratio is None or e is None:return None
        large=e['font_size']*PT>=LARGE_PT or (e.get('bold') and e['font_size']*PT>=LARGE_BOLD_PT)
        need=3.0 if item['code']=='LOW_SOLID_CONTRAST' else READABLE_LARGE if large else READABLE
        return {'code':item['code'],'slide':item['slide'],'element':item['element'],'measured':ratio,'threshold':need} if ratio>=need else None
    dropped=[]
    for key in ('errors','warnings','findings'):
        kept=[]
        for item in audit.get(key,[]):
            decided=readable(item)
            if decided:dropped.append(decided)
            else:kept.append(item)
        if key in audit:audit[key]=kept
    if dropped:audit['decided_by_render']=audit.get('decided_by_render',[])+dropped
    return dropped


def context_finding(f):
    """C7: a "no" of the contextual audit as a finding of the registry, with the reason of the model."""
    entry=checks()[f['code']]
    out={'code':f['code'],'slide':f['slide'],'element':f.get('element'),'message':f['reason'],'question':f['question'],'check':'context',
         'severity':entry['severity'],'title':entry['title']}
    if entry['fixer']:out['fixer']=entry['fixer']
    return out


def revision_findings(doc, audit, render=None, context=None):
    """C5: every finding of a revision in one list with a stable id: the model audit (not the blocking factual errors,
    not the rough fit estimate the browser measurement replaces), the checks of Appendix 1, the rendered check and the
    contextual audit of the content (C7). A fixer the registry names but the product does not have yet is kept as
    fixer_planned, so the interface does not offer it."""
    model=[finding(e,doc['variant'],'audit') for e in audit['errors']+audit['warnings'] if e['code'] not in FACTUAL_ERRORS+('TEXT_FIT_ESTIMATE',)]
    found=model+[{**f,'variant':doc['variant']} for f in audit.get('findings',[])]+[dict(f) for f in render or []]
    found+=[{**f,'variant':doc['variant']} for f in context or []]
    for f in found:
        f['id']=fixers.finding_id(f)
        if f.get('fixer') and f['fixer'] not in fixers.FIXERS and f['fixer'] not in fixers.MODEL_FIXERS:f['fixer_planned']=f.pop('fixer')
    return found


def save_revision(directory, doc, renderer, reference=None, findings=None, context=None, measurements=None):
    audit=audit_document(doc)
    # Owner decision 49: the contrast the check of the rendering measured under the glyphs decides over the model.
    render_decides(doc,audit,measurements)
    mode=doc.get('export',{}).get('mode','standalone')
    # C4b: during generation on the carrier (findings is a list) only factual errors block; the rest stays in audit.json.
    issue=findings is not None and mode=='carrier'
    blocking=[e for e in audit['errors'] if e['code'] in FACTUAL_ERRORS] if issue else audit['errors']
    if blocking:
        raise ValueError('Экспорт остановлен аудитом: '+json.dumps(blocking,ensure_ascii=False))
    if issue:
        audit['render_findings']=findings
    audit['findings']=revision_findings(doc,audit,findings if issue else None,context)
    if mode=='carrier':
        # The reference of a run is <run>/input.pptx; a failed self-check leaves no revision behind.
        deck,report=export_carrier_pptx(doc,reference if reference is not None else (directory.parent.parent/'input.pptx').read_bytes())
    elif mode!='standalone':
        raise ValueError('Неизвестный режим экспорта')
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory/'document.json',doc)
    write_json(directory/'audit.json',audit)
    if mode=='carrier':
        # C2: text of the finished slides that the model does not have (placeholders, sample text left after cloning).
        audit['findings']+=[{**f,'variant':doc['variant'],'id':fixers.finding_id(f)} for f in package_findings(doc,deck)]
        write_json(directory/'audit.json',audit)
        (directory/'deck.pptx').write_bytes(deck)
        write_json(directory/'export.json',report)
    else:
        (directory/'deck.pptx').write_bytes(export_pptx(doc))
    (directory/'deck.html').write_text(export_html(doc,renderer),encoding='utf-8')
    write_json(directory/'checksums.json',{p.name:digest(p.read_bytes()) for p in directory.iterdir()})
    return audit


def attach_pdfs(directories, audits):
    """H2: deck.pdf of every revision printed from its deck.html in one browser session and checked (pdf.inspect_pdf).
    A failed print or check is a finding of the revision, not a refusal (C4); pdf.json keeps the report and the slides
    whose decor only PowerPoint draws (missing from the PDF as from the preview). Returns the audits with those findings."""
    try:
        reports=export_pdfs([(d/'deck.html',d/'deck.pdf') for d in directories])
    except ValueError as exc:
        reports=[{'error':str(exc)} for _ in directories]
    result=[]
    for d,report,audit in zip(directories,reports,audits):
        doc=json.loads((d/'document.json').read_text('utf-8'))
        if 'error' in report or not (d/'deck.pdf').is_file():
            checked={'pages':0,'fonts':[],'fonts_not_embedded':[],'empty_pages':[],'issues':[{'code':'PDF_INVALID','error':report.get('error','')}]}
        else:
            checked=inspect_pdf((d/'deck.pdf').read_bytes(),len(doc['slides']),doc['style']['font'])
        incomplete=[s['id'] for s in doc['slides'] if any(n.get('code')=='PREVIEW_INCOMPLETE_DECOR' for n in s.get('preview_notes',[]))]
        write_json(d/'pdf.json',{'schema':'vsp.pdf/1','method':'chromium-print-of-deck-html',
            **{k:v for k,v in report.items() if k not in ('html','pdf')},**checked,'preview_incomplete_slides':incomplete})
        audit['findings']+=[{**f,'id':fixers.finding_id(f)} for f in (finding(i,doc['variant'],'export') for i in checked['issues'])]
        write_json(d/'audit.json',audit)
        write_json(d/'checksums.json',{p.name:digest(p.read_bytes()) for p in sorted(d.iterdir()) if p.name!='checksums.json'})
        result.append(audit)
    return result


def live_note(calls):
    """Limitation of live calls: a development stand-in model (models.json, producer_kind live-stand-in) is not Qwen3.8-27B."""
    stand_in=sorted({c['producer'].get('model') or '?' for c in calls if (c.get('producer') or {}).get('kind')=='live-stand-in'})
    return ('Live calls of a development stand-in model ('+', '.join(stand_in)+'), not Qwen3.8-27B; quality and time of Qwen are not measured.'
            if stand_in else 'Live model calls.')


def template_facts(style):
    """What the reading of a template gave, for the page that shows the run (decision 55)."""
    return {'slides':style.get('slide_count'),'font':style.get('font'),'colours':len(style.get('palette') or {}),
            'observations':len(style.get('observations') or [])}


def scene_facts(markup):
    """Whether the markup of the scenes was asked now or is the one kept from the preparation of this template."""
    calls=markup['calls']
    return {'source':'none' if not calls else 'kept' if calls[0].get('provider')=='cache' else 'asked','marked':sorted(markup['accepted'])}


def prepare(template, output_root, provider=None, template_name=None, progress=None):
    """The preparation stage of a template (owner decisions 27-28, 26.09.2026): its scene markup (scenes.mark), kept for the
    generations that follow; outside the time of a generation. Returns what the markup gave and how long it took."""
    tell=progress or Quiet()
    started=time.perf_counter()
    directory=Path(output_root)/'prepare'/digest(template)[:16]
    directory.mkdir(parents=True,exist_ok=True)
    if progress is not None:llm.listen(directory,tell.request)
    try:
        tell.plan(['template','scenes'])
        tell.begin('template',bytes=len(template))
        # 29.09.2026 (template E on this machine): the markup of the preparation was kept under another key than the generation
        # looks for, and the generation asked the model again (36 s of its time). The key of a markup holds the faces that
        # draw the sample slides (scenes.cache_key), and the generation reads them through the catalogue of the fonts of the
        # server; so does the preparation now.
        style=catalog_fonts(resolve_fonts(read_style(template)),template)
        tell.end('template',**template_facts(style),typefaces=len(style['font_inventory']['families']))
        tell.begin('scenes')
        markup=scenes.mark(style,template_name or 'template',provider=provider,record_dir=directory,cache=ROOT/'var/generator/cache')
        tell.end('scenes',**scene_facts(markup))
        result={'template_sha256':digest(template),'accepted':sorted(markup['accepted']),'rejected':len(markup['rejected']),'fallback':markup['fallback'],
                'source':markup['calls'][0].get('provider') if markup['calls'] else None,'seconds':round(time.perf_counter()-started,1)}
        tell.done(seconds=result['seconds'])
        return result
    except Exception as exc:
        tell.failed(exc)
        raise
    finally:
        if progress is not None:llm.forget(directory)


def generate(template, brief, output_root, export_mode=None, provider=None, materials=None, template_name=None, progress=None):
    started=time.perf_counter()
    # Owner decision 55: the steps of the run are told to the page as they begin and end (progress.py); tell is silent
    # when nobody watches.
    tell=progress or Quiet()
    release=verify_release()
    config=json.loads((ROOT/'profiles/mvp/layouts.json').read_text('utf-8'))
    if export_mode is not None:
        config={**config,'export_mode':export_mode}
    renderer=(ROOT/'apps/generator/renderer.js').read_text('utf-8')
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    directory=Path(output_root)/run_id
    directory.mkdir(parents=True,exist_ok=False)
    manifest={'schema':'vsp.run/1','id':run_id,'state':'running','created_at':datetime.now(timezone.utc).isoformat(),
        'profile':config['profile'],'export_mode':config.get('export_mode','standalone'),'release':release,'template_sha256':digest(template),'brief_sha256':digest(json_bytes(brief)),
        'runtime':{'python':platform.python_version(),'platform':platform.platform()},'models_called':[],
        'planner':config['planner'],'timings':{},'client_display_measurements':[],
        'limitations':['Structured brief; no model planning.','First sample does not prove fidelity or 300-second SLA on 10–15 slides.']}
    if template_name:
        manifest['template_name']=str(template_name)
    write_json(directory/'run.json',manifest)
    (directory/'input.pptx').write_bytes(template)
    journal(output_root,'upload',run=run_id,release=release['id'],
        template={'name':template_name,'sha256':manifest['template_sha256'],'bytes':len(template),'stored':run_id+'/input.pptx'},
        materials=[{'name':n,'sha256':digest(d),'bytes':len(d)} for n,d in materials or []],brief_sha256=manifest['brief_sha256'])
    # Stage B: a free brief is turned into vsp.brief/1 by the agents of planner.py; every call is in run.json.
    free=brief if brief.get('schema')=='vsp.free-brief/1' else None
    # Owner decision 52 (29.09.2026): a request over BRIEF_LIMIT signs (a Habr article, item 51) is read as the material "Текст
    # запроса" over its whole text (materials.brief_text), and the request itself is a neutral line.
    if free is not None and len(str(free.get('text') or ''))>BRIEF_LIMIT:
        if len(free['text'])>REQUEST_MAX:raise ValueError(f'Текст длиннее {REQUEST_MAX:,} знаков'.replace(',',' '))
        materials=[request_material(free['text'])]+list(materials or [])
        free={**free,'text':LONG_REQUEST}
    # Decision 44: the dash of the sample unless the user names one (free brief, "dash": "en" | "em"; owner 28.09.2026 for
    # the pitch on template D, whose three em dashes stand on its instruction slides).
    dash={'en':EN_DASH,'em':EM_DASH}.get((free or {}).get('dash')) or template_dash(template)
    write_json(directory/('free_brief.json' if free else 'brief.json'),brief)
    carrier=config.get('export_mode')=='carrier'
    if progress is not None:llm.listen(directory,tell.request)
    try:
        tell.plan((['package'] if materials else [])+(['content'] if free is not None else [])+['template']+(['scenes'] if carrier else [])
                  +['icons','layout']+(['titles'] if free is not None and llm.is_live(provider) else [])
                  +['notes','meaning','render','pictures','export','pdf'])
        # Item 34 (25.09.2026): the content package of the user (decisions 17, 22). Its materials are read into text with
        # provenance (materials.read_package); a free brief reads the request of the user and then the materials
        # (materials.brief_text, with the report of what went in and what did not); a font of the package in a typeface of the
        # sample serves the preview and the measurement; the text of every slide reads the paragraphs around the quotes of
        # its facts (variant b). package.json keeps the report, not the files.
        package=None
        if materials:
            stage=time.perf_counter()
            tell.begin('package',files=len(materials))
            package=read_package(materials)
            manifest['timings']['package_seconds']=time.perf_counter()-stage
            if free is not None:
                text,used=brief_text(package,free['text'])
                if len(text)>=20:
                    free={**free,'text':text}
                    package['brief_text']=used
                    write_json(directory/'free_brief.json',free)
            write_json(directory/'package.json',package)
            manifest['content_package']={'sha256':package['sha256'],'materials':len(package['materials']),'paragraphs':package['paragraphs'],
                'chars':package['chars'],'fonts':sorted({f['family'] for f in package['fonts']}),
                'unread':[{'name':m['name'],'warnings':m['warnings']} for m in package['materials'] if m['warnings'] and not m['paragraphs']],
                # Decision 37: a PDF read in part (pages that are pictures only, pages over the limit) is not "unread".
                'partial':[{'name':m['name'],'warnings':m['warnings']} for m in package['materials'] if m['warnings'] and m['paragraphs']],
                'left_out':(package.get('brief_text') or {}).get('left_out',[])}
            tell.end('package',materials=len(package['materials']),paragraphs=package['paragraphs'],chars=package['chars'])
        if free is not None:
            stage=time.perf_counter()
            # The analyst hears which materials of the package the brief text holds (twelfth session, run P of holdout-next).
            names=[m['name'] for m in ((package or {}).get('brief_text') or {}).get('materials',[]) if m['taken']]
            tell.begin('content',chars=len(free['text']))
            brief,planning=structure(free,provider=provider,record_dir=directory,materials=names or None)
            # Decision 44 (28.09.2026): the en dash in the texts the agents wrote, unless the sample itself sets the em dash.
            brief=with_dash(brief,dash)
            manifest['timings']['planning_seconds']=time.perf_counter()-stage
            manifest['planner']='free-brief-agents-v1'
            manifest['models_called']=planning['calls']
            manifest['planning']={'fallbacks':planning['fallbacks'],'skipped':planning['skipped'],'omitted':planning.get('omitted',[]),'single_fact_slides':planning['single_fact_slides'],
                'charts':planning.get('charts',0),'metrics':planning.get('metrics',0),'processes':planning.get('processes',0),
                # Owner decision 47: statements taken from the text of the user and added by content_writer (the page reports both).
                'supplement':planning.get('supplement')}
            providers=sorted({c['provider'] for c in planning['calls']})
            manifest['limitations']=['Free brief planned by brief_analyst, outline_planner, slide_writer and chart_planner (G1) with deterministic checks and fallbacks.',
                'Provider replay returns recorded answers of a developer stand-in model, not Qwen3.8-27B; quality and time of Qwen are not measured.' if 'replay' in providers else live_note(planning['calls'])]
            write_json(directory/'brief.json',brief)
            tell.end('content',slides=len(brief['sections'])+1,statements=sum(len(x['facts']) for x in brief['sections']),
                     added=(planning.get('supplement') or {}).get('added',0),charts=planning.get('charts',0),numbers=planning.get('metrics',0),
                     refused=len(planning['fallbacks']))
        stage=time.perf_counter()
        tell.begin('template',bytes=len(template))
        style=resolve_fonts(read_style(template))
        if package is not None:
            style=package_fonts(style,package,materials)
        # Item 38 (owner decision 24, 25.09.2026): every typeface of the template with its source; the faces the template
        # embeds for its other typefaces; else the same family or a metric-compatible substitute from the fonts of the server.
        style=catalog_fonts(style,template)
        inventory=style['font_inventory']
        manifest['fonts']={f['family']:f.get('source') for f in inventory['families']}
        journal(output_root,'fonts',run=run_id,template_sha256=manifest['template_sha256'],main=style['font'],
            families=[{k:f[k] for k in ('family','scripts','parts','chars','source','served_by','files','embedded','embedded_not_used') if k in f}
                      for f in inventory['families']],
            embedded=inventory['embedded'],catalog={'dirs':inventory['catalog_dirs'],'faces':inventory['catalog_faces']},
            unavailable=[f['family'] for f in inventory['families'] if f.get('source')=='unavailable'])
        manifest['timings']['import_seconds']=time.perf_counter()-stage
        tell.end('template',**template_facts(style),typefaces=len(inventory['families']))
        # Owner decisions 27-28 (26.09.2026): the scene markup of the preparation stage (scenes.py). A prepared template (and
        # recorded answers) read the kept markup; an unprepared one is marked here on a live provider (this counts in the time
        # of the run); without a model the markup is empty and the deck is built as before.
        stage=time.perf_counter()
        if carrier:
            tell.begin('scenes')
            markup=scenes.mark(style,template_name or 'template',provider=provider,record_dir=directory,cache=ROOT/'var/generator/cache')
            tell.end('scenes',**scene_facts(markup))
            style['scene_markup']={k:markup[k] for k in ('key','accepted','rejected','fallback')}
            manifest['models_called']=manifest['models_called']+markup['calls']
            manifest['scene_markup']={'key':markup['key'],'accepted':sorted(markup['accepted']),'rejected':len(markup['rejected']),'fallback':markup['fallback'],
                                      'source':(markup['calls'][0].get('provider') if markup['calls'] else None)}
        manifest['timings']['scene_markup_seconds']=time.perf_counter()-stage
        # G3: pictograms of the sample for the facts (icon_tagger once per sample, cached for live calls; icon_picker).
        stage=time.perf_counter()
        live=(provider or json.loads((ROOT/llm.MODELS).read_bytes().decode('utf-8'))['default_provider'])!='replay'
        tell.begin('icons')
        chosen=choose_icons(brief,style,provider=provider,record_dir=directory,cache=ROOT/'var/generator/cache' if live else None)
        tell.end('icons',found=chosen['icons'],described=chosen['tagged'],slides=chosen['slides'])
        manifest['timings']['icons_seconds']=time.perf_counter()-stage
        manifest['models_called']=manifest['models_called']+chosen['calls']
        manifest['icons']={k:chosen[k] for k in ('icons','tagged','slides','fallbacks')}
        write_json(directory/'style.json',style)
        # Twelfth session (25.09.2026, docs/LLM_AGENT_ROLE_2026-09-25.md): titles the planner wrote that the template sets far
        # below its own size are rewritten within what their frames hold (agent title_fitter, titles.py), before the text of
        # every slide and the contextual audit read them. The template is read before them for this; a structured brief keeps
        # the titles of its author. The sizes are those the check of the rendering sets: the first layout estimates lines
        # conservatively, and the check grows a title back to its own size where the browser fits it (template A: all 11 titles
        # below 85 % in some variant after the layout, 2 of 33 titles of the variants after the check; template B: 33 of 33). Only a live model
        # is asked; with recorded answers the documents of this first layout are issued as before.
        stage=time.perf_counter()
        tell.begin('layout')
        documents=issue_order(serve_fonts(build_documents(brief,style,config),style),config)
        manifest['timings']['layout_seconds']=time.perf_counter()-stage
        tell.end('layout',variants=[d['variant'] for d in documents],slides=len(documents[0]['slides']),
                 objects=sum(len(x['elements']) for d in documents for x in d['slides']))
        if free is not None and llm.is_live(provider):
            stage=time.perf_counter()
            tell.begin('titles')
            measured=apply_repairs(documents,inspect_documents(documents,repair=True,min_body=config['min_body_size_px']))
            fitted,title_fit=fit_titles(brief,measured,provider=provider,record_dir=directory)
            manifest['timings']['title_fit_seconds']=time.perf_counter()-stage
            manifest['models_called']=manifest['models_called']+title_fit['calls']
            manifest['title_fit']={k:title_fit[k] for k in ('shrunk','asked','fitted','fallbacks')}
            if fitted is not brief:
                brief=with_dash(fitted,dash)
                write_json(directory/'brief.json',brief)
                stage=time.perf_counter()
                documents=issue_order(serve_fonts(build_documents(brief,style,config),style),config)
                manifest['timings']['layout_seconds']+=time.perf_counter()-stage
            tell.end('titles',small=title_fit['shrunk'],asked=title_fit['asked'],rewritten=len(title_fit['fitted']),laid_out_again=fitted is not brief)
        gaps=subset_gaps(style,documents)
        if gaps:
            manifest['subset_font_gaps']=gaps
        # Text of every slide (owner decision 21, answer 1a): one call per brief, as the variants share facts, titles and
        # order; it needs only the brief, so it runs beside the contextual audit.
        def timed_notes():
            begun=time.perf_counter()
            try:
                result=write_notes(brief,minutes=brief.get('talk_minutes') or (free or {}).get('talk_minutes'),purpose=(free or {}).get('purpose',''),
                                   provider=provider,record_dir=directory,sources=slide_sources(package,brief))
            except Exception:
                tell.end('notes',broken=True)
                raise
            tell.end('notes',seconds=time.perf_counter()-begun,words=result[0]['words'],minutes=result[0]['minutes'],slides=len(result[0]['slides']))
            return result,time.perf_counter()-begun
        tell.begin('notes')
        notes_pool=ThreadPoolExecutor(max_workers=1)
        pending_notes=notes_pool.submit(timed_notes)
        notes_pool.shutdown(wait=False)
        # C7: contextual audit of the content, once per brief: the three variants share facts, titles and order.
        stage=time.perf_counter()
        tell.begin('meaning',slides=len(brief['sections']))
        context=review(brief,provider=provider,record_dir=directory)
        manifest['timings']['context_audit_seconds']=time.perf_counter()-stage
        tell.end('meaning',findings=len(context['findings']),unanswered=len(context['fallbacks']))
        manifest['models_called']=manifest['models_called']+context['calls']
        manifest['context_audit']={'fallbacks':context['fallbacks'],'findings':len(context['findings']),'not_asked':context['not_asked']}
        if context['calls']:
            manifest['limitations']=manifest['limitations']+['Contextual audit (C7) by context_auditor on the text of slides and the quotes of the brief; question 6 by image_auditor on the pictures of the slides of every variant. '
                +('Provider replay returns recorded answers of a developer stand-in model, not Qwen3.8-27B; a slide without a record has no contextual findings.' if 'replay' in {c['provider'] for c in context['calls']} else live_note(context['calls']))]
        context_findings=[context_finding(f) for f in context['findings']]
        stage=time.perf_counter()
        (notes,notes_report),notes_seconds=pending_notes.result()
        manifest['timings']['notes_wait_seconds']=time.perf_counter()-stage
        manifest['timings']['notes_seconds']=notes_seconds
        manifest['models_called']=manifest['models_called']+notes_report['calls']
        manifest['speaker_notes']={'agent':notes_report.get('agent','speaker_notes'),'minutes':notes['minutes'],'words':notes['words'],'words_total':notes['words_total'],
            'agent_slides':notes_report['agent_slides'],'fallback_slides':notes_report['fallback_slides'],
            'fallbacks':notes_report['fallbacks'],'warnings':notes_report.get('warnings',[])}
        if free is not None:notes=with_dash(notes,dash)
        write_json(directory/'speaker_notes.json',notes)
        for doc in documents:doc['speaker_notes']=copy.deepcopy(notes)
        findings=[]
        tell.begin('render',variants=len(documents))
        for doc in documents:
            preliminary=audit_document(doc)
            # C4b: on the carrier non-factual errors of the preliminary audit are issued as findings.
            blocking=[e for e in preliminary['errors'] if e['code'] in FACTUAL_ERRORS] if carrier else preliminary['errors']
            if blocking:raise ValueError('Проверка содержания и геометрии: '+json.dumps(blocking,ensure_ascii=False))
            findings+=[(doc['variant'],e) for e in preliminary['errors']]
        stage=time.perf_counter()
        quality=inspect_documents(documents,repair=True,min_body=config['min_body_size_px'])
        documents=apply_repairs(documents,quality)
        # Re-render serialized patched documents from scratch: a patch cannot
        # pass merely because of incidental state in its search page.
        recheck=inspect_documents(documents,repair=False,min_body=config['min_body_size_px'])
        measured={d['variant']:item.get('measurements',[]) for d,item in zip(documents,recheck['outputs'])}
        # Owner decision 49: an error of contrast of the preliminary audit on a text the rendering measured readable is not issued.
        decided={d['variant']:{(x['slide'],x['element'],x['code']) for x in render_decides(d,{'errors':[e for v,e in findings if v==d['variant']]},measured[d['variant']])}
                 for d in documents}
        findings=[finding(e,v,'audit') for v,e in findings if (e.get('slide'),e.get('element'),e.get('code')) not in decided.get(v,set())]
        quality['recheck_outputs']=recheck['outputs']
        quality['diversity']=audit_diversity(documents)
        # C4a, 3: on the carrier too few distinct compositions are issued with a warning instead of a refusal.
        diversity_warning=config.get('export_mode')=='carrier' and not quality['diversity']['passed']
        quality['passed']=all(r['passed'] for r in recheck['outputs']) and (quality['diversity']['passed'] or diversity_warning)
        warnings=[{'code':'DIVERSITY_LOW','distinct_compositions':quality['diversity']['distinct_compositions'],'duplicates':quality['diversity']['duplicates']}] if diversity_warning else []
        if warnings:quality['warnings']=warnings
        write_json(directory/'quality.json',quality)
        manifest['timings']['render_check_seconds']=time.perf_counter()-stage
        manifest['render_runtime']=quality['runtime']
        manifest['quality']={'passed':quality['passed'],'distinct_compositions':quality['diversity']['distinct_compositions'],
            'warnings':[w for r in recheck['outputs'] for w in r['warnings']]+warnings}
        for doc,item in zip(documents,recheck['outputs']):
            doc['render_quality']={k:v for k,v in item.items() if k not in ('measurements','patches')}
        # C4b: on the carrier a failed capacity check after repairs issues the decks with findings.
        # A missing checker still raises in inspect_documents: a mandatory check not run is no finding.
        render_findings={d['variant']:[finding(e,d['variant'],'render') for e in item['after']] for d,item in zip(documents,recheck['outputs'])} if carrier else {}
        findings+=[f for v in render_findings.values() for f in v]
        if findings:
            manifest['quality']['passed']=False
            manifest['issued_with_findings']=findings
        tell.end('render',seconds=manifest['timings']['render_check_seconds'],texts=sum(len(m) for m in measured.values()),
                 repairs=sum(len(o.get('patches') or []) for o in quality['outputs']),findings=len(findings),chromium=quality['runtime'].get('chromium'))
        if not quality['passed'] and not carrier:
            diagnostics=directory/'diagnostics';diagnostics.mkdir()
            for doc in documents:write_json(diagnostics/(doc['variant']+'.json'),doc)
            raise ValueError(failure_message(quality))
        # C7, question 6: the pictures the variants put next to the facts, asked once per slide and set of pictures.
        stage=time.perf_counter()
        tell.begin('pictures')
        images=review_images(documents,provider=provider,record_dir=directory)
        tell.end('pictures',asked=len(images['calls']),findings=sum(len(f) for f in images['findings'].values()))
        manifest['timings']['image_audit_seconds']=time.perf_counter()-stage
        manifest['models_called']=manifest['models_called']+images['calls']
        manifest['context_audit']['images']={'calls':len(images['calls']),'fallbacks':images['fallbacks'],
                                             'findings':{v:len(f) for v,f in images['findings'].items()}}
        stage=time.perf_counter()
        audits=[]
        tell.begin('export',variants=len(documents))
        for doc in documents:
            doc['run_id']=run_id
            audits.append(save_revision(directory/doc['variant']/'r0',doc,renderer,findings=render_findings[doc['variant']] if carrier else None,
                                        context=context_findings+[context_finding(f) for f in images['findings'][doc['variant']]],
                                        measurements=measured[doc['variant']]))
        manifest['timings']['audit_export_seconds']=time.perf_counter()-stage
        tell.end('export',checks=sum(c.get('status')=='active' for c in checks().values()),findings={d['variant']:len(a['findings']) for d,a in zip(documents,audits)},
                 pptx_bytes={d['variant']:(directory/d['variant']/'r0'/'deck.pptx').stat().st_size for d in documents})
        # H2: the PDF of every variant, printed from its HTML in one browser session.
        stage=time.perf_counter()
        tell.begin('pdf',variants=len(documents))
        audits=attach_pdfs([directory/d['variant']/'r0' for d in documents],audits)
        manifest['timings']['pdf_seconds']=time.perf_counter()-stage
        tell.end('pdf',pages={d['variant']:json.loads((directory/d['variant']/'r0'/'pdf.json').read_text('utf-8')).get('pages') for d in documents})
        # C2, C3: findings of the checks of Appendix 1 by severity and code, per variant (details in <variant>/r0/audit.json).
        manifest['audit_findings']={d['variant']:dict(Counter(f"{f['severity']}:{f['code']}" for f in a['findings'])) for d,a in zip(documents,audits)}
        manifest['timings']['server_pipeline_seconds']=time.perf_counter()-started
        manifest['state']='succeeded'
        manifest['variants']=[d['variant'] for d in documents]
        manifest['output_hashes']={str(p.relative_to(directory)).replace('\\','/'):digest(p.read_bytes()) for p in directory.glob('*/r0/*')}
        write_json(directory/'run.json',manifest)
        journal(output_root,'done',run=run_id,state='succeeded',seconds=round(manifest['timings']['server_pipeline_seconds'],2))
        tell.done(seconds=round(manifest['timings']['server_pipeline_seconds'],2),requests=len(manifest['models_called']),
                  slides=len(documents[0]['slides']),variants=len(documents),findings=sum(len(a['findings']) for a in audits))
        return {'run':manifest,'documents':documents,'audits':audits,'diagnostics':style['diagnostics']}
    except Exception as exc:
        manifest['state']='failed'
        manifest['error']=str(exc)
        manifest['timings']['server_pipeline_seconds']=time.perf_counter()-started
        write_json(directory/'run.json',manifest)
        journal(output_root,'done',run=run_id,state='failed',error=f'{type(exc).__name__}: {exc}')
        tell.failed(exc)
        raise
    finally:
        if progress is not None:llm.forget(directory)


def revise(directory, original, proposed):
    verify_release()
    # Only geometry, text/cell values and typography are editable in v1.
    # Inputs, source links, image bytes and fact IDs remain server-owned.
    doc=copy.deepcopy(original)
    by_id={s['id']:s for s in doc['slides']}
    if len(proposed['slides'])!=len(by_id) or {s['id'] for s in proposed['slides']}!=set(by_id):
        raise ValueError('Состав слайдов менять пока нельзя')
    ordered=[]
    for changed in proposed['slides']:
        slide=by_id[changed['id']]
        elements={e['id']:e for e in slide['elements']}
        if len(changed['elements'])!=len(elements) or {e['id'] for e in changed['elements']}!=set(elements):
            raise ValueError('Состав объектов менять пока нельзя')
        for change in changed['elements']:
            e=elements[change['id']]
            if e.get('locked'):
                if change != e:
                    raise ValueError('Элемент оформления образца защищен от изменения')
                continue
            allowed=['x','y','w','h'] + (['text','font_size'] if e['type']=='text' else ['rows','font_size'] if e['type']=='table' else [])
            for key in allowed:
                e[key]=copy.deepcopy(change[key])
        ordered.append(slide)
    doc['slides']=ordered
    # Text of every slide: the editor changes the words of a note, not which slides have one.
    proposed_notes=(proposed.get('speaker_notes') or {}).get('slides')
    if doc.get('speaker_notes') and proposed_notes is not None:
        if set(proposed_notes)!=set(doc['speaker_notes']['slides']):
            raise ValueError('Состав заметок к слайдам менять нельзя')
        for sid,note in proposed_notes.items():
            text=note.get('text') if isinstance(note,dict) else None
            if not isinstance(text,str) or len(text)>4000:
                raise ValueError('Неверный текст заметки к слайду')
            if text!=doc['speaker_notes']['slides'][sid]['text']:
                doc['speaker_notes']['slides'][sid]={'text':text.strip(),'source':'user'}
        doc['speaker_notes']['words']=sum(len(n['text'].split()) for n in doc['speaker_notes']['slides'].values())
    for n,slide in enumerate(doc['slides'],1):
        for element in slide['elements']:
            if element['id']=='page':
                element['text']=str(n)
    next_revision=max(int(p.name[1:]) for p in directory.glob('r[0-9]*'))+1
    doc['revision']=next_revision
    doc['parent_revision']=original['revision']
    doc['edited_at']=datetime.now(timezone.utc).isoformat()
    doc['edit_release']=verify_release()
    audit=audit_document(doc)
    quality=inspect_documents([doc],repair=False)
    # Owner decision 49: the contrast the check of the rendering measured under the glyphs decides over the model.
    render_decides(doc,audit,quality['outputs'][0].get('measurements'))
    if audit['errors']:raise ValueError('Экспорт остановлен аудитом: '+json.dumps(audit['errors'],ensure_ascii=False))
    if not quality['outputs'][0]['passed']:
        raise ValueError('Сохранение остановлено проверкой отрисовки: '+json.dumps(quality['outputs'][0]['after'],ensure_ascii=False))
    doc['render_quality']={k:v for k,v in quality['outputs'][0].items() if k not in ('measurements','patches')}
    reference=(directory.parent/'input.pptx').read_bytes() if doc.get('export',{}).get('mode')=='carrier' else None
    # C7: the content did not change its meaning: the contextual findings of the parent revision stay.
    context=[f for f in json.loads((directory/f"r{original['revision']}"/'audit.json').read_text('utf-8')).get('findings',[]) if f.get('check')=='context']
    audit=save_revision(directory/f'r{next_revision}',doc,(ROOT/'apps/generator/renderer.js').read_text('utf-8'),reference,context=context,
                        measurements=quality['outputs'][0].get('measurements'))
    audit=attach_pdfs([directory/f'r{next_revision}'],[audit])[0]
    return {'document':doc,'audit':audit}


# Rehearsal 4 of the video (29.09.2026, template E): the answer repeated the title, and the page showed the record of the refusal
# ([{"finding": ..., "problems": [{"code": "TITLE_UNCHANGED"}]}]). The reasons are told in the words of the page.
REFUSALS={'no-fixer':'у находки нет автоматического исправления','not-editable':'объект образца защищён от изменения',
          'no-value-of-the-reference-fits':'ни одно значение из образца не подошло','no-title':'на слайде нет заголовка'}


def refusal(skipped):
    """Why none of the chosen fixes was applied."""
    reasons=[]
    for item in skipped:
        if item.get('reason')=='model':
            codes={p.get('code') for p in item.get('problems') or []}
            reason='ответ повторил прежний заголовок' if codes=={'TITLE_UNCHANGED'} else 'новый заголовок не прошёл проверку' if codes else 'новый заголовок не получен'
        else:
            reason=REFUSALS.get(item.get('reason'),'исправление не подошло')
        if reason not in reasons:reasons.append(reason)
    return 'Ни одно выбранное исправление не применено: '+'; '.join(reasons)+'. Повторите исправление или поправьте текст в редакторе.'


def fix(directory, original, finding_ids, provider=None, progress=None):
    """C5, C6: apply the fixers of the findings the user chose in revision `original` of a variant; the result is a new
    revision rN made as revise() makes it, audited again. Reports which findings went away, which stay and which are new.
    Model fixers (rewrite-title) call their agent; every finding they answer on the slide is resolved in the new revision,
    without asking the contextual audit again."""
    verify_release()
    tell=progress or Quiet()
    if progress is not None:llm.listen(directory.parent,tell.request)
    try:
        result=_fix(directory,original,finding_ids,provider,tell)
        tell.done(revision=result['document']['revision'],fixed=len(result['fixed']),not_fixed=len(result['skipped']),
                  gone=len(result['resolved']),new=len(result['new']))
        return result
    except Exception as exc:
        tell.failed(exc)
        raise
    finally:
        if progress is not None:llm.forget(directory.parent)


def _fix(directory, original, finding_ids, provider, tell):
    before=json.loads((directory/f"r{original['revision']}"/'audit.json').read_text('utf-8')).get('findings',[])
    wanted=set(finding_ids)
    chosen=[f for f in before if f['id'] in wanted]
    if not chosen:
        raise ValueError('Не выбраны находки этой версии')
    doc=copy.deepcopy(original)
    model=[f for f in chosen if f.get('fixer') in fixers.MODEL_FIXERS]
    tell.plan((['fixers'] if len(model)<len(chosen) else [])+(['title'] if model else [])+['render','export','pdf'])
    if len(model)<len(chosen):tell.begin('fixers',findings=len(chosen)-len(model))
    done,skipped=fixers.apply(doc,[f for f in chosen if f not in model])
    if len(model)<len(chosen):tell.end('fixers',fixed=len(done),not_fixed=len(skipped))
    if model:tell.begin('title',slides=len({f['slide'] for f in model}))
    resolved=set()
    titles=title_findings([{**f,'reason':f['message']} for f in before if f.get('check')=='context'])
    for sid in sorted({f['slide'] for f in model}):
        slide=next((s for s in doc['slides'] if s['id']==sid),None)
        element=next((e for e in (slide or {}).get('elements',[]) if e['id']=='title'),None)
        if element is None or sid not in titles:
            skipped+=[{'finding':f['id'],'reason':'no-title'} for f in model if f['slide']==sid];continue
        payload=title_payload(doc['brief'],sid,element['text'],titles[sid])
        title,record,problems=rewrite_title(payload,provider=provider,record_dir=directory.parent)
        if title is None:
            skipped+=[{'finding':f['id'],'reason':'model','problems':problems} for f in model if f['slide']==sid];continue
        call={k:record.get(k) for k in ('agent','request_sha256','prompt_version','provider','ok')}
        done.append({'finding':'|'.join(f['id'] for f in titles[sid]),'fixer':'rewrite-title','slide':sid,'change':{'title':[element['text'],title]},'call':call})
        element['text']=title
        resolved|={f['id'] for f in before if f.get('check')=='context' and f['slide']==sid and fixers.finding_id(f) in {fixers.finding_id(t) for t in titles[sid]}}
    if model:tell.end('title',rewritten=sum(d['fixer']=='rewrite-title' for d in done),not_fixed=sum(x.get('reason') in ('model','no-title') for x in skipped))
    if not done:
        raise ValueError(refusal(skipped))
    next_revision=max(int(p.name[1:]) for p in directory.glob('r[0-9]*'))+1
    doc['revision']=next_revision
    doc['parent_revision']=original['revision']
    doc['edited_at']=datetime.now(timezone.utc).isoformat()
    doc['edit_release']=verify_release()
    doc['fixes']=done
    carrier=doc.get('export',{}).get('mode')=='carrier'
    tell.begin('render',variants=1)
    quality=inspect_documents([doc],repair=False)
    # A title the model rewrote may be longer than the frame holds: the browser measures it, and an overflowing one takes
    # the next smaller size of the scale of the reference (the fixer fit-text), measured again, at most three times.
    rewritten={d['slide'] for d in done if d['fixer']=='rewrite-title'}
    for _ in range(3):
        over=[o for o in quality['outputs'][0]['after'] if o['code']=='TEXT_OVERFLOW' and o.get('element')=='title' and o.get('slide') in rewritten]
        changed=False
        for o in over:
            slide=next(s for s in doc['slides'] if s['id']==o['slide'])
            element=next(e for e in slide['elements'] if e['id']=='title')
            change=fixers.fit_text(doc,slide,element)
            if change:
                changed=True
                next(d for d in done if d['fixer']=='rewrite-title' and d['slide']==o['slide']).setdefault('fit',[]).append(change)
        if not changed:break
        quality=inspect_documents([doc],repair=False)
    doc['render_quality']={k:v for k,v in quality['outputs'][0].items() if k not in ('measurements','patches')}
    tell.end('render',texts=len(quality['outputs'][0].get('measurements') or []),findings=len(quality['outputs'][0]['after']),
             chromium=quality['runtime'].get('chromium'))
    if not carrier and not quality['outputs'][0]['passed']:
        raise ValueError('Исправление остановлено проверкой отрисовки: '+json.dumps(quality['outputs'][0]['after'],ensure_ascii=False))
    render=[finding(e,doc['variant'],'render') for e in quality['outputs'][0]['after']] if carrier else None
    reference=(directory.parent/'input.pptx').read_bytes() if carrier else None
    context=[f for f in before if f.get('check')=='context' and f['id'] not in resolved]
    tell.begin('export',variants=1)
    audit=save_revision(directory/f'r{next_revision}',doc,(ROOT/'apps/generator/renderer.js').read_text('utf-8'),reference,findings=render,context=context,
                        measurements=quality['outputs'][0].get('measurements'))
    tell.end('export',checks=sum(c.get('status')=='active' for c in checks().values()),findings={doc['variant']:len(audit['findings'])},
             pptx_bytes={doc['variant']:(directory/f'r{next_revision}'/'deck.pptx').stat().st_size})
    tell.begin('pdf',variants=1)
    audit=attach_pdfs([directory/f'r{next_revision}'],[audit])[0]
    tell.end('pdf',pages={doc['variant']:json.loads((directory/f'r{next_revision}'/'pdf.json').read_text('utf-8')).get('pages')})
    old={f['id'] for f in before};new={f['id'] for f in audit['findings']}
    return {'document':doc,'audit':audit,'fixed':done,'skipped':skipped,
            'resolved':sorted(old-new),'remaining':sorted(old&new),'new':sorted(new-old)}
