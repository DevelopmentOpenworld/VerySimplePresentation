"""C7: contextual audit (Appendix 1 of the case, content validation) by the agent context_auditor, one call per slide.

The questions concern the content, which is the same in the three variants (facts, titles and order do not change
between them, docs/VARIANT_AXIS.md), so the audit runs once per brief, not per variant. Version 1 answers from the text
of a slide and the quotes of the brief its facts come from. Question 6 (pictures and icons) is asked by image_auditor,
per variant (variants put different sample cards), on the pictures the product put next to the facts, sent as images.
A deterministic check keeps only complete answers (every question once, a reason for every "no"); an answer that fails
it, or no answer, gives no findings for that slide and is recorded as a fallback. Each "no" becomes a finding of the
registry audit/checks.json with the model's reason.
"""
from concurrent.futures import ThreadPoolExecutor

from . import llm

QUESTIONS = {1: "CONTEXT_TITLE_IS_CONCLUSION", 2: "CONTEXT_CONTENT_MATCHES_TITLE", 3: "CONTEXT_ONE_SENTENCE",
             4: "CONTEXT_FACTS_IN_SOURCES", 5: "CONTEXT_HAS_CONTENT", 7: "CONTEXT_NO_SERVICE_TEXT", 8: "CONTEXT_NO_TYPOS",
             9: "CONTEXT_ONE_LANGUAGE", 10: "CONTEXT_ROWS_SUPPORT_POINT", 11: "CONTEXT_SLIDES_CONNECTED"}
NOT_ASKED = []
PICTURE_MIN = 16      # px; smaller pictures are dots and dashes of the design


def payloads(brief):
    """(section id, input of context_auditor) for every content slide of a structured brief vsp.brief/1."""
    sections = brief["sections"]
    for n, s in enumerate(sections):
        table = s["kind"] == "table"
        items = [] if table else [{"label": f.get("label", ""), "text": f["text"]} for f in s["facts"]]
        rows = [s["columns"]] + [f["cells"] for f in s["facts"]] if table else None
        # A fact of a free brief keeps the quote of the brief it came from; a structured brief is its own source.
        sources = [{"item": i + 1, "quote": (f.get("origin") or {}).get("quote") or (" | ".join(f["cells"]) if table else f["text"])}
                   for i, f in enumerate(s["facts"])]
        yield s["id"], {"deck_title": brief["title"], "language": brief.get("language", "ru"),
                        "slide": {"number": n + 2, "title": s["title"], "items": items, "table": rows},
                        "sources": sources, "previous_title": sections[n - 1]["title"] if n else None,
                        "next_title": sections[n + 1]["title"] if n + 1 < len(sections) else None}


def check(value):
    """Violations of an answer: every question asked exactly once, a reason for every "no"."""
    violations = []
    asked = sorted(a["question"] for a in value["answers"])
    if asked != sorted(QUESTIONS):
        violations.append({"code": "QUESTIONS_DO_NOT_MATCH", "questions": asked})
    violations += [{"code": "NO_WITHOUT_REASON", "question": a["question"]} for a in value["answers"]
                   if a["answer"] == "no" and not a["reason"].strip()]
    return violations


def pictures(doc, slide):
    """Pictures the product put next to the facts of a slide: images cloned with sample cards or taken from a sample slide
    (clone, furniture), not logos and decor of a layout or master, not backgrounds, not the mandatory decor of the sample
    slides (session 16, M1: the logo of deck 24 on every slide asked image_auditor 33 times in the package run, 57.5 s, and
    gave 32 false findings "the picture does not belong to the slide")."""
    area = doc["width"] * doc["height"]
    # Owner decision 48: an ornament of the template (the same picture in every card of the sample) is not asked either.
    return [e for e in slide["elements"] if e["type"] == "image" and (e.get("binding") or {}).get("kind") in ("clone", "furniture")
            and not e.get("brand_item") and not e.get("ornament") and e["w"] * e["h"] < area * .25 and min(e["w"], e["h"]) >= PICTURE_MIN]


