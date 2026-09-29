"""Titles fitted to the typography of the template (agent title_fitter; twelfth session, 25.09.2026).

The planner writes titles before the template is read (up to 75 signs; 65 in the median of the regressions). A template with
short large titles (deck 07: 44 pt, about 20 signs; deck 02, deck 10) then sets them far below its own size: on 17 of
the 33 development templates the median title of a content slide came out below three quarters of the size of the sample
(analysis/style-experiments/20260925-holdout-fixes, probe_title_scale.py). The typographic scale is part of the style.

After a first composition and its check of the rendering the frame and the size of every title are known. A title set below
TITLE_SHRUNK of its own size in some variant gets a budget: the signs its frame holds at that size (the tightest variant). One call of title_fitter
rewrites those titles within their budgets; the check is that of the planner — within the budget, numbers only from the
facts of the slide — and a title that fails keeps its text. Without a model (replay without records) every title stays, so
the recorded regressions do not change. Only titles the planner wrote are fitted: a structured brief keeps the titles its
author gave.
"""
import math

from . import llm
from .planner import explain, numbers, _flat
from .placement import estimate_lines

TITLE_SHRUNK = .85     # a title set below this share of its own size is too long for the template
CHAR_WIDTH = .56       # of the size, as placement.estimate_lines counts a character
# Item 37, cause 9 (holdout-next 25.09.2026, deck 27): a title the placeholder sets in capitals (cap="all"); capitals are
# wider, and the budget of .56 let six fitted titles of deck 27 leave their frames in PowerPoint.
CAPS_CHAR_WIDTH = .67
BUDGET_MIN = 24        # a budget below this holds no conclusion; such a frame stays as it is
# Owner decision 49 (29.09.2026), limitation 1: a title at its own size in a narrow title column of the template is as long for
# its frame as a shrunk one (template A sample 25: 358 px at 32 px, four lines on slide 3 of the issued split variant of K3-9;
# titles in four lines and more on the 19 rebuilt inputs: 5 of 591, all at their own size). More than TITLE_MAX_LINES lines
# (estimated) at its size give a title the budget of its frame, which budget() counts in at most three lines.
TITLE_MAX_LINES = 3
# The model does not count signs exactly (Luna on deck 10, 25.09: 31-33 signs for a budget of 29, twice), and the budget is
# conservative (a sign as .56 of the size); a title up to this share over the budget is taken, the check of the rendering
# decides its size. The request still asks for the budget.
ACCEPT_SLACK = .15


def own_size(e):
    """Size in px the template gives a title: of its layout placeholder, or of the sample frame it was taken from."""
    b = e.get('binding') or {}
    if b.get('kind') == 'placeholder' and (b.get('typography') or {}).get('size_pt'):
        return b['typography']['size_pt'] * 4 / 3
    return e.get('size_own') or e['font_size']


def budget(e):
    """Signs a title frame holds at the own size of the title: whole lines of the frame without its insets, a word wrap
    losing up to two signs a line."""
    own = own_size(e)
    left, top, right, bottom = e.get('inset') or (0, 0, 0, 0)
    width, height = max(1.0, e['w'] - left - right), max(1.0, e['h'] - top - bottom)
    per_line = math.floor(width / (own * (CAPS_CHAR_WIDTH if e.get('caps') == 'all' else CHAR_WIDTH)))
    lines = max(1, min(3, math.floor(height / (own * (e.get('line_height') or 1.2)))))
    return per_line * lines - 2 * (lines - 1) - 1


def budgets(documents):
    """{section id: signs} for the titles of content slides set below TITLE_SHRUNK of their own size, or in more than
    TITLE_MAX_LINES lines, in some variant of the documents after the check of the rendering (their sizes are final there)."""
    found = {}
    for doc in documents:
        # Owner decision 46 (28.09.2026), step 3: a title whose glyphs still meet the text under it after the check of the
        # rendering (TEXT_COLLISION; neither a move nor a smaller size of the scale cleared it) gets the budget of the room
        # between the top of its frame and that text.
        collided = {x['slide']: x.get('room') for x in (doc.get('render_quality') or {}).get('after', [])
                    if x.get('code') == 'TEXT_COLLISION' and x.get('element') == 'title'}
        for s in doc['slides']:
            if s['role'] == 'cover':
                continue
            e = next((x for x in s['elements'] if x['id'] == 'title' and x['type'] == 'text'), None)
            if e is None or '\n' in e['text']:
                continue
            if s['id'] in collided:
                room = collided[s['id']]
                n = budget({**e, 'h': min(e['h'], room)}) if room and room > 0 else 0
            elif e['font_size'] < own_size(e) * TITLE_SHRUNK:
                n = budget(e)
            elif estimate_lines(e['text'], max(1.0, e['w'] - sum((e.get('inset') or (0, 0, 0, 0))[0::2])), e['font_size']) > TITLE_MAX_LINES:
                n = budget(e)
            else:
                continue
            found[s['id']] = min(found.get(s['id'], 10 ** 6), n)
    return {sid: n for sid, n in found.items() if n >= BUDGET_MIN}


