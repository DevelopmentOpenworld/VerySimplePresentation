"""The model in the analysis of the sample (owner decisions 27-28, 26.09.2026; research:
analysis/style-experiments/20260925-model-input-analysis): the preparation stage marks the scenes of the sample slides.

1. Sample slides are chosen by one deterministic rule (the cover, the first slide of every other layout, evenly spread
   others; 6 to 10).
2. Each is rebuilt from its composition as a document of our own model (background, decor, text frames with the text of
   the sample) and drawn by renderer.js in Chromium (apps/generator/render_worker.mjs): the server has no PowerPoint.
3. What the drawing cannot show (a vector logo of the sample slide, undrawable decor of its layout or master, a gradient
   drawn as its mean colour) is described in words from the file, with its box, next to the pictures (not_drawn).
4. One call of the agent scene_marker; its answer is used only where the file and the pixels of the pictures confirm it
   (accept). The answer is kept with its pictures (cache_key: template, prompt, schema, what is drawn), so a repeated run of the
   same template reads it back and gives the same deck, recorded answers (provider replay) read it too, and the checks run
   again on every read.
Without a model, without an answer or with a rejected one the product works as before.
"""
import base64
import functools
import json
from pathlib import Path
import subprocess
import os
import threading

from . import llm
from . import png
from .patterns import contrast
from .quality import node_runtime

ROOT = Path(__file__).resolve().parents[3]
MAX_SAMPLES = 10
# One markup of a template at a time: a generation that starts while the preparation of the same template runs waits for it
# and reads its answer rather than asking the model again.
_LOCKS, _GUARD = {}, threading.Lock()


def select_samples(carrier):
    """Numbers of the sample slides shown to the model: the opening slide, the first slide of every other layout in slide
    order (with more than 10 layouts: the cover and 9 of them evenly spread), then evenly spaced remaining slides until 6; at
    most 10. Slides of instructions at the start of the sample are not in carrier['slides'] (reference.read_inventory)."""
    slides = carrier['slides']
    if not slides:
        return []
    chosen, seen = [slides[0]['number']], {slides[0]['layout']}
    for s in slides:
        if s['layout'] not in seen:
            seen.add(s['layout'])
            chosen.append(s['number'])
    if len(chosen) > MAX_SAMPLES:
        others = chosen[1:]
        chosen = [chosen[0]] + [others[round(k * (len(others) - 1) / 8)] for k in range(9)]
    rest = [s['number'] for s in slides if s['number'] not in chosen]
    while len(chosen) < min(6, len(slides)) and rest:
        step = len(rest) / (min(6, len(slides)) - len(chosen) + 1)
        pick = rest[int(step) - 1 if step >= 1 else 0]
        chosen.append(pick)
        rest.remove(pick)
    return sorted(chosen)


def _box_percent(box, w, h):
    x, y, bw, bh = box
    return [round(100 * x / w, 1), round(100 * y / h, 1), round(100 * (x + bw) / w, 1), round(100 * (y + bh) / h, 1)]


def _text(eid, frame, value, loaded, main):
    x, y, w, h = frame['box']
    return {'id': eid, 'type': 'text', 'text': value, 'x': round(x, 2), 'y': round(y, 2), 'w': round(max(w, 1), 2), 'h': round(max(h, 1), 2),
            'font': frame['font'] if frame['font'] in loaded else main, 'font_size': frame['font_size'], 'color': frame['color'],
            'bold': bool(frame.get('bold')), 'align': frame.get('align', 'left'), 'line_height': 1.18}