def image_payloads(doc):
    """(slide id, picture element ids, input of image_auditor) for every content slide of a document with pictures."""
    for slide in doc["slides"]:
        found = pictures(doc, slide)
        if slide["role"] == "cover" or not found:
            continue
        facts = [e for e in slide["elements"] if e["type"] == "text" and e.get("fact_ids")]
        def near(p):
            # G3: a pictogram chosen for a fact stands next to that fact, wherever the nearest text centre lies.
            chosen = next((e for e in facts if (p.get("icon") or {}).get("fact") in e["fact_ids"]), None)
            if chosen is not None:
                return chosen["text"]
            cx, cy = p["x"] + p["w"] / 2, p["y"] + p["h"] / 2
            best = min(facts, key=lambda e: abs(e["x"] + e["w"] / 2 - cx) + abs(e["y"] + e["h"] / 2 - cy), default=None)
            return best["text"] if best else None
        title = next((e["text"] for e in slide["elements"] if e["id"] == "title"), "")
        yield slide["id"], [p["id"] for p in found], {
            "deck_title": doc["brief"]["title"], "language": doc["brief"].get("language", "ru"),
            "slide": {"title": title, "items": [e["text"] for e in facts]},
            "pictures": [{"picture": n + 1, "next_to": near(p)} for n, p in enumerate(found)],
            "images": [{"mime": p["mime"], "data": p["data"]} for p in found]}


def check_images(value, count):
    """Violations of an image_auditor answer: one answer per picture, a reason for every "no"."""
    violations = []
    asked = sorted(a["picture"] for a in value["answers"])
    if asked != list(range(1, count + 1)):
        violations.append({"code": "QUESTIONS_DO_NOT_MATCH", "pictures": asked})
    violations += [{"code": "NO_WITHOUT_REASON", "picture": a["picture"]} for a in value["answers"] if a["answer"] == "no" and not a["reason"].strip()]
    return violations


def review_images(documents, provider=None, record_dir=None, root=llm.ROOT, workers=6):
    """C7, question 6: the pictures of every slide of every variant (variants put different sample cards, so pictures
    differ); the same slide and pictures are asked once. Findings by variant, with the picture as the element."""
    report = {"schema": "vsp.image-audit/1", "calls": [], "fallbacks": [], "findings": {d["variant"]: [] for d in documents}}
    jobs, keys = {}, []
    for doc in documents:
        for sid, ids, payload in image_payloads(doc):
            key = llm.canonical(payload)
            jobs.setdefault(key, payload)
            keys.append((doc["variant"], sid, ids, key))

    def one(item):
        key, payload = item
        value, record = llm.call("image_auditor", payload, provider, record_dir, root)
        problems = [{"code": "NO_ANSWER", "reason": record.get("reason")}] if value is None else check_images(value, len(payload["pictures"]))
        if value is not None:
            record["violations"] = problems
        return key, (None if problems else value), record, problems

    with ThreadPoolExecutor(max_workers=workers) as pool:
        answers = {key: (value, record, problems) for key, value, record, problems in pool.map(one, list(jobs.items()))}
    for key, (value, record, problems) in answers.items():
        report["calls"].append(record)
        if value is None:
            report["fallbacks"].append({"agent": "image_auditor", "problems": problems})
    for variant, sid, ids, key in keys:
        value = answers[key][0]
        for a in (value or {"answers": []})["answers"]:
            if a["answer"] == "no":
                report["findings"][variant].append({"code": "CONTEXT_IMAGES_RELEVANT", "slide": sid, "element": ids[a["picture"] - 1], "question": 6, "reason": a["reason"]})
    return report


# ---- C6, model fixer rewrite-title ---------------------------------------------------------------------------------------
TITLE_FIXER = "rewrite-title"