def check_titles(value, asked):
    """(accepted {section id: title}, violations) of an answer; asked: {slide number: (section, budget, allowed numbers)}."""
    accepted, violations, seen = {}, [], set()
    for item in value['titles']:
        n, title = item['number'], _flat(item['title']).strip('«»"').rstrip('.')
        if n not in asked or n in seen:
            violations.append({'code': 'TITLE_NOT_ASKED', 'slide_number': n})
            continue
        seen.add(n)
        section, limit, allowed = asked[n]
        extra = set(numbers(title)) - allowed
        if extra:
            violations.append({'code': 'TITLE_NUMBER_NOT_IN_FACTS', 'slide_number': n, 'title': title, 'numbers': sorted(extra), 'allowed': sorted(allowed)})
        elif len(title) > math.floor(limit * (1 + ACCEPT_SLACK)):
            violations.append({'code': 'TITLE_TOO_LONG', 'slide_number': n, 'title': title, 'length': len(title), 'limit': limit})
        else:
            accepted[section['id']] = title
    return accepted, violations


def fit_titles(brief, documents, provider=None, record_dir=None, root=llm.ROOT):
    """(brief with the fitted titles, report for run.json). The brief given is not changed."""
    limits = budgets(documents)
    report = {'schema': 'vsp.title-fit/1', 'calls': [], 'shrunk': len(limits), 'asked': 0, 'fitted': [], 'fallbacks': []}
    asked = {}
    for n, section in enumerate(brief['sections'], 2):
        limit = limits.get(section['id'])
        if limit is None or len(section['title']) <= limit:
            continue
        allowed = {x for f in section['facts'] for text in (f.get('text', ''), (f.get('origin') or {}).get('analysis', '')) for x in numbers(text)}
        asked[n] = (section, limit, allowed)
    report['asked'] = len(asked)
    if not asked:
        return brief, report
    payload = {'deck_title': brief['title'], 'language': brief.get('language') or 'ru',
               'slides': [{'number': n, 'title': s['title'], 'max_chars': limit, 'facts': [f.get('text', '') for f in s['facts']]}
                          for n, (s, limit, _) in sorted(asked.items())]}
    value, record = llm.call('title_fitter', payload, provider, record_dir, root)
    report['calls'].append(record)
    accepted, violations = ({}, [{'code': 'NO_ANSWER', 'reason': record.get('reason')}]) if value is None else check_titles(value, asked)
    if value is not None:
        record['violations'] = violations
    if violations and value is not None and llm.is_live(provider, root):
        # One repeat with what the answer broke, in plain words (planner.explain); it may add titles, not take any back.
        again, retry = llm.call('title_fitter', {**payload, 'previous_answer_problems': explain(violations)}, provider, record_dir, root)
        retry['retry_of'] = record['request_sha256']
        report['calls'].append(retry)
        if again is not None:
            more, problems = check_titles(again, asked)
            retry['violations'] = problems
            accepted = {**more, **accepted}
            violations = [v for v in problems if v.get('slide_number') not in {n for n, (s, _, _) in asked.items() if s['id'] in accepted}]
    if violations:
        report['fallbacks'].append({'agent': 'title_fitter', 'problems': violations})
    report['fitted'] = [{'section': s['id'], 'before': s['title'], 'after': accepted[s['id']], 'max_chars': limit}
                        for n, (s, limit, _) in sorted(asked.items()) if s['id'] in accepted]
    if not accepted:
        return brief, report
    return {**brief, 'sections': [{**s, 'title': accepted[s['id']]} if s['id'] in accepted else s for s in brief['sections']]}, report