def replicas(style, numbers):
    """A document with one slide per chosen sample slide that has a composition, and what each picture does not show."""
    ref, w, h = style['reference'], style['width'], style['height']
    loaded = {f['family'] for f in style['fonts']}
    by_slide = {p['source_slide']: p for p in ref['patterns'] if p.get('source_kind') == 'slide'}
    # A sample slide without a composition of its own (deck 30: only its layouts give compositions) is shown by the
    # composition of its layout.
    by_layout = {p['layout_part']: p for p in ref['patterns'] if p.get('source_kind') == 'layout'}
    layout_of = {s['number']: s['layout'] for s in style['carrier']['slides']}
    slides, not_drawn = [], {}
    for n in numbers:
        p = by_slide.get(n) or by_layout.get(layout_of.get(n))
        if p is None:
            continue
        elements, missing = [], []
        for k, d in enumerate(p['decorations']):
            if d.get('kind') == 'backplate':
                missing.append({'what': 'vector picture over the whole slide under its text (EMF): the backplate of the slide, such as a frame, '
                                        'a pattern or a background picture', 'box': [0, 0, 100, 100]})
            elif d.get('kind') == 'ghost':
                missing.append({'what': 'vector picture of the slide (EMF), probably a logo' if d['box'][2] * d['box'][3] < w * h * .05
                                else 'vector picture of the slide (EMF)', 'box': _box_percent(d['box'], w, h)})
            elif d.get('kind') == 'shape':
                elements.append({'id': f'd{k}', 'type': 'shape', 'x': d['box'][0], 'y': d['box'][1], 'w': d['box'][2], 'h': d['box'][3],
                                 'geometry': d['geometry'], 'fill': d['fill'], 'opacity': d.get('opacity', 1)})
            else:
                asset = ref['assets'][d['asset']]
                elements.append({'id': f'd{k}', 'type': 'image', 'x': d['box'][0], 'y': d['box'][1], 'w': d['box'][2], 'h': d['box'][3],
                                 'mime': asset['mime'], 'data': asset['data'], 'crop': d['crop']})
        # Item 37, cause 4: an empty picture placeholder of the sample is the place of a photograph; PowerPoint shows it grey,
        # our drawing showed nothing (deck 23: the model missed the photo places on our pictures, found them on PowerPoint ones).
        for k, (x, y, bw, bh) in enumerate(p.get('picture_frames') or []):
            elements.append({'id': f'photo{k}', 'type': 'shape', 'x': x, 'y': y, 'w': bw, 'h': bh, 'geometry': 'rect', 'fill': 'D9D9D9', 'opacity': 1})
            missing.append({'what': 'empty picture placeholder: the place of a photograph (drawn grey)', 'box': _box_percent([x, y, bw, bh], w, h)})
        for u in p.get('unrendered', []):
            if u.get('reason', '').startswith('approximated-'):
                missing.append({'what': f"background drawn as its mean colour #{p['background']}; in the file it is a "
                                        f"{u['reason'][len('approximated-'):-len('-background')]}", 'box': [0, 0, 100, 100]})
            else:
                missing.append({'what': f"decor of the layout or master in a format the program cannot draw ({str(u.get('media', '')).rsplit('.', 1)[-1]})",
                                'box': _box_percent(u['box'], w, h)})
        # A text frame with a fill of its own (the blue title bar of Rosseti is the filled title text box) is drawn with it.
        for eid, frame in [('title', p['title'])] + [(f's{k}', slot) for k, slot in enumerate(p['slots'])]:
            if frame.get('fill'):
                x, y, fw, fh = frame['box']
                elements.append({'id': f'{eid}-fill', 'type': 'shape', 'x': x, 'y': y, 'w': fw, 'h': fh,
                                 'geometry': frame.get('geometry') if frame.get('geometry') in ('rect', 'roundRect') else 'rect',
                                 'fill': frame['fill'], 'opacity': 1})
            if (frame.get('source_text') or '').strip():
                elements.append(_text(eid, frame, frame['source_text'].strip(), loaded, style['font']))
        slides.append({'id': f'sample-{n}', 'number': n, 'background': p['background'], 'elements': elements})
        if missing:
            not_drawn[str(n)] = missing
    doc = {'schema': 'vsp.document/1', 'width': w, 'height': h, 'style': {'font': style['font'], 'fonts': style['fonts']},
           'slides': [{k: v for k, v in s.items() if k != 'number'} for s in slides]}
    return doc, [s['number'] for s in slides], not_drawn