def title_payload(brief, sid, title, findings):
    """Input of title_writer: the slide (its current title and the texts of its facts) and every finding of the contextual
    audit on its title, in the order of the questions, so the request does not depend on which of them the user chose."""
    section = next(s for s in brief["sections"] if s["id"] == sid)
    items = [f["text"] for f in section["facts"]] if section["kind"] == "text" else [" | ".join(f["cells"]) for f in section["facts"]]
    ordered = sorted(findings, key=lambda f: f["question"])
    return {"deck_title": brief["title"], "language": brief.get("language", "ru"), "slide": {"title": title, "items": items},
            "finding": {"question": "; ".join(f["title"] for f in ordered), "reason": " ".join(f["reason"] for f in ordered)}}


def check_title(value, old, items):
    """Violations of a title_writer answer: another title, numbers only from the facts of the slide (length: the schema)."""
    from .planner import numbers
    title = value["title"].strip()
    violations = []
    if not title or title == old.strip():
        violations.append({"code": "TITLE_UNCHANGED"})
    extra = sorted(set(numbers(title)) - set(numbers(" ".join(items))))
    if extra:
        violations.append({"code": "TITLE_NUMBER_NOT_IN_FACTS", "numbers": extra})
    return violations


def rewrite_title(payload, provider=None, record_dir=None, root=llm.ROOT):
    """A new title of a slide by title_writer, checked; (title or None, record of the call that decided, problems).

    Rehearsals of the video on template E and the check of the view of the work (29.09.2026, Qwen3.8-27B): in 2 fixes of 8 the
    answer repeated the title, and the fix the user chose was refused. A live model whose answer broke a check is asked
    once more and told what it broke, as the writers of the slides are (planner.explain)."""
    from .planner import explain, numbers
    old, items = payload["slide"]["title"], payload["slide"]["items"]

    def ask(body):
        value, record = llm.call("title_writer", body, provider, record_dir, root)
        problems = [{"code": "NO_ANSWER", "reason": record.get("reason")}] if value is None else check_title(value, old, items)
        if value is not None:
            record["violations"] = problems
        return value, record, problems

    value, record, problems = ask(payload)
    if problems and value is not None and llm.is_live(provider, root):
        told = [{**p, "title": old, "allowed": sorted(set(numbers(" ".join(items))))} for p in problems]
        first = record
        value, record, problems = ask({**payload, "previous_answer_problems": explain(told)})
        record["retry_of"] = first["request_sha256"]
    return (None if problems else value["title"].strip()), record, problems


def title_findings(findings):
    """Findings of the contextual audit whose fixer is rewrite-title, by slide."""
    from .audit import checks
    by_slide = {}
    for f in findings:
        if checks().get(f["code"], {}).get("fixer") == TITLE_FIXER:
            by_slide.setdefault(f["slide"], []).append(f)
    return by_slide


def review(brief, provider=None, record_dir=None, root=llm.ROOT, workers=6):
    """The contextual audit of a structured brief: calls, fallbacks and findings for run.json and the revisions."""
    report = {"schema": "vsp.context-audit/1", "calls": [], "fallbacks": [], "findings": [], "not_asked": NOT_ASKED}

    def one(item):
        sid, payload = item
        value, record = llm.call("context_auditor", payload, provider, record_dir, root)
        problems = [{"code": "NO_ANSWER", "reason": record.get("reason")}] if value is None else check(value)
        if value is not None:
            record["violations"] = problems
        return sid, (None if problems else value), record, problems

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, list(payloads(brief))))
    for sid, value, record, problems in results:
        report["calls"].append(record)
        if value is None:
            report["fallbacks"].append({"agent": "context_auditor", "slide": sid, "problems": problems})
            continue
        for a in sorted(value["answers"], key=lambda a: a["question"]):
            if a["answer"] == "no":
                report["findings"].append({"code": QUESTIONS[a["question"]], "slide": sid, "question": a["question"], "reason": a["reason"]})
    return report
