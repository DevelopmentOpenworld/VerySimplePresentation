"""Text of every slide: speaker notes of the deck (stage of 24.09.2026, owner decision 21, answer 1, variant a).

The organizers (docs/PROJECT_DECISIONS.md, item 17) put into the five minutes of generation both the slides and the text a
speaker says on each of them. One call of the agent speaker_notes per brief writes the notes of the cover and of every
section: the three variants share facts, titles and order, so they share the notes too. The words fit the length of the
talk (talk_minutes of the brief, 7 minutes by default, as the pitch of the task and the example of the organizers).

A deterministic check guards what a model must not do here: bring numbers that are not in the facts of its slide, or talk
longer than the time allows. A slide whose text fails the check, or a missing answer, takes a fallback made of the facts of
the slide themselves, so the pipeline never depends on a model; every call and fallback is recorded for run.json.
Variant b (item 34 of the plan, 25.09.2026): with a content package the agent speaker_notes_sources also reads the paragraphs
of the materials around the quotes of the facts of each slide (materials.slide_sources); the check of numbers stays that of
the facts.
"""
import math

from . import llm
from .planner import HEADING, ITEM, SENTENCE, explain, numbers, _flat

WORDS_PER_MINUTE = 120
DEFAULT_MINUTES = 7
# A slide may take more than its even share of the words when it has more to say, not the whole talk.
SLIDE_SHARE = 2.0
TOTAL_TOLERANCE = 1.15


def fact_text(fact, section):
    """What a fact says in words: the whole fact a free brief was analysed into (the slide shows a shortened item of it),
    its text, or the cells of a table row with their column names."""
    if (fact.get("origin") or {}).get("analysis"):
        return fact["origin"]["analysis"]
    if "text" in fact:
        return fact["text"]
    return "; ".join(f"{c}: {v}" for c, v in zip(section.get("columns", []), fact.get("cells", [])))


def slides_of(brief):
    """The slides the notes are written for, in deck order: the cover, then every section."""
    cover = {"id": "cover", "title": brief["title"], "facts": []}
    return [cover] + [{"id": s["id"], "title": s["title"], "facts": [{"id": f["id"], "text": fact_text(f, s)} for f in s["facts"]]}
                      for s in brief["sections"]]


def budget(minutes, count):
    """Words of the whole talk and the limit of one slide."""
    total = int(minutes * WORDS_PER_MINUTE)
    return total, max(40, math.ceil(SLIDE_SHARE * total / max(1, count)))


def words(text):
    return len(_flat(text).split())


def allowed_numbers(slide, brief):
    source = [slide["title"]] + [f["text"] for f in slide["facts"]]
    if slide["id"] == "cover":
        source += [brief.get("subtitle", "")]
    return {x for text in source for x in numbers(text)}


def check_notes(value, slides, brief, total, per_slide):
    """(violations of the whole answer, {slide id: violations of its text})."""
    ids = [s["id"] for s in slides]
    got = [item["id"] for item in value["slides"]]
    whole, by_slide = [], {}
    if got != ids:
        whole.append({"code": "NOTES_SLIDES_DO_NOT_MATCH", "expected": ids, "got": got})
    by_id = {s["id"]: s for s in slides}
    for item in value["slides"]:
        slide = by_id.get(item["id"])
        if slide is None:
            continue
        problems = []
        extra = set(numbers(item["text"])) - allowed_numbers(slide, brief)
        if extra:
            problems.append({"code": "NOTES_NUMBER_NOT_IN_FACTS", "numbers": sorted(extra)})
        if words(item["text"]) > per_slide:
            problems.append({"code": "NOTES_SLIDE_TOO_LONG", "words": words(item["text"]), "limit": per_slide})
        if problems:
            by_slide[item["id"]] = problems
    spoken = sum(words(item["text"]) for item in value["slides"])
    if spoken > total * TOTAL_TOLERANCE:
        whole.append({"code": "NOTES_OVER_TIME", "words": spoken, "limit": total})
    return whole, by_slide


def source_sentences(paragraphs):
    """Sentences of the paragraphs of a content package a speaker can say: headings, code and table rows are structure,
    a list mark is dropped (as planner.fallback_analysis does)."""
    for paragraph in paragraphs:
        line = _flat(paragraph)
        if HEADING.match(line) or line.startswith("```") or " | " in line:
            continue
        item = ITEM.match(line)
        yield from (s.strip() for s in SENTENCE.split(line[item.end():] if item else line) if s.strip())


def _key(text):
    return _flat(text).casefold().rstrip(" .;:")