def draw(doc, timeout=120):
    """PNG (bytes) of every slide of a document, drawn by renderer.js (render_worker.mjs)."""
    node, env = node_runtime()
    process = subprocess.run([node, str(ROOT / 'apps/generator/render_worker.mjs')], input=json.dumps({'documents': [doc]}, ensure_ascii=False),
                             capture_output=True, text=True, encoding='utf-8', env=env, timeout=timeout,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if process.returncode:
        raise ValueError('Отрисовка слайдов-образцов не выполнена: ' + process.stderr[-1500:])
    return [base64.b64decode(s['png']) for s in json.loads(process.stdout)['outputs'][0]['slides']]


def payload(style, name):
    """The request of scene_marker: the numbers and pictures of the chosen sample slides and what they do not show."""
    doc, numbers, not_drawn = replicas(style, select_samples(style['carrier']))
    if not numbers:
        return None, []
    pictures = draw(doc)
    w, h = style['width'], style['height']
    ratio = w / h
    aspect = '16:9' if abs(ratio - 16 / 9) < .02 else '4:3' if abs(ratio - 4 / 3) < .02 else f'{round(w)}:{round(h)}'
    body = {'template': name, 'aspect': aspect, 'slides': [{'slide': n, 'image': k + 1} for k, n in enumerate(numbers)],
            'not_drawn': not_drawn,
            'images': [{'mime': 'image/png', 'data': base64.b64encode(p).decode()} for p in pictures]}
    return body, pictures


# ---- checks of the answer ------------------------------------------------------------------------------------------------
def _hex(value):
    return value[1:].upper() if isinstance(value, str) and len(value) == 7 and value.startswith('#') else None


# The checks read pictures THUMB_WIDTH px wide: a picture of 1280 px takes 2.6 s to decode in pure Python, and the checks of
# a kept markup run at every generation, inside its 300 s (regression of 26.09.2026: 7 of 33 decks in 15 minutes). The
# thumbnails are made once, when the answer is kept; each is decoded once per process.
THUMB_WIDTH = 320


def thumb(picture):
    """A PNG at most THUMB_WIDTH px wide (every n-th pixel of every n-th row) of a picture."""
    width, height, rgba = png.decode(picture)
    step = max(1, -(-width // THUMB_WIDTH))
    if step == 1:
        return picture
    xs, ys = range(0, width, step), range(0, height, step)
    out = bytearray()
    for y in ys:
        row = rgba[y * width * 4:(y + 1) * width * 4]
        for x in xs:
            out += row[x * 4:x * 4 + 4]
    return png.encode(len(xs), len(ys), bytes(out))


@functools.lru_cache(maxsize=64)
def _pixels(picture):
    width, height, rgba = png.decode(picture)
    return width, height, rgba


def mean_colour(picture, box):
    """Mean colour (RRGGBB) of a box in percent of a picture, or None for an empty box."""
    width, height, rgba = _pixels(picture)
    x0, y0, x1, y1 = [max(0, min(1, v / 100)) for v in box]
    xs, ys = range(int(x0 * width), max(int(x0 * width) + 1, int(x1 * width))), range(int(y0 * height), max(int(y0 * height) + 1, int(y1 * height)))
    total, count = [0, 0, 0], 0
    for y in ys[::max(1, len(ys) // 40)]:
        for x in xs[::max(1, len(xs) // 40)]:
            i = (y * width + x) * 4
            total = [t + rgba[i + c] for c, t in enumerate(total)]
            count += 1
    return ''.join(f'{round(t / count):02X}' for t in total) if count else None


def distance(a, b):
    return sum((int(a[i:i + 2], 16) - int(b[i:i + 2], 16)) ** 2 for i in (0, 2, 4)) ** .5


def drawn(picture, box):
    """Share of a box in percent of a picture whose pixels differ from the main colour of the whole picture by more than 60
    (RGB distance): something is drawn there (a logo, a strip) on a background picture."""
    width, height, rgba = _pixels(picture)
    def colour(x, y):
        i = (y * width + x) * 4
        return f'{rgba[i]:02X}{rgba[i + 1]:02X}{rgba[i + 2]:02X}'
    grid = [colour(x, y) for y in range(0, height, max(1, height // 60)) for x in range(0, width, max(1, width // 60))]
    counts = {}
    for c in grid:
        q = ''.join(f'{int(c[i:i + 2], 16) // 16:X}' for i in (0, 2, 4))
        counts.setdefault(q, []).append(c)
    main = max(counts.values(), key=len)[0]
    x0, y0, x1, y1 = [max(0, min(1, v / 100)) for v in box]
    xs = range(int(x0 * width), max(int(x0 * width) + 1, int(x1 * width)))
    ys = range(int(y0 * height), max(int(y0 * height) + 1, int(y1 * height)))
    seen = [colour(x, y) for y in ys[::max(1, len(ys) // 30)] for x in xs[::max(1, len(xs) // 30)]]
    return sum(distance(c, main) > 60 for c in seen) / max(1, len(seen))


def accept(answer, style, numbers, pictures):
    """The fields of an answer that the file and the pixels confirm, and the rejected ones with the reason.

    title_band (content_style.title_band): present, with a colour and a box in the top half; on at least two pictures of content
    sample slides the mean colour of that box is within 40 of the colour (RGB distance), and the text colour of the title
    reads on it (3:1). no_text_zones of the content sample slides: a decor strip, header picture, photo area or logo whose box
    covers an object of the composition (decor, undrawn picture, picture placeholder) by at least 30 %% of the zone, or lies on
    a background picture of the composition where at least 10 %% of its pixels differ from the main colour of the picture."""
    accepted, rejected = {}, []
    index = {n: k for k, n in enumerate(numbers)}
    roles = {s['slide']: s['role'] for s in answer.get('slides', [])}
    content = [n for n in numbers if roles.get(n) == 'content']
    cs = answer.get('content_style') or {}
    band = cs.get('title_band') or {}
    colour, box = _hex(band.get('colour')), band.get('box') or []
    if band.get('present'):
        reason = None
        if not colour or len(box) != 4 or box[1] > 50:
            reason = 'no colour or box in the top half'
        else:
            seen = [mean_colour(pictures[index[n]], box) for n in content]
            near = [c for c in seen if c and distance(c, colour) <= 40]
            ink = _hex(cs.get('title_text_colour'))
            if len(near) < 2:
                reason = f'the pixels confirm the colour on {len(near)} of {len(seen)} content samples'
            elif not ink or contrast(ink, colour) < 3:
                reason = 'the title colour does not read on the band'
        if reason:
            rejected.append({'field': 'title_band', 'reason': reason})
        else:
            w, h = style['width'], style['height']
            accepted['title_band'] = {'box': [box[0] * w / 100, box[1] * h / 100, (box[2] - box[0]) * w / 100, (box[3] - box[1]) * h / 100],
                                      'fill': colour, 'color': _hex(cs.get('title_text_colour')), 'caps': bool(cs.get('title_caps')),
                                      'samples': content}
    by_slide = {p['source_slide']: p for p in style['reference']['patterns'] if p.get('source_kind') == 'slide'}
    # A sample slide without a composition of its own was shown by the composition of its layout (replicas); its zones are
    # checked against that one and kept for it (holdout-next2, SGD: the logo of the layout beside the title).
    by_layout = {p['layout_part']: p for p in style['reference']['patterns'] if p.get('source_kind') == 'layout'}
    layout_of = {x['number']: x['layout'] for x in (style.get('carrier') or {}).get('slides', [])}
    zones = []
    w, h = style['width'], style['height']
    for s in answer.get('slides', []):
        p = by_slide.get(s['slide']) or by_layout.get(layout_of.get(s['slide']))
        if s['role'] != 'content' or p is None:
            continue
        objects = [d['box'] for d in p['decorations'] if d['box'][2] * d['box'][3] < w * h * .9] + list(p.get('picture_frames') or [])
        # Holdout-next2 (SGD): the logo is drawn in the background picture of the layout, no object of its own; a zone over
        # such a picture is confirmed by the pixels of the picture of that sample slide.
        backdrop = any(d.get('asset') and d['box'][2] * d['box'][3] >= w * h * .9 for d in p['decorations'])
        for z in s.get('no_text_zones', []):
            if z['what'] not in ('decor_strip', 'header_image', 'photo_area', 'logo') or len(z['box']) != 4:
                continue
            zb = [z['box'][0] * w / 100, z['box'][1] * h / 100, (z['box'][2] - z['box'][0]) * w / 100, (z['box'][3] - z['box'][1]) * h / 100]
            area = max(1, zb[2] * zb[3])
            covered = max((max(0, min(zb[0] + zb[2], o[0] + o[2]) - max(zb[0], o[0])) * max(0, min(zb[1] + zb[3], o[1] + o[3]) - max(zb[1], o[1]))
                           for o in objects), default=0)
            # A zone over a text frame of the sample is a place for text, not decor (BCNET: its coloured panels carry the title
            # and text placeholders; the model called them decor on PowerPoint and on our pictures alike).
            frames = [p['title']['box']] + [q['box'] for q in p['slots']]
            on_text = any(max(0, min(zb[0] + zb[2], f[0] + f[2]) - max(zb[0], f[0])) * max(0, min(zb[1] + zb[3], f[1] + f[3]) - max(zb[1], f[1]))
                          >= .3 * max(1, f[2] * f[3]) for f in frames)
            if on_text and z['what'] != 'logo':
                rejected.append({'field': 'no_text_zone', 'slide': s['slide'], 'what': z['what'], 'reason': 'a text frame of the sample lies there'})
            elif covered >= area * .3:
                zones.append({'slide': s['slide'], 'pattern': p.get('id'), 'what': z['what'], 'box': [round(v, 2) for v in zb]})
            elif backdrop and s['slide'] in index and drawn(pictures[index[s['slide']]], z['box']) >= .1:
                zones.append({'slide': s['slide'], 'pattern': p.get('id'), 'what': z['what'], 'box': [round(v, 2) for v in zb], 'evidence': 'background-pixels'})
            else:
                rejected.append({'field': 'no_text_zone', 'slide': s['slide'], 'what': z['what'], 'reason': 'no object of the file under it'})
    if zones:
        accepted['no_text_zones'] = zones
    return accepted, rejected


def repair(value, errors):
    """Schema errors only in fields the product does not use yet (the stand-in on 57 templates, 26.09.2026: 5 answers of
    57 failed on stale text over 160 signs or more than 12 of it, and on a brand element named in more than 80 signs): the
    lists are cut to their limits and the texts shortened. A missing object_ids (always empty in this version of the prompt;
    2 answers of 57 left it out of no-text zones) is added empty. Any other error leaves the answer rejected."""
    if not isinstance(value, dict) or not all(e.startswith(('$.stale_text', '$.brand_elements[')) and ('.what' in e or '.text' in e or 'stale_text:' in e)
                                              or e.endswith('.object_ids: обязательное поле отсутствует') for e in errors):
        return None
    fixed = json.loads(json.dumps(value))
    def ids(node):
        if isinstance(node, dict):
            if 'box' in node and 'object_ids' not in node and ('what' in node or 'present' in node):
                node['object_ids'] = []
            for v in node.values():
                ids(v)
        elif isinstance(node, list):
            for v in node:
                ids(v)
    ids(fixed)
    fixed['stale_text'] = [{**t, 'text': str(t.get('text', ''))[:160]} for t in (fixed.get('stale_text') or [])[:12] if isinstance(t, dict)]
    fixed['brand_elements'] = [{**b, 'what': str(b.get('what', ''))[:80]} if isinstance(b, dict) else b for b in (fixed.get('brand_elements') or [])]
    return fixed


def cache_key(style):
    """Identity of a markup: the template, the prompt and schema of scene_marker, and what its pictures show — the document of
    the replicas with the words of not_drawn, and renderer.js that draws it (not the bytes of the pictures: the anti-aliasing of
    another machine must not lose the answer of the same template). A change of the analysis of the sample that changes what
    the model would see asks it again; any other change of the code reads the kept answer. The checks (accept) are not part of
    it: the kept pictures let them run again on the kept answer."""
    request = llm.build_request('scene_marker', {})
    doc, numbers, not_drawn = replicas(style, select_samples(style['carrier']))
    return llm.sha256(llm.canonical({'template': style.get('source_sha256'), 'prompt': request['prompt_sha256'], 'schema': request['schema_sha256'],
                                     'renderer': llm.sha256((ROOT / 'apps/generator/renderer.js').read_bytes().decode('utf-8')),
                                     'replicas': llm.sha256(llm.canonical({'document': doc, 'slides': numbers, 'not_drawn': not_drawn}))}))


def mark(style, name, provider=None, record_dir=None, cache=None):
    """The preparation stage: scene markup of a template. Returns {'accepted', 'rejected', 'calls', 'fallback', 'key'}.

    A markup kept in cache/scene_marker/<key>.json (the answer, the sample slides and thumbnails of their pictures) is read back without
    drawing or calling anything and checked again: a repeated generation of the same template, and recorded answers (provider
    replay), take it. Otherwise only a live provider is asked; its answer is kept."""
    key = cache_key(style)
    with _GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    with lock:
        return _mark(style, name, provider, record_dir, cache, key)


def _mark(style, name, provider, record_dir, cache, key):
    report = {'key': key, 'accepted': {}, 'rejected': [], 'calls': [], 'fallback': None}
    path = Path(cache) / 'scene_marker' / f'{key}.json' if cache else None
    if path is not None and path.is_file():
        stored = json.loads(path.read_bytes().decode('utf-8'))
        pictures = [base64.b64decode(p) for p in stored['thumbs']] if 'thumbs' in stored else [thumb(base64.b64decode(p)) for p in stored['pictures']]
        report['accepted'], report['rejected'] = accept(stored['response'], style, stored['slides'], pictures)
        report['answer'] = stored['response']
        report['calls'].append({'agent': 'scene_marker', 'request_sha256': stored['request_sha256'], 'provider': 'cache', 'ok': True, 'milliseconds': 0})
        return report
    if not llm.is_live(provider):
        report['fallback'] = 'no markup: the template was not prepared and recorded answers have none'
        return report
    try:
        body, pictures = payload(style, name)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        report['fallback'] = f'pictures: {str(exc)[:300]}'
        return report
    if body is None:
        report['fallback'] = 'no sample slide with a composition'
        return report
    value, record = llm.call('scene_marker', body, provider, record_dir, repair=repair)
    report['calls'].append(record)
    if value is None:
        report['fallback'] = f"no answer: {record.get('reason')}"
        return report
    numbers = [x['slide'] for x in body['slides']]
    thumbs = [thumb(p) for p in pictures]
    report['accepted'], report['rejected'] = accept(value, style, numbers, thumbs)
    report['answer'] = value
    if path is not None:
        # The full pictures are in the record of the call (record_dir); the checks need only the thumbnails.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps({'key': key, 'request_sha256': record['request_sha256'], 'response': value, 'slides': numbers,
                                     'thumbs': [base64.b64encode(p).decode() for p in thumbs]}, ensure_ascii=False).encode('utf-8'))
    return report
