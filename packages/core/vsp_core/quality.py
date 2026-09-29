"""Mandatory local browser preflight, independent of PPTX serialization."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[3]

def failure_message(report):
    issues=[e for r in report['recheck_outputs'] for e in r['after']]
    codes={e['code'] for e in issues};reasons=[]
    if 'FONT_UNAVAILABLE' in codes:
        reasons.append('недоступны нужные шрифты: '+', '.join(sorted({e['family'] for e in issues if e['code']=='FONT_UNAVAILABLE'})))
    if codes & {'TEXT_OVERFLOW','TABLE_OVERFLOW'}:reasons.append('текст не помещается в выбранные области')
    if 'TEXT_COLLISION' in codes:reasons.append('тексты наезжают друг на друга')
    if 'LOW_RENDERED_CONTRAST' in codes:reasons.append('часть текста не читается на оформлении образца')
    if 'BRAND_ASSET_OVERLAP' in codes:reasons.append('текст пересекается с защищенным элементом оформления')
    if 'CARD_TEXT_MARGIN' in codes:reasons.append('недостаточны отступы текста внутри карточек')
    if 'RASTER_SURFACE_MARGIN' in codes:reasons.append('текст выходит за безопасную область визуальной подложки')
    if not report['diversity']['passed']:reasons.append('не удалось получить три различающиеся композиции')
    return 'Комплект не выдан: '+'; '.join(reasons)+'. Попробуйте другой образец или скорректируйте содержание.'

def node_runtime():
    """Node executable and environment of the browser workers (render check, PDF export)."""
    env=os.environ.copy()
    bundled=Path(sys.executable).parent.parent/'node'
    node=env.get('VSP_NODE_EXECUTABLE') or (str(bundled/'bin/node.exe') if (bundled/'bin/node.exe').is_file() else shutil.which('node'))
    if not node:raise ValueError('Проверка отрисовки недоступна: задайте VSP_NODE_EXECUTABLE и установите зависимости apps/generator/package.json')
    if not env.get('VSP_NODE_MODULES') and (bundled/'node_modules/playwright').is_dir():env['VSP_NODE_MODULES']=str(bundled/'node_modules')
    return node,env

def inspect_documents(documents,repair=False,min_body=19):
    node,env=node_runtime()
    try:
        process=subprocess.run([node,str(ROOT/'apps/generator/quality_worker.mjs')],input=json.dumps({'documents':documents,'options':{'repair':repair,'minBody':min_body}},ensure_ascii=False),
            capture_output=True,text=True,encoding='utf-8',env=env,timeout=120,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    except (OSError,subprocess.TimeoutExpired) as exc:raise ValueError('Обязательная проверка отрисовки не выполнена: '+str(exc)) from exc
    if process.returncode:raise ValueError('Обязательная проверка отрисовки не выполнена: '+process.stderr[-1800:])
    report=json.loads(process.stdout)
    if len(report.get('outputs',[]))!=len(documents):raise ValueError('Неполный ответ проверки отрисовки')
    return report

RECOLOUR_NEUTRALS={'151515','FFFFFF'}

def recolour_allowed(doc):
    """Colours a text may take on a picture (item 35, H2g; item 37, cause 5): of the template or of a text of the document,
    and the neutral ones of the render check (quality.js, qRecolours)."""
    colours={str(c).upper() for c in (doc.get('template') or {}).get('colours',[])}|RECOLOUR_NEUTRALS
    return colours|{str(e['color']).upper() for s in doc['slides'] for e in s['elements'] if e['type']=='text' and e.get('color')}

def recolourable(element):
    """Text that may take a colour of the template where it read nowhere in its own (quality.js, qRecolourable): a title or
    subtitle (item 35, H2g), text in a layout placeholder or set by the product (item 37, cause 5); not the footer, decor
    text or text cloned from a sample slide."""
    kind=(element.get('binding') or {}).get('kind')
    return element['id'] in ('title','subtitle') or (element['type']=='text' and element['id'] not in ('disclosure','page')
                                                     and not element.get('decor') and kind in ('placeholder','native',None))

def apply_repairs(documents,report):
    result=copy.deepcopy(documents)
    for doc,item in zip(result,report['outputs']):
        if item['variant']!=doc['variant']:raise ValueError('Проверка относится к другому варианту')
        allowed=recolour_allowed(doc)
        for patch in item['patches']:
            slide=next(s for s in doc['slides'] if s['id']==patch['slide'])
            element=next(e for e in slide['elements'] if e['id']==patch['element'])
            # G3c (proposals of 24.09.2026): a title moved off the picture of the reference takes the content under it along;
            # that shift may move the frame of a card, a chart or a table of the slide, never inherited or cloned decor.
            # Item 40: so does a title that grows over the content of its column (TITLE_PUSH, quality.js qTitlePush).
            shift=patch.get('reasons') in (['TITLE_OFF_RASTER'],['TITLE_PUSH']) and element['type']!='text' and set(patch['after'])=={'x','y','w','h'} \
                and (element.get('binding') or {}).get('kind') not in ('inherited','furniture','clone') and not element.get('background_decoration')
            # The footer of the product (disclosure, page number) may also take another colour where it moves (quality.js,
            # qCandidates): it is not text of the reference. Item 35, H2g: so may a title or subtitle that read nowhere in its
            # own colour on the picture of the reference, only a colour of the template, of the document or a neutral one;
            # item 37, cause 5: so may other text in a placeholder or set by the product (recolourable).
            recolour=recolourable(element) and patch.get('recolour') is True and 'LOW_RENDERED_CONTRAST' in (patch.get('reasons') or []) \
                and str(patch['after'].get('color','')).upper() in allowed
            keys={'x','y','w','h','font_size'}|({'color'} if (element['id'] in ('disclosure','page') or recolour) and 'color' in patch['after'] else set())
            if not shift and (element['type']!='text' or element.get('locked') and patch.get('reasons')!=['TITLE_OFF_RASTER'] or set(patch['after'])!=keys):
                raise ValueError('Недопустимая правка проверяющего')
            if any(element[k]!=v for k,v in patch['before'].items()):raise ValueError('Правка относится к другому состоянию объекта')
            element.update(patch['after'])
            element.setdefault('quality_adjustments',[]).append(patch)
        doc['render_quality']={k:v for k,v in item.items() if k not in ('measurements','patches')}
    return result