def fallback_text(slide, brief, per_slide, sources=(), share=None, said=None):
    """The facts of the slide as spoken sentences, cut to the limit of a slide; the cover says what the talk is about.
    With a content package (variant b) the sentences of the paragraphs around the facts follow, while the text stays within
    the even share of the talk: only a sentence without a number other than the numbers of the slide, and not one already
    said — `said` holds the facts of every slide (a paragraph after a quote may be the fact of the next slide) and the
    sentences taken so far, and grows."""
    if slide["id"] == "cover":
        parts = [brief["title"], brief.get("subtitle", "")]
    else:
        parts = [f["text"] for f in slide["facts"]]
    sentences = []
    for part in parts:
        part = _flat(part).rstrip(" .;:")
        if part:
            sentences.append(part + ".")
    text = " ".join(sentences)
    if words(text) > per_slide:
        return " ".join(text.split()[:per_slide]).rstrip(" ,;:") + "…"
    said = said if said is not None else set()
    allowed = allowed_numbers(slide, brief)
    for sentence in source_sentences(sources):
        key = _key(sentence)
        # Said already: the sentence is a piece of a fact or of a sentence taken, or holds one (without a model the facts are
        # the sentences of the materials and their pieces; ui-3 of the journal: a sentence holding its own fact repeated it).
        if (any(key in s or (len(s) >= 12 and s in key) for s in said) or set(numbers(sentence)) - allowed
                or words(text) + words(sentence) > min(per_slide, share or per_slide)):
            continue
        text += " " + sentence
        said.add(key)
    return text


def write_notes(brief, minutes=None, purpose="", provider=None, record_dir=None, root=llm.ROOT, sources=None):
    """vsp.speaker-notes/1 of a structured brief, and the record of every call, check and fallback for run.json.
    minutes and purpose come from the brief the user gave (a free brief keeps them, the structured one it becomes does not).
    sources (variant b, item 34 of the plan): {section id: paragraphs of the content package around the quotes of its facts};
    with them the agent speaker_notes_sources tells more than the facts; without them the request is that of variant a."""
    minutes = minutes or DEFAULT_MINUTES
    slides = slides_of(brief)
    total, per_slide = budget(minutes, len(slides))
    language = brief.get("language") or "ru"
    report = {"schema": "vsp.notes-report/1", "calls": [], "fallbacks": []}
    agent = "speaker_notes"
    request_slides = slides
    if sources:
        agent = "speaker_notes_sources"
        request_slides = [{**s, "sources": sources[s["id"]]} if sources.get(s["id"]) else s for s in slides]
    payload = {"deck_title": brief["title"], "subtitle": brief.get("subtitle", ""), "audience": brief.get("audience", ""),
               "purpose": purpose or brief.get("purpose", ""), "minutes": minutes, "words_total": total, "words_per_slide_max": per_slide,
               "slides": request_slides, "language": language}
    report["agent"] = agent
    value, record = llm.call(agent, payload, provider, record_dir, root)
    report["calls"].append(record)
    whole, by_slide = ([{"code": "NO_ANSWER", "reason": record.get("reason")}], {}) if value is None else check_notes(value, slides, brief, total, per_slide)
    if value is not None:
        record["violations"] = whole + [dict(v, slide=sid) for sid, problems in by_slide.items() for v in problems]
    if (whole or by_slide) and llm.is_live(provider, root):
        # As the planner does for a live model (proposals of 24.09.2026): one more call with what the answer broke; the
        # repeat replaces the first answer only when it breaks less.
        problems = record["violations"] if value is not None else whole
        # Twelfth session: the repeat says in plain words what the answer broke (planner.explain).
        again, retry = llm.call(agent, {**payload, "previous_answer_problems": explain(problems)}, provider, record_dir, root)
        retry["retry_of"] = record["request_sha256"]
        report["calls"].append(retry)
        if again is not None:
            whole2, by_slide2 = check_notes(again, slides, brief, total, per_slide)
            retry["violations"] = whole2 + [dict(v, slide=sid) for sid, problems in by_slide2.items() for v in problems]
            if value is None or len(retry["violations"]) < len(record["violations"]):
                value, whole, by_slide = again, whole2, by_slide2
    answered = {item["id"]: item["text"] for item in value["slides"]} if value is not None else {}
    notes = {}
    said = {_key(f["text"]) for s in slides for f in s["facts"]}
    for slide in slides:
        text = answered.get(slide["id"])
        if text is not None and slide["id"] not in by_slide:
            notes[slide["id"]] = {"text": _flat(text), "source": "agent"}
        else:
            notes[slide["id"]] = {"text": fallback_text(slide, brief, per_slide, (sources or {}).get(slide["id"], ()), total // len(slides), said),
                                  "source": "fallback"}
            report["fallbacks"].append({"agent": agent, "slide": slide["id"],
                                        "problems": by_slide.get(slide["id"]) or whole or [{"code": "NOTES_MISSING"}]})
    if value is not None and whole and not by_slide and all(n["source"] == "agent" for n in notes.values()):
        # Only the whole talk runs over time: the texts stay, the report says so.
        report["warnings"] = whole
    spoken = sum(words(n["text"]) for n in notes.values())
    result = {"schema": "vsp.speaker-notes/1", "minutes": minutes, "words_per_minute": WORDS_PER_MINUTE, "words_total": total,
              "words_per_slide_max": per_slide, "words": spoken, "language": language, "slides": notes}
    report["agent_slides"] = sum(n["source"] == "agent" for n in notes.values())
    report["fallback_slides"] = len(notes) - report["agent_slides"]
    report["words"] = spoken
    return result, report
