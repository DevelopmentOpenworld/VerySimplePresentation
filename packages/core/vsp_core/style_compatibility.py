"""Conservative evidence for layouts belonging to an observed visual family."""
import hashlib
import json


def decoration_signature(element):
    fields=('type','x','y','w','h','geometry','fill','opacity','mime','data','crop')
    payload={k:element[k] for k in fields if k in element}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def decorative_keys(pattern):
    keys=set()
    for d in pattern['decorations']:
        # Item 37, cause 3: the lines and undrawable logos a sample slide draws itself (patterns.py) keep the families as
        # they were; they are decor of that slide, not evidence for layouts.
        if d.get('source',{}).get('kind') in ('slide-line','unrendered-slide-picture'):continue
        if d.get('asset'):keys.add(('asset',d['asset']))
        elif d.get('fill'):
            color=d['fill'];channels=bytes.fromhex(color)
            if max(channels)-min(channels)>30:keys.add(('fill',color))
    return keys


def filter_layouts(patterns,used_layouts=()):
    """Unused masters or generic Office layouts do not establish a brand style. A layout some sample slide stands on does
    (G3, blind test of 23.09), even when that slide gives no composition of its own."""
    observed=[p for p in patterns if p['source_kind']=='slide']
    kept=[];rejected=[]
    for p in patterns:
        if p['source_kind']=='slide':
            p['compatibility']={'basis':'observed-slide'};kept.append(p);continue
        # Complete compositions are anchored by complete observed slides only, as before A6 (part);
        # a composition with undrawable inherited decor (preview_complete False) by any observed slide.
        complete=p.get('preview_complete',True)
        anchors=[a for a in observed if a['master_part']==p['master_part'] and (not complete or a.get('preview_complete',True))]
        keys=decorative_keys(p)
        matches=[a for a in anchors if keys & decorative_keys(a)]
        if not matches:
            # Plain styles require an observed plain slide with the same paint
            # and typography. Shared white background alone is not evidence.
            matches=[a for a in anchors if not decorative_keys(a) and not keys
                and (a['background'],a['title']['font'],a['title']['color'])==
                    (p['background'],p['title']['font'],p['title']['color'])]
        if matches:
            p['compatibility']={'basis':'shared-decoration' if keys else 'observed-plain-style',
                                'anchors':[a['id'] for a in matches]}
            kept.append(p)
        elif p['source_part'] in used_layouts:
            p['compatibility']={'basis':'layout-used-by-sample-slide'}
            kept.append(p)
        else:
            rejected.append({'id':p['id'],'source_part':p['source_part'],'master_part':p['master_part'],
                             'reason':'no-observed-compatible-style',**({} if complete else {'preview_complete':False})})
    return kept,rejected
