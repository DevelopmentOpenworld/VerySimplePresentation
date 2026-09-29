"""Places of titles on the scene of the template (session 16, 26.09.2026; M7 of holdout-next3, research
analysis/style-experiments/20260926-hn3-m7/process/research-titleband.md). Applied to the documents of the composer before the
check of the rendering.

B1 (Оргздрав): the dark band of the title is drawn in the pixels of the background picture of the master, and the sample
slides set their titles as white text boxes inside it; the Office title placeholder of the other layouts crossed the lower
edge of the band, black, and the check moved it under the band. A content title whose frame crosses the lower edge of the
title band of the scene markup (scenes.accept), when the sample titles lie inside that band, takes their place, colour, weight
and size (the most common of their frames), where no content of the slide lies.

B3 (deck 20, slide 6): a sample slide with a white panel over the left half and two titles; the frame of the first title spans
the whole slide, its colour was chosen by the dark background of the frame, and the letters stood on the white panel. On a slide
that draws such a panel (a filled shape at the left or right edge, at least 60 % of the height, at most 70 % of the width, 3:1
against the slide), a title frame across the inner edge of the panel ends 12 px before it when the text of the sample title
fits one line there, in the colour of the sample title when that reads 4.5:1 on the panel.
"""
from collections import Counter
import math

from . import composer
from .patterns import contains, contrast, overlap
from .placement import estimate_lines


def _title(slide):
    return next((e for e in slide['elements'] if e['id'] == 'title' and e['type'] == 'text'), None)


def _resize(e, box, own_px=None):
    typography = (e.get('binding') or {}).get('typography')
    if (e.get('binding') or {}).get('kind') == 'placeholder' and typography:
        size_pt = own_px * .75 if own_px else typography.get('size_pt')
        return composer.placeholder_text_size(e['text'], box, {**typography, 'size_pt': size_pt}, 12) or 12
    return composer.text_size(e['text'], box, own_px or e.get('size_own') or e['font_size'], e.get('line_height', 1.18))


def band_place(style, band):
    """B1: the place of the sample titles in the title band of the markup — the most common frame of the titles of its sample
    slides that lie inside it, when at least half of them (and two) do; None otherwise."""
    box = band['box']
    seen = [p for p in style['reference']['patterns'] if p.get('source_kind') == 'slide' and p['source_slide'] in band.get('samples', [])]
    inside = [p['title'] for p in seen if contains(box, p['title']['box'], 4)]
    if not seen or len(inside) < max(2, math.ceil(len(seen) / 2)):
        return None
    key = Counter(tuple(round(v / 8) for v in t['box']) for t in inside).most_common(1)[0][0]
    frames = [t for t in inside if tuple(round(v / 8) for v in t['box']) == key]
    first = frames[0]
    sizes = sorted(f['font_size'] for f in frames)
    return {'box': list(first['box']), 'color': first['color'] if contrast(first['color'], band['fill']) >= 3 else band['color'],
            'font_size': sizes[len(sizes) // 2], 'bold': sum(bool(f.get('bold')) for f in frames) * 2 >= len(frames)}


def into_band(slide, band, place):
    """B1 on one content slide."""
    e = _title(slide)
    if slide['role'] == 'cover' or e is None:
        return
    b = band['box']
    bottom = b[1] + b[3]
    if not (e['y'] < bottom - 4 and e['y'] + e['h'] > bottom + 4):
        return
    pb = place['box']
    box = [pb[0], pb[1], pb[2], max(pb[3], bottom - 4 - pb[1])]
    others = [x for x in slide['elements'] if x is not e and not x.get('template_decoration') and not x.get('ghost') and x['id'] not in ('disclosure', 'page')]
    if any(overlap([x['x'], x['y'], x['w'], x['h']], box) > 0 for x in others):
        return
    before = {k: e[k] for k in ('x', 'y', 'w', 'h', 'font_size', 'color', 'bold')}
    size = _resize(e, box, place['font_size'])
    e.update({'x': round(box[0], 2), 'y': round(box[1], 2), 'w': round(box[2], 2), 'h': round(box[3], 2), 'font_size': size,
              'color': place['color'], 'bold': place['bold']})
    e['style_adjustments'] = [a for a in e.get('style_adjustments', []) if a.get('reason') != 'solid-background-contrast'] + [
        {'reason': 'title-into-band', 'from': before, 'band': [round(v, 1) for v in b], 'own_px': place['font_size']}]


def on_panel(doc, slide, pattern):
    """B3 on one content slide."""
    e = _title(slide)
    if slide['role'] == 'cover' or e is None or pattern is None:
        return
    width, height = doc['width'], doc['height']
    panels = [x for x in slide['elements'] if x['type'] == 'shape' and not x.get('ghost') and (x.get('opacity', 1) or 0) >= .9 and x.get('fill')
              and x['h'] >= height * .6 and x['w'] <= width * .7 and (x['x'] <= 2 or x['x'] + x['w'] >= width - 2)
              and contrast(x['fill'], slide['background']) >= 3]
    sample = pattern['title']
    for panel in panels:
        left = panel['x'] <= 2
        edge = panel['x'] + panel['w'] if left else panel['x']
        if not (e['x'] < edge < e['x'] + e['w']) or left != (e.get('align', 'left') == 'left'):
            continue
        box = [e['x'], e['y'], edge - 12 - e['x'], e['h']] if left else [edge + 12, e['y'], e['x'] + e['w'] - edge - 12, e['h']]
        inset = e.get('inset') or [0, 0, 0, 0]
        own = (sample.get('source_text') or '').strip()
        if not own or box[2] - inset[0] - inset[2] <= 0 or estimate_lines(own, box[2] - inset[0] - inset[2], sample['font_size']) > 1:
            continue
        colour = next((c for c in (sample['color'], e['color']) if contrast(c, panel['fill']) >= 4.5),
                      max(('FFFFFF', '151515'), key=lambda c: contrast(c, panel['fill'])))
        before = {k: e[k] for k in ('x', 'y', 'w', 'h', 'font_size', 'color')}
        e.update({'x': round(box[0], 2), 'y': round(box[1], 2), 'w': round(box[2], 2), 'h': round(box[3], 2), 'font_size': _resize(e, box), 'color': colour})
        e['style_adjustments'] = [a for a in e.get('style_adjustments', []) if a.get('reason') not in ('observed-title-colour', 'solid-background-contrast')] + [
            {'reason': 'title-on-panel', 'from': before, 'panel': [round(panel[k]) for k in ('x', 'y', 'w', 'h')]}]
        return


def place_titles(documents, style):
    patterns = {p['id']: p for p in (style.get('reference') or {}).get('patterns', [])}
    band = ((style.get('scene_markup') or {}).get('accepted') or {}).get('title_band')
    place = band_place(style, band) if band else None
    for doc in documents:
        for slide in doc['slides']:
            on_panel(doc, slide, patterns.get((slide.get('reference') or {}).get('pattern')))
            if place:
                into_band(slide, band, place)
    return documents
