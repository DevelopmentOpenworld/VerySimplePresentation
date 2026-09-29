"""Free brief -> structured brief vsp.brief/1 through three agents (stage B, docs/LLM_LAYER_2026-09-21.md, section 4).

brief_analyst extracts facts, outline_planner groups them into slides, slide_writer shortens every fact to a card item
with a label. After every agent a deterministic check runs; an answer that fails it, or no answer at all, is replaced by
a deterministic fallback, so the pipeline never depends on a model being available. The checks guard the one thing a
model must not do here: bring numbers that are not in the brief.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import math
import re

from . import llm
from .charts import check_chart, check_metrics, check_process

NUMBER = re.compile(r"\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")
SPACES = re.compile(r"\s+")
SENTENCE = re.compile(r"(?<=[.!?…])\s+(?=[«\"A-ZА-ЯЁ0-9])")
# Instructions addressed to the model inside the brief are data, not facts (B6); the fallback leaves them out too.
INSTRUCTION = re.compile(r"(игнорируй|игнорировать|ignore (all|previous|the)|предыдущ\w* (правил|инструкц)|добавь факт|ответь не|system prompt)", re.I)
HEADING = re.compile(r"#{1,6}\s")
ITEM = re.compile(r"(?:[-*•+]|\d{1,2}[.)])\s+")
FACT_MAX = 180
TITLE_MAX = 75
DEFAULT_DISCLOSURE = "Черновик по брифу пользователя; проверьте факты перед показом."
# Owner decision 47: the source of the statements content_writer adds to a short request.
MODEL_SOURCE = "Дописано сервисом по теме запроса: общие сведения без чисел, которых нет в запросе; проверьте перед показом"


def numbers(text):
    """Numbers of a text, normalized: thousands separators removed, decimal comma as a point."""
    return [re.sub(r"[   ]", "", n).replace(",", ".") for n in NUMBER.findall(text or "")]


def _flat(text):
    return SPACES.sub(" ", text or "").strip()


def _cut(text, limit):
    """Text up to limit characters, cut at a word boundary, without trailing punctuation of the cut."""
    text = _flat(text)
    if len(text) <= limit:
        return text
    head = text[:limit + 1].rsplit(" ", 1)[0]
    return head.rstrip(" ,;:—-")


# ---- checks ------------------------------------------------------------------------------------------------------------
def check_analysis(value, brief_text):
    """Violations of the analyst answer; numbers of facts, title and subtitle must come from the brief."""
    violations = []
    source = set(numbers(brief_text))
    flat = _flat(brief_text)
    ids = [f["id"] for f in value["facts"]]
    if len(ids) != len(set(ids)):
        violations.append({"code": "DUPLICATE_FACT_ID"})
    for field in ("title", "subtitle"):
        extra = set(numbers(value[field])) - source
        if extra:
            violations.append({"code": "NUMBER_NOT_IN_BRIEF", "field": field, "numbers": sorted(extra)})
    for fact in value["facts"]:
        own = set(numbers(fact["text"]))
        if own - source:
            violations.append({"code": "NUMBER_NOT_IN_BRIEF", "fact": fact["id"], "numbers": sorted(own - source)})
        # A quote is a piece of the brief; the model may close it with its own mark where the brief goes on (Luna, 24.09:
        # "...точки доступа." for "...точки доступа, для ночных смен..."), so closing punctuation is not compared.
        if _flat(fact["quote"]).rstrip(" .,;:!?…") not in flat:
            violations.append({"code": "QUOTE_NOT_IN_BRIEF", "fact": fact["id"]})
        elif own - set(numbers(fact["quote"])):
            violations.append({"code": "NUMBER_NOT_IN_QUOTE", "fact": fact["id"], "numbers": sorted(own - set(numbers(fact["quote"])))})
    return violations


# ---- content package: facts by material (twelfth session, 25.09.2026) --------------------------------------------------------
# Run P of the blind test holdout-next (analysis/style-experiments/20260925-holdout-next-blind, M6b): on a content package of
# three materials (95 paragraphs) the live stand-in took 7 and 5 facts from the short history of the team only and ignored
# README and ARCHITECTURE; the decks had 8 and 6 slides for 12 asked. In the third run the answer failed the schema twice and
# the fallback kept the first 44 of 170 sentences, 43 of them from the first material. The analyst now hears how many
# slides the deck has and which materials the brief holds; too few facts and a material with no fact are "soft" violations:
# a live model repeats with the explanation, and an answer that still has only them is taken (never the sentence fallback).
PACKAGE_SHARE = .10          # a material with at least this share of the text of the package must give facts
FACTS_PER_SLIDE_WANTED = 2   # the analyst is asked for at least this many facts per content slide
# Owner remark of 28.09.2026 (server, template A): slide_writer moved the subject of an item into its label ("Удельный импульс")
# and began the text with the predicate ("Составляет около 450 секунд…"); cards that show no label (template A) printed items
# without a subject. An item text or a statement of content_writer that begins with such a predicate is a violation.
PREDICATE_START = frozenset("""составляет составляют является являются равен равна равно равны достигает достигают может могут
позволяет позволяют обеспечивает обеспечивают требует требуют ускоряет ускоряют использует используют работает работают
занимает занимают превышает превышают включает включают содержит содержат зависит зависят снижает снижают повышает повышают
увеличивает увеличивают сокращает сокращают помогает помогают даёт дает дают служит служат открывает открывают подходит подходят
делает делают создаёт создает создают""".split())
# Words ending as a verb of the third person plural (-ют, -ят) that are not verbs and may open a sentence.
PLURAL_NOT_VERB = frozenset("абсолют парашют салют приют уют валют".split())


def starts_with_predicate(text):
    """Whether a text begins with a predicate whose subject is elsewhere (in a label or the title): a verb of the list or a
    verb of the third person plural (-ют, -ят), alone or after «не». Reflexive forms (используется, применяются…) are not
    counted: in Russian they stand before their subject («Используется модель …»). A text that opens with a word in another
    script or with a number (run.json, Deep Space 1) opens with its subject."""
    words = [w for w in (re.sub(r"[^\w-]", "", t).lower() for t in text[:80].split()) if w]
    if not words:
        return False
    first = words[1] if words[0] == "не" and len(words) > 1 else words[0]
    if not re.fullmatch(r"[а-яё]+", first):
        return False
    plural = len(first) >= 5 and first.endswith(("ют", "ят")) and not first.endswith("десят") and first not in PLURAL_NOT_VERB
    return first in PREDICATE_START or plural


# Owner decision 54 (29.09.2026; rehearsal 5 of the video on template E): «Собирает презентацию…», «Разбирает макеты…» and «Строится
# на макетах образца…» passed the check above, 3 items of 19: a verb of the third person singular is known to it only from
# the list, a reflexive form not at all. An item is written from a fact, and the fact keeps its subject («Сервис разбирает
# образец…»): an item that opens with a word in the form of a finite verb has left its subject behind when the fact holds
# that word after its own first words and the item has none of those words; when the fact does not hold the word, when the
# item has not the first word of the fact. A reflexive form counts only before a preposition: «Используется модель …» has
# its subject after the verb. The past tense is not judged (its endings are those of too many nouns).
# The words are compared with «ё» written as «е» (_words), and so are these lists.
VERB_ENDINGS = ("ет", "ит", "ут", "ют", "ят", "жат", "чат", "шат", "щат")
REFLEXIVE_ENDING = "тся"
NOUN_ENDINGS = ("итет",)
NOT_VERB = PLURAL_NOT_VERB | frozenset("""аудит кредит лимит дефицит профицит визит транзит депозит реквизит магнит гранит композит
аппетит фаворит колорит сателлит метеорит паразит бандит бюджет пакет макет буклет билет планшет кабинет портрет запрет привет
секрет предмет сюжет балет скелет паркет жилет интернет факультет туалет пистолет браслет силуэт квартет диабет атлет омлет
трафарет турникет табурет рикошет цвет свет ответ совет рассвет завет полет взлет перелет налет самолет вертолет пулемет
отчет счет расчет учет зачет просчет институт маршрут атрибут статут мазут батут минут дебют принят занят поднят изъят
будет следует стоит значит хватит""".split())
PREPOSITIONS = frozenset("на в во по из с со к ко за для при через от до без над под о об у про между перед после".split())
FUNCTION_WORDS = frozenset("который которая которое которые также этот эта это эти свой своя свое свои".split())


def _words(text):
    return [w for w in (re.sub(r"[^\w-]", "", t).lower().replace("ё", "е") for t in text.split()) if w]


def _same_word(a, b):
    """The same word in another form: a common beginning of four letters or more that leaves at most two letters of the shorter."""
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return common >= 4 and common >= min(len(a), len(b)) - 2


def left_its_subject(text, source):
    """Whether an item opens with a finite verb whose subject stayed in the fact it was written from (decision 54)."""
    words, fact = _words(text[:160]), _words(source)
    if len(words) < 2 or not fact:
        return False
    at = 1 if words[0] == "не" else 0
    first = words[at]
    reflexive = first.endswith(REFLEXIVE_ENDING)
    if not re.fullmatch(r"[а-я]+", first) or len(first) < 5 or first in NOT_VERB:
        return False
    if not reflexive and (not first.endswith(VERB_ENDINGS) or first.endswith(NOUN_ENDINGS)):
        return False
    if reflexive and (len(words) <= at + 1 or words[at + 1] not in PREPOSITIONS):
        return False

    def content(found):
        return [w for w in found if len(w) > 3 and w not in FUNCTION_WORDS and w != "не"]

    rest = words[at + 1:]
    if first in fact:
        before = content(fact[:fact.index(first)])
        return bool(before) and not any(_same_word(b, w) for b in before for w in rest)
    opening = content(fact)[:1]
    return bool(opening) and not any(_same_word(opening[0], w) for w in words)


SOFT = {"FACTS_TOO_FEW", "MATERIAL_NOT_USED"}
# Violations of one fact of an analyst answer: that fact is left out and the others stay (live check P2, 25.09: one quote of
# 30 stitched from two list items failed the repeat as a whole, and the deck fell back to the sentences of the materials).
FACT_LEVEL = {"QUOTE_NOT_IN_BRIEF", "NUMBER_NOT_IN_QUOTE", "NUMBER_NOT_IN_BRIEF"}


FACT_LENGTH_ERROR = re.compile(r"^\$\.facts\[(\d+)\]\.(?:quote|text): длина \d+ вне границ$")


def drop_long_facts(value, errors):
    """Session 16 (26.09.2026, R1 of analysis/style-experiments/20260926-hn3-m7/process/research-charts-time.md): the facts of
    an analyst answer whose quote or text is longer than the schema allows are left out when those are all its schema errors
    (deck 24, package run: a quote of a whole row of a table, 364 signs for 300, failed an answer of 39 facts, the repeat
    failed again, and the deck fell back to 44 fragments of the materials). The first answer is repaired too."""
    bad = set()
    for error in errors:
        match = FACT_LENGTH_ERROR.match(error)
        if not match:
            return None
        bad.add(int(match.group(1)))
    facts = [f for n, f in enumerate(value.get("facts") or []) if n not in bad]
    return {**value, "facts": facts} if facts else None


def without_facts(value, violations):
    """(the answer without the facts whose own checks failed, their ids) when every hard violation of an analyst answer
    belongs to one fact; None when a violation concerns the whole answer (title, subtitle, duplicate ids) or none is hard."""
    hard = [v for v in violations if v["code"] not in SOFT]
    if not hard or any(v["code"] not in FACT_LEVEL or "fact" not in v for v in hard):
        return None
    bad = {v["fact"] for v in hard}
    return {**value, "facts": [f for f in value["facts"] if f["id"] not in bad]}, sorted(bad)


def material_marks(brief_text, materials):
    """(flat text, [(start, name)]) of a brief built from a content package: every material stands under its heading
    "## <name>" after the request of the user (materials.brief_text); a name not found in the text (cut by the limit) is
    left out."""
    flat = _flat(brief_text)
    marks = sorted((flat.find(f"## {name}"), name) for name in materials or [] if flat.find(f"## {name}") >= 0)
    return flat, marks


def fact_material(quote, flat, marks):
    """Name of the material whose part of the brief holds the quote; None for the request of the user or a quote not found."""
    at = flat.find(_flat(quote).rstrip(" .,;:!?…"))
    name = None
    for start, n in marks:
        if 0 <= start <= at:
            name = n
    return name


def check_package(value, brief_text, materials, slides, available):
    """Soft violations of an analyst answer on a content package: fewer facts than the deck needs while the materials hold
    more statements (available: sentences of the deterministic fallback), and a material with at least PACKAGE_SHARE of the
    text that gave no fact. No materials: none."""
    if not materials:
        return []
    flat, marks = material_marks(brief_text, materials)
    violations = []
    need = min(FACTS_PER_SLIDE_WANTED * slides, available // 2)
    if len(value["facts"]) < need:
        violations.append({"code": "FACTS_TOO_FEW", "facts": len(value["facts"]), "need": need, "slides": slides})
    used = {}
    for fact in value["facts"]:
        name = fact_material(fact["quote"], flat, marks)
        used[name] = used.get(name, 0) + 1
    ends = [start for start, _ in marks[1:]] + [len(flat)]
    total = sum(end - start for (start, _), end in zip(marks, ends)) or 1
    unused = [name for (start, name), end in zip(marks, ends) if (end - start) / total >= PACKAGE_SHARE and not used.get(name)]
    if unused:
        violations.append({"code": "MATERIAL_NOT_USED", "materials": unused,
                           "used": {name: used.get(name, 0) for _, name in marks}})
    return violations


def spread(facts, capacity, brief_text, materials):
    """(kept, left out) when there are more facts than the deck holds: without materials the first ones in brief order (A-01
    of the external review); with a content package every material, and the request of the user, gets an equal share
    (owner decision 17), in brief order within each. Run P, 25.09: the first 44 of 170 sentences were 43 of one material."""
    if len(facts) <= capacity:
        return facts, []
    flat, marks = material_marks(brief_text, materials)
    if not marks:
        return facts[:capacity], facts[capacity:]
    queues = {}
    for fact in facts:
        queues.setdefault(fact_material(fact["quote"], flat, marks), []).append(fact)
    queues, chosen = list(queues.values()), set()
    while len(chosen) < capacity and any(queues):
        for queue in queues:
            if queue and len(chosen) < capacity:
                chosen.add(queue.pop(0)["id"])
    return [f for f in facts if f["id"] in chosen], [f for f in facts if f["id"] not in chosen]


def slide_range(fact_count, requested, limits):
    """Content slides the planner may make: the requested deck minus the cover, within what the facts allow.

    Owner decision 43 (28.09.2026; organizers 17.09, 09:18-09:30: «создай презентацию до пяти слайдов… такая инструкция тоже
    должна быть учтена»): the user may ask for fewer than slides_min, down to slides_floor; a request of slides_min or more
    keeps the range it had (least slides_min - 1)."""
    total = min(max(requested, limits.get("slides_floor", limits["slides_min"])), limits["slides_max"])
    most = min(total - 1, fact_count)
    least = min(max(min(total, limits["slides_min"]) - 1, math.ceil(fact_count / limits["facts_per_slide_max"])), most)
    return least, most


def check_outline(value, facts, least, most, per_slide):
    violations = []
    known = {f["id"]: f for f in facts}
    used = [fid for s in value["slides"] for fid in s["fact_ids"]]
    skipped = [s["fact_id"] for s in value["skipped"]]
    if not least <= len(value["slides"]) <= most:
        violations.append({"code": "SLIDE_COUNT", "slides": len(value["slides"]), "range": [least, most]})
    unknown = sorted(set(used + skipped) - set(known))
    if unknown:
        violations.append({"code": "UNKNOWN_FACT", "facts": unknown})
    repeated = sorted({fid for fid in used if used.count(fid) > 1} | (set(used) & set(skipped)))
    if repeated:
        violations.append({"code": "FACT_REPEATED", "facts": repeated})
    missing = sorted(set(known) - set(used) - set(skipped))
    if missing:
        violations.append({"code": "FACT_NOT_COVERED", "facts": missing})
    for n, slide in enumerate(value["slides"]):
        if len(slide["fact_ids"]) > per_slide:
            violations.append({"code": "TOO_MANY_FACTS", "slide": n})
        allowed = {x for fid in slide["fact_ids"] if fid in known for x in numbers(known[fid]["text"])}
        extra = set(numbers(slide["title"])) - allowed
        if extra:
            # The title, its numbers and those of the facts go into the repeat of a live model (explain).
            violations.append({"code": "TITLE_NUMBER_NOT_IN_FACTS", "slide": n, "title": slide["title"], "numbers": sorted(extra),
                               "allowed": sorted(allowed)})
    return violations


def check_slide(value, facts, words_max):
    violations = []
    if [i["fact_id"] for i in value["items"]] != [f["id"] for f in facts]:
        violations.append({"code": "ITEMS_DO_NOT_MATCH_FACTS"})
        return violations
    allowed = {x for f in facts for x in numbers(f["text"])}
    extra = set(numbers(value["title"])) - allowed
    if extra:
        violations.append({"code": "TITLE_NUMBER_NOT_IN_FACTS", "title": value["title"], "numbers": sorted(extra), "allowed": sorted(allowed)})
    for item, fact in zip(value["items"], facts):
        if set(numbers(item["text"])) != set(numbers(fact["text"])):
            violations.append({"code": "ITEM_NUMBERS_CHANGED", "fact": fact["id"], "expected": sorted(set(numbers(fact["text"]))),
                               "got": sorted(set(numbers(item["text"])))})
        # The fallback puts the fact itself on the slide: an item longer than the limit is a violation only when it is also
        # longer than its fact (Luna, 24.09: 17 words for a fact of 20 were replaced by the 20 words).
        if len(item["text"].split()) > max(words_max, len(fact["text"].split())):
            violations.append({"code": "ITEM_TOO_LONG", "fact": fact["id"], "words": len(item["text"].split())})
        if len(item["label"].split()) > 4 or set(numbers(item["label"])) - set(numbers(fact["text"])):
            violations.append({"code": "LABEL_INVALID", "fact": fact["id"]})
        if starts_with_predicate(item["text"]) or left_its_subject(item["text"], fact["text"]):
            violations.append({"code": "ITEM_WITHOUT_SUBJECT", "fact": fact["id"], "text": item["text"][:60], "label": item["label"]})
    return violations


def check_supplement(value, request_text, facts, count):
    """Violations of an answer of content_writer (owner decision 47): more statements than asked, a number the request does
    not have, a statement without its subject, one that repeats a fact or another statement. Every violation but the count
    belongs to one statement (index), so an answer is taken without those (drop_supplement)."""
    violations = []
    if len(value["facts"]) > count:
        violations.append({"code": "SUPPLEMENT_TOO_MANY", "got": len(value["facts"]), "count": count})
    allowed = set(numbers(request_text))
    seen = {_flat(f["text"]).lower() for f in facts}
    for n, item in enumerate(value["facts"]):
        text = _flat(item["text"])
        extra = set(numbers(text)) - allowed
        if extra:
            violations.append({"code": "SUPPLEMENT_NUMBER_NOT_IN_REQUEST", "index": n, "text": text[:60], "numbers": sorted(extra)})
        elif starts_with_predicate(text):
            violations.append({"code": "SUPPLEMENT_WITHOUT_SUBJECT", "index": n, "text": text[:60]})
        elif text.lower() in seen:
            violations.append({"code": "SUPPLEMENT_REPEATED", "index": n, "text": text[:60]})
        seen.add(text.lower())
    return violations


def user_facts_kept(value, facts, added_ids):
    """Owner decision 47 (check C4, 29.09.2026): when content_writer added statements, the plan may not skip a statement of
    the text of the user (a one-sentence request lost both of its statements to 20 of the service)."""
    if not added_ids:
        return []
    skipped = [s["fact_id"] if isinstance(s, dict) else s for s in value.get("skipped", [])]
    lost = [fid for fid in skipped if fid in {f["id"] for f in facts} and fid not in added_ids]
    return [{"code": "USER_FACT_SKIPPED", "facts": lost}] if lost else []


def drop_supplement(value, violations, count):
    """(the answer without the statements that broke a rule and beyond the count, what was dropped), or None when none is
    left or a violation belongs to no statement."""
    bad = {v["index"] for v in violations if "index" in v}
    if any("index" not in v and v["code"] != "SUPPLEMENT_TOO_MANY" for v in violations):
        return None
    kept = [f for n, f in enumerate(value["facts"]) if n not in bad][:count]
    dropped = [f["text"][:60] for n, f in enumerate(value["facts"]) if n in bad]
    return ({"facts": kept}, dropped) if kept else None


def no_answer(record):
    """The violation of a call that gave no usable answer, with the detail of the failure (the path of a schema error)."""
    out = {"code": "NO_ANSWER", "reason": record.get("reason")}
    if record.get("detail"):
        out["detail"] = str(record["detail"])[:300]
    return out


# Plain-language explanations of the violations of an answer, for the one repeat a live model gets (twelfth session,
# 25.09.2026, docs/LLM_AGENT_ROLE_2026-09-25.md). Told only {"code": "TITLE_NUMBER_NOT_IN_FACTS", "slide": 1}, the stand-in
# model repeated "48 сотрудников" (42 + 6) in the title of the same slide in 5 of 5 repeats of the holdout run L, and the
# title fell back to the start of its first fact. The repeat now says which title, which numbers and which numbers the facts
# have; the codes and fields stay, so run.json and the fallbacks read as before.
EXPLAIN = {
    "NO_ANSWER": "Ответ не получен или не прошёл схему ({reason}: {detail}). Верни только JSON по схеме, без пояснений.",
    "DUPLICATE_FACT_ID": "Идентификаторы фактов повторяются: у каждого факта свой id — f01, f02, … по порядку.",
    "NUMBER_NOT_IN_BRIEF": "Числа {numbers} не встречаются в брифе. Бери числа только дословно из брифа.",
    "QUOTE_NOT_IN_BRIEF": "Цитата факта {fact} не найдена в брифе дословно: цитата — точный отрывок брифа.",
    "NUMBER_NOT_IN_QUOTE": "Числа {numbers} факта {fact} не входят в его цитату: все числа факта должны быть в цитате.",
    "FACTS_TOO_FEW": "Фактов {facts}, а на {slides} слайдов нужно не меньше {need}; в материалах утверждений больше. "
                     "Возьми больше фактов из всех материалов — дословно по тексту, не дроби утверждения и не выдумывай.",
    "MATERIAL_NOT_USED": "Из материалов {materials} не взято ни одного факта. Бери факты из каждого материала, "
                         "в первую очередь то, что относится к запросу пользователя.",
    "SLIDE_COUNT": "Слайдов {slides}, а нужно от {low} до {high}.",
    "UNKNOWN_FACT": "Неизвестные идентификаторы фактов {facts}: используй только id из входа.",
    "FACT_REPEATED": "Факты {facts} использованы больше одного раза.",
    "FACT_NOT_COVERED": "Факты {facts} не попали ни на один слайд и не указаны в skipped.",
    "TOO_MANY_FACTS": "На слайде {number} фактов больше, чем разрешено.",
    "TITLE_NUMBER_NOT_IN_FACTS": "В заголовке «{title}» есть числа {numbers}, которых нет в фактах этого слайда (в фактах: {allowed}). "
                                 "Числа в заголовке — только дословно из фактов слайда: не складывай, не округляй и не пересчитывай их; "
                                 "иначе убери число из заголовка.",
    "ITEMS_DO_NOT_MATCH_FACTS": "Пункты идут по фактам входа: по одному на факт, в том же порядке и с теми же fact_id.",
    "ITEM_NUMBERS_CHANGED": "В пункте факта {fact} изменились числа: в факте {expected}, в пункте {got}. Числа пункта — те же, что в факте.",
    "ITEM_TOO_LONG": "Пункт факта {fact} — {words} слов: сократи, сохранив числа и уточнения.",
    "LABEL_INVALID": "Подпись пункта факта {fact} длиннее четырёх слов или содержит числа не из факта.",
    "ITEM_WITHOUT_SUBJECT": "Пункт факта {fact} «{text}…» начинается со сказуемого и без подписи «{label}» не понятен: шаблон может не "
                            "показать подпись. Напиши текст пункта законченным утверждением с подлежащим в самом тексте.",
    "USER_FACT_SKIPPED": "Факты {facts} взяты из текста пользователя: их нельзя пропускать, поставь их на слайды "
                         "(пропускать можно только дописанные утверждения).",
    "SUPPLEMENT_TOO_MANY": "Утверждений {got}, а нужно не больше {count}.",
    "SUPPLEMENT_NUMBER_NOT_IN_REQUEST": "В утверждении «{text}…» есть числа {numbers}, которых нет в запросе: пиши без чисел или только "
                                        "с числами из запроса.",
    "SUPPLEMENT_WITHOUT_SUBJECT": "Утверждение «{text}…» начинается со сказуемого: подлежащее — в самом предложении.",
    "SUPPLEMENT_REPEATED": "Утверждение «{text}…» повторяет факт запроса или другое утверждение.",
    "NOTES_SLIDES_DO_NOT_MATCH": "Тексты идут по слайдам входа, в том же порядке: {expected}.",
    "NOTES_NUMBER_NOT_IN_FACTS": "В тексте слайда {slide} есть числа {numbers}, которых нет в его фактах: числа — только из фактов своего слайда.",
    "NOTES_SLIDE_TOO_LONG": "Текст слайда {slide} — {words} слов при пределе {limit}.",
    "NOTES_OVER_TIME": "Всего {words} слов при бюджете {limit}: сократи тексты.",
    "TITLE_TOO_LONG": "Заголовок слайда {slide_number} «{title}» — {length} знаков при пределе {limit}: сократи его.",
    "TITLE_UNCHANGED": "Ты вернул прежний заголовок «{title}»: исправления нет. Напиши другой заголовок — вывод из утверждений слайда, "
                       "который отвечает на замечание проверки.",
    "TITLE_NOT_ASKED": "Слайда {slide_number} нет во входе или он повторён: отвечай по слайдам входа, по одному разу.",
}


class _Fields(dict):
    def __missing__(self, key):
        return "?"


SCHEMA_WORDS = [
    (re.compile(r"\$\.facts\[(\d+)\]\.quote: длина (\d+) вне границ"),
     lambda m: f"Цитата факта № {int(m.group(1)) + 1} — {m.group(2)} знаков при пределе 300: возьми из абзаца или строки таблицы "
               "только отрывок с числами факта."),
    (re.compile(r"\$\.facts\[(\d+)\]\.text: длина (\d+) вне границ"),
     lambda m: f"Текст факта № {int(m.group(1)) + 1} — {m.group(2)} знаков при пределе 180: сократи его, сохранив числа."),
    (re.compile(r"\$\.slides\[(\d+)\]\.fact_ids: число элементов (\d+) вне границ"),
     lambda m: f"На слайде {int(m.group(1)) + 1} фактов {m.group(2)} при пределе на слайд: разнеси их по слайдам или укажи лишние в skipped."),
    (re.compile(r"\$\.metrics\[(\d+)\]\.items\[(\d+)\]\.value: длина (\d+) вне границ"),
     lambda m: f"Ключевое число № {int(m.group(2)) + 1} в наборе № {int(m.group(1)) + 1} — {m.group(3)} знаков, а нужно от 1 до 24: "
               "одно число цифрами со словом единицы; если у факта нет числа, карточек на этом слайде нет."),
    (re.compile(r"\$\.metrics\[(\d+)\]\.items: число элементов (\d+) вне границ"),
     lambda m: f"В наборе ключевых чисел № {int(m.group(1)) + 1} карточек {m.group(2)}, а нужно по одной на каждый факт слайда, "
               "от 2 до 4; на слайде с одним фактом карточек нет."),
]


def schema_words(detail):
    """Session 16 (D2'): the errors of a failed schema in plain words; the path of JSON alone did not help the repeat (deck 24
    Days: the analyst repeated the quote of 364 signs and added another one)."""
    words = [text(m) for pattern, text in SCHEMA_WORDS for m in pattern.finditer(detail or "")]
    return " ".join(words)


def explain(violations):
    """The violations of an answer, each with a message in plain words for the repeat of a live model."""
    out = []
    for v in violations:
        fields = _Fields({k: (", ".join(str(x) for x in value) if isinstance(value, list) else value) for k, value in v.items()})
        if isinstance(v.get("range"), list) and len(v["range"]) == 2:
            fields["low"], fields["high"] = v["range"]
        if isinstance(v.get("slide"), int):
            fields["number"] = v["slide"] + 1
        template = EXPLAIN.get(v.get("code"))
        message = template.format_map(fields) if template else None
        if message and v.get("code") == "NO_ANSWER" and schema_words(v.get("detail")):
            message += " " + schema_words(v.get("detail"))
        out.append({**v, "message": message} if message else dict(v))
    return out


# ---- fallbacks ---------------------------------------------------------------------------------------------------------
def fallback_analysis(free):
    """Sentences of the brief as facts; instructions addressed to a model are left out. The structure of a content package
    (item 34, materials.brief_text) is not a fact: a heading (the name of a material, a Markdown heading), a code block and
    the first row of a table without numbers (its header) are left out, a list marker is dropped, and a table row
    "cell | cell" becomes "cell: cell" with the row as its quote."""
    facts, dropped = [], []
    previous = ""
    for paragraph in (free["text"] or "").split("\n"):
        line = _flat(paragraph)
        if not line:
            continue
        row, header = " | " in line, " | " in line and " | " not in previous and not numbers(line)
        previous = line
        if HEADING.match(line) or line.startswith("```") or header:
            continue
        quote = None
        if row:
            cells = [c.strip() for c in line.split(" | ") if c.strip()]
            sentences, quote = [cells[0] + (": " + ", ".join(cells[1:]) if len(cells) > 1 else "")], line
        else:
            item = ITEM.match(line)
            sentences = SENTENCE.split(line[item.end():] if item else line)
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if INSTRUCTION.search(sentence):
                dropped.append(sentence)
                continue
            while sentence:
                piece = sentence if len(sentence) <= FACT_MAX else _cut(re.split(r"(?<=[;:])\s", sentence)[0], FACT_MAX)
                facts.append({"id": f"f{len(facts) + 1:02d}", "text": piece, "numbers": numbers(piece), "quote": quote or piece})
                sentence = sentence[len(piece):].strip(" ,;:")
    first = facts[0]["text"] if facts else "Презентация"
    subtitle = free.get("audience") or (facts[1]["text"] if len(facts) > 1 else first)
    return {"title": _cut(first.rstrip("."), TITLE_MAX), "subtitle": _cut(subtitle, 120), "language": free.get("language", "ru"),
            "facts": facts}, dropped


TITLE_TOO_LONG = re.compile(r"^\$\.slides\[(\d+)\]\.title: длина \d+ вне границ$")


def cut_titles(value, errors):
    """Repair of an outline whose only schema errors are titles over TITLE_MAX (Luna, 24.09: 3 plans of 5 had titles of 76 to
    88 signs and the whole plan fell back): the titles are cut at a word boundary, as the brief does with every title
    (_cut); slide_writer writes the final title anyway. Any other error keeps the answer failed."""
    found = [TITLE_TOO_LONG.match(e) for e in errors]
    if not found or not all(found):
        return None
    fixed = json.loads(json.dumps(value, ensure_ascii=False))
    for match in found:
        slide = fixed["slides"][int(match.group(1))]
        slide["title"] = _cut(slide["title"], TITLE_MAX)
    return fixed


VISUAL_ITEM_ERROR = re.compile(r"^\$\.(charts|metrics|processes)\[(\d+)\]")


def drop_bad_visuals(value, errors):
    """Repair of the repeated answer of chart_planner whose schema errors all sit inside items (session 15, run L2: deck 36,
    one key number of 30 signs where the schema allows 24 and an empty card, and the whole answer fell back, two good charts,
    two slides of cards and a process with it): those items are left out, the others stay. An error outside an item keeps
    the answer failed. Only the repeat: repairing the first answer instead of repeating lost two charts of deck 11 in the replay
    of the research (process/research-chart-planner of 20260926-hn2-rest)."""
    bad = set()
    for error in errors:
        match = VISUAL_ITEM_ERROR.match(error)
        if not match:
            return None
        bad.add((match.group(1), int(match.group(2))))
    fixed = json.loads(json.dumps(value, ensure_ascii=False))
    for kind in ("charts", "metrics", "processes"):
        fixed[kind] = [item for n, item in enumerate(fixed.get(kind) or []) if (kind, n) not in bad]
    return fixed


def fallback_outline(facts, least, most):
    """Facts in brief order, spread evenly over the planned number of slides; the title is the start of the first fact."""
    count = max(least, min(most, len(facts)))
    size, extra = divmod(len(facts), count) if count else (0, 0)
    slides, start = [], 0
    for n in range(count):
        end = start + size + (1 if n < extra else 0)
        group = facts[start:end]
        slides.append({"title": _cut(group[0]["text"].rstrip("."), 60), "fact_ids": [f["id"] for f in group]})
        start = end
    return {"slides": slides, "skipped": []}


def fallback_slide(title, facts):
    return {"title": title, "items": [{"fact_id": f["id"], "label": None, "text": f["text"]} for f in facts]}


def check_charts(value, sections):
    """Violations of every chart of the chart_planner answer, by its position: a known slide, one chart per slide, and
    charts.check_chart against the facts of that slide."""
    by_id = {s["id"]: s for s in sections}
    seen, found = set(), []
    for n, chart in enumerate(value["charts"]):
        section = by_id.get(chart["slide"])
        if section is None:
            found.append((n, [{"code": "CHART_DATA_INVALID", "reason": "unknown-slide"}]))
        elif chart["slide"] in seen:
            found.append((n, [{"code": "CHART_DATA_INVALID", "reason": "second-chart-on-slide"}]))
        else:
            found.append((n, check_chart({k: v for k, v in chart.items() if k != "slide"}, section)))
        seen.add(chart["slide"])
    return found


def check_visuals(value, sections):
    """G2: violations of the key numbers and processes of the chart_planner answer, by kind and position."""
    by_id = {s["id"]: s for s in sections}
    found = []
    for kind, check in (("metrics", lambda item, s: check_metrics(item["items"], s)), ("processes", lambda item, s: check_process(True, s))):
        seen = set()
        for n, item in enumerate(value.get(kind, [])):
            section = by_id.get(item["slide"])
            if section is None or item["slide"] in seen:
                found.append((kind, n, [{"code": "METRIC_INVALID", "reason": "unknown-slide" if section is None else "second-on-slide"}]))
            else:
                found.append((kind, n, check(item, section)))
            seen.add(item["slide"])
    return found


# Owner decision 33 (Н14 б, 27.09.2026): an item a rule-following answer would not give at all loses nothing on the slide; it
# stays a violation of the call (record["violations"]), not a fallback of the plan — as decision 31 (Н13) did for a second
# chart. Such items: key numbers on a slide that does not have 2-4 facts each with a number in digits (a card "план" or "в
# октябре"); a process (a process holds only 3-4 facts in order, check_process refuses nothing else). Run L of holdout-next3:
# 9 of 14 fallbacks; run L2 after prompt v3: 6 of 6. A chart is not judged so: a wrong unit or a single value can hide a chart
# a rule-following answer gives ("сек" for facts in minutes), so only the second chart of decision 31 stays out.
def gives_nothing(kind, item, problems, section):
    if section is None:
        return False
    if kind == "processes":
        return True
    if kind == "metrics":
        facts = section["facts"]
        return not (2 <= len(facts) <= 4 and all(numbers(f["text"]) for f in facts))
    return False


def charts(brief, language, provider, record_dir, root, report):
    """G1, G2: charts of the numbers of facts, key numbers and processes (agent chart_planner). Every item is checked on
    its own; an item that fails is left out and recorded as a fallback, the others stay. No answer: none of them, the facts
    are shown as text."""
    sections = brief["sections"]
    payload = {"deck_title": brief["title"], "language": language,
               "slides": [{"id": s["id"], "title": s["title"], "facts": [{"id": f["id"], "text": f["text"]} for f in s["facts"]]} for s in sections]}
    value, record = llm.call("chart_planner", payload, provider, record_dir, root)
    report["calls"].append(record)
    if value is None and record.get("reason") in ("schema", "no-json") and llm.is_live(provider, root):
        # Twelfth session: an answer outside the schema of a live model gets one repeat with the error of the schema (Luna,
        # 25.09: key numbers with one item where the schema asks two; the whole answer fell back and no chart was left).
        again, retry = llm.call("chart_planner", {**payload, "previous_answer_problems": explain([no_answer(record)])}, provider, record_dir, root,
                                repair=drop_bad_visuals)
        retry["retry_of"] = record["request_sha256"]
        report["calls"].append(retry)
        if again is not None:
            value, record = again, retry
            if retry.get("repaired"):
                report["fallbacks"].append({"agent": "chart_planner", "problems": [{"code": "SCHEMA_ITEMS_DROPPED", "errors": retry["repaired"]}]})
    if value is None:
        report["fallbacks"].append({"agent": "chart_planner", "problems": [no_answer(record)]})
        report["charts"] = report["metrics"] = report["processes"] = 0
        return
    checked = check_charts(value, sections)
    visuals = check_visuals(value, sections)
    record["violations"] = [dict(v, chart=n) for n, problems in checked for v in problems] + [dict(v, **{kind: n}) for kind, n, problems in visuals for v in problems]
    by_id = {s["id"]: s for s in sections}
    for (n, problems), chart in zip(checked, value["charts"]):
        if problems:
            # Owner decision 31 (Н13, 26.09.2026): a second chart for a slide that already has an accepted one loses nothing on
            # the slide (the layout draws one chart); it stays a violation of the call (record["violations"]), not a fallback
            # of the plan (runs L and L2: 3 of 10 and 3 of 8 fallbacks were such, minutes and % "before and after").
            if all(p.get("reason") == "second-chart-on-slide" for p in problems) and "chart" in by_id[chart["slide"]]:
                continue
            report["fallbacks"].append({"agent": "chart_planner", "slide": chart["slide"], "problems": problems})
        else:
            by_id[chart["slide"]]["chart"] = {k: v for k, v in chart.items() if k != "slide"}
    for kind, n, problems in visuals:
        item = value[kind][n]
        if problems:
            if not gives_nothing(kind, item, problems, by_id.get(item.get("slide"))):
                report["fallbacks"].append({"agent": "chart_planner", "slide": item["slide"], "problems": problems})
        elif kind == "metrics":
            by_id[item["slide"]]["metrics"] = [dict(m) for m in item["items"]]
        else:
            by_id[item["slide"]]["process"] = True
    report["charts"] = sum("chart" in s for s in sections)
    report["metrics"] = sum("metrics" in s for s in sections)
    report["processes"] = sum("process" in s for s in sections)


# ---- the pipeline --------------------------------------------------------------------------------------------------------
def structure(free, provider=None, record_dir=None, root=llm.ROOT, workers=6, materials=None):
    """vsp.brief/1 from a vsp.free-brief/1, and the record of every call, check and fallback for run.json. materials:
    names of the materials of a content package in the order the brief text holds them under "## <name>"."""
    errors = llm.validate(free, json.loads(llm._read(root, "schemas/free_brief.json")))
    if errors:
        raise ValueError("Неверный свободный бриф: " + "; ".join(errors[:5]))
    limits = json.loads(llm._read(root, llm.REGISTRY))["limits"]
    language = free.get("language", "ru")
    report = {"schema": "vsp.planning/1", "calls": [], "fallbacks": []}

    live = llm.is_live(provider, root)

    def attempt(agent, payload, check, repair=None, accept=None):
        value, record = llm.call(agent, payload, provider, record_dir, root, repair)
        report["calls"].append(record)
        attempt.used = record
        violations = [no_answer(record)] if value is None else check(value)
        if value is not None:
            record["violations"] = violations
        attempt.repeated = None
        attempt.partial = None
        if violations and live and value is not None and accept is not None:
            # Owner decision 34 (27.09.2026, R2 of analysis/style-experiments/20260926-hn3-m7/process/research-charts-time.md):
            # an answer whose hard violations each belong to one fact is taken at once without those facts; the repeat (37-60 s
            # on a content package) gave no more facts in 11 decks, failed itself in three and once sent the deck to the
            # sentences of the materials. The record is the partial fallback it would have become after a failed repeat.
            taken = accept(value, violations)
            if taken is not None:
                attempt.rejected = value
                attempt.partial = {"problems": violations, "dropped": taken[1]}
                return taken[0], []
        if violations and live:
            # Proposals of 24.09.2026: a live model gets one more call with what its answer broke (a whole answer used to
            # fall back to the deterministic path over one quote or one title); recorded answers stay as they were. The
            # repeat replaces the first answer only when it passes; otherwise the first one goes on (Luna, 24.09: the
            # repeated plan kept its title and broke the schema).
            again, record = llm.call(agent, {**payload, "previous_answer_problems": explain(violations)}, provider, record_dir, root, repair)
            record["retry_of"] = report["calls"][-1]["request_sha256"]
            report["calls"].append(record)
            problems = [no_answer(record)] if again is None else check(again)
            if again is not None:
                record["violations"] = problems
                attempt.repeated = again
            if not problems:
                value, violations = again, []
                attempt.used = record
        attempt.rejected = value
        return (None, violations) if violations else (value, [])

    base = {k: free.get(k, "") for k in ("purpose", "audience", "consumer", "wishes")}
    # Twelfth session: the analyst hears the size of the deck (content slides and facts a slide holds) and, for a content
    # package, the names of its materials; see check_package for the soft violations of such an answer.
    content = min(max(free.get("slides", 12), limits.get("slides_floor", limits["slides_min"])), limits["slides_max"]) - 1
    request = {"brief": free["text"], **base, "language": language, "slides": content, "facts_per_slide_max": limits["facts_per_slide_max"]}
    if materials:
        request["materials"] = list(materials)
    available = len(fallback_analysis(free)[0]["facts"])

    def check(v):
        return check_analysis(v, free["text"]) + check_package(v, free["text"], materials, content, available)
    def trim(value, violations):
        trimmed = without_facts(value, violations)
        return trimmed if trimmed and trimmed[0]["facts"] and all(p["code"] in SOFT for p in check(trimmed[0])) else None
    analysis, problems = attempt("brief_analyst", request, check, drop_long_facts, accept=trim)
    if analysis is not None and attempt.used.get("repaired"):
        report["fallbacks"].append({"agent": "brief_analyst", "problems": [{"code": "SCHEMA_FACTS_DROPPED", "errors": attempt.used["repaired"]}]})
    if analysis is not None and attempt.partial:
        report["fallbacks"].append({"agent": "brief_analyst", "problems": attempt.partial["problems"], "partial": "facts", "dropped": attempt.partial["dropped"]})
    if analysis is None:
        # An answer (the first or the repeat) with only soft violations, or whose hard ones each belong to one fact that is
        # then left out: the one with the most facts is taken, not the sentences of the fallback (run P: 43 of 44 of them
        # from the first material, fragments of lists and code).
        options = []
        for answer in (attempt.rejected, attempt.repeated):
            if answer is None:
                continue
            found, dropped = check(answer), []
            trimmed = without_facts(answer, found)
            if trimmed and trimmed[0]["facts"]:
                answer, dropped = trimmed
            if all(p["code"] in SOFT for p in check(answer)):
                options.append((answer, found, dropped))
        if options:
            analysis, problems, dropped = max(options, key=lambda o: len(o[0]["facts"]))
            report["fallbacks"].append({"agent": "brief_analyst", "problems": problems, "partial": "facts", **({"dropped": dropped} if dropped else {})})
    if analysis is None:
        analysis, dropped = fallback_analysis(free)
        report["fallbacks"].append({"agent": "brief_analyst", "problems": problems, "left_out": dropped})
    facts = [{"id": f["id"], "text": f["text"], "quote": f["quote"]} for f in analysis["facts"]]
    # Owner decision 47 (28.09.2026, «1 — в), делай вместе с сообщением из а)»): a request with fewer statements than two per
    # requested content slide (the server, 28.09: one sentence of 99 signs, 2 facts, 3 slides of 12) gets statements of
    # content_writer on its topic: without numbers the request does not have, each one marked as added by the model (source
    # "model"), found by the audit (CONTENT_ADDED_BY_MODEL) and counted for the page (report["supplement"]). The statements of
    # the user are neither changed nor dropped; a structured brief is never supplemented; recorded answers are not asked.
    need = FACTS_PER_SLIDE_WANTED * content
    report["supplement"] = {"from_text": len(facts), "added": 0, "need": need, "requested_slides": content + 1}
    added_ids = set()
    if live and len(facts) < need:
        count = need - len(facts)
        asked = {"request": free["text"][:6000], "title": analysis["title"], "purpose": free.get("purpose", ""), "audience": free.get("audience", ""),
                 "facts": [{"id": f["id"], "text": f["text"]} for f in facts], "count": count, "language": language}
        written, problems = attempt("content_writer", asked, lambda v: check_supplement(v, free["text"], facts, count),
                                    accept=lambda v, found: drop_supplement(v, found, count))
        if attempt.partial:
            report["fallbacks"].append({"agent": "content_writer", "problems": attempt.partial["problems"], "partial": "facts", "dropped": attempt.partial["dropped"]})
        if written is None:
            report["fallbacks"].append({"agent": "content_writer", "problems": problems})
        else:
            start = max([int(f["id"][1:]) for f in facts] or [0]) + 1
            for n, item in enumerate(written["facts"][:count]):
                fid = f"f{start + n:02d}"
                facts.append({"id": fid, "text": _cut(_flat(item["text"]), FACT_MAX), "quote": ""})
                added_ids.add(fid)
            report["supplement"]["added"] = len(added_ids)
    least, most = slide_range(len(facts), free.get("slides", 12), limits)
    # A-01 of the external review of 24.09.2026: the deck holds at most `most` slides of facts_per_slide_max facts (14 x 4 = 56
    # for 15 slides); more facts made the fallback plan put a fifth fact on a slide and the brief ended in an error. The facts
    # beyond the capacity are left out on the record (run.json, planning.omitted), never silently; a content package makes
    # such briefs usual (item 34), and then every material keeps an equal share (spread).
    capacity = most * limits["facts_per_slide_max"]
    if len(facts) > capacity:
        facts, left = spread(facts, capacity, free["text"], materials)
        report["omitted"] = [{"id": f["id"], "text": f["text"], "reason": "capacity"} for f in left]
        least, most = slide_range(len(facts), free.get("slides", 12), limits)
    # Owner remark of 28.09.2026 (12 asked, 3 and on 29.09 still 10 issued, check C3): a live model is asked for the number of
    # slides the user asked for whenever the facts allow it. The recorded answers of the stand-in keep the range they were
    # recorded with (their requests would change otherwise).
    if live:
        least = most
    outline, problems = attempt("outline_planner", {"title": analysis["title"], **base, "facts": [{"id": f["id"], "text": f["text"]} for f in facts],
                                                    "slides_min": least, "slides_max": most, "facts_per_slide_max": limits["facts_per_slide_max"],
                                                    "language": language},
                                lambda v: check_outline(v, facts, least, most, limits["facts_per_slide_max"]) + user_facts_kept(v, facts, added_ids), cut_titles)
    if outline is None and attempt.rejected is not None and problems and all(v["code"] == "TITLE_NUMBER_NOT_IN_FACTS" and "slide" in v for v in problems):
        # Only titles carry numbers the facts do not have (Luna, 24.09: 42 + 6 summed to "48 сотрудников"): the plan stays and
        # those titles take the start of their first fact, as the fallback plan titles every slide.
        outline = json.loads(llm.canonical(attempt.rejected))
        first = {f["id"]: f for f in facts}
        # Twelfth session: a repeat that fixed this title but broke others (Luna on deck 10, 25.09: "48 сотрудников" gone, two
        # titles on regrouped facts wrong) still gives its title to the slide with the same facts when that title is valid.
        repeated = {tuple(s["fact_ids"]): s["title"] for s in (attempt.repeated or {}).get("slides", [])}
        for v in problems:
            slide = outline["slides"][v["slide"]]
            allowed = {x for fid in slide["fact_ids"] if fid in first for x in numbers(first[fid]["text"])}
            title = repeated.get(tuple(slide["fact_ids"]))
            if title and len(title) <= TITLE_MAX and not set(numbers(title)) - allowed:
                slide["title"] = title
                v["repaired_from"] = "repeat"
            else:
                slide["title"] = _cut(first[slide["fact_ids"][0]]["text"].rstrip("."), 60)
        report["fallbacks"].append({"agent": "outline_planner", "problems": problems, "partial": "titles"})
    if outline is None:
        outline = fallback_outline(facts, least, most)
        report["fallbacks"].append({"agent": "outline_planner", "problems": problems})
    by_id = {f["id"]: f for f in facts}

    def write(slide):
        group = [by_id[fid] for fid in slide["fact_ids"]]
        value, problems = attempt("slide_writer", {"title": slide["title"], "facts": [{"id": f["id"], "text": f["text"]} for f in group],
                                                   "audience": free.get("audience", ""), "language": language},
                                  lambda v: check_slide(v, group, limits["words_per_item_max"]))
        if value is None:
            report["fallbacks"].append({"agent": "slide_writer", "slide": slide["title"], "problems": problems})
            value = fallback_slide(slide["title"], group)
        return value

    with ThreadPoolExecutor(max_workers=workers) as pool:
        written = list(pool.map(write, outline["slides"]))
    sections = []
    for n, (slide, text) in enumerate(zip(outline["slides"], written), 1):
        section_facts = []
        for item in text["items"]:
            fact = by_id[item["fact_id"]]
            entry = {"id": fact["id"], "text": _cut(item["text"], FACT_MAX), "source": "brief", "origin": {"quote": fact["quote"], "analysis": fact["text"]}}
            if fact["id"] in added_ids:
                # Owner decision 47: a statement of the model, not of the user; no quote of the request stands behind it.
                entry["source"], entry["origin"] = "model", {"added_by": "content_writer", "analysis": fact["text"]}
            if item.get("label"):
                entry["label"] = item["label"]
            section_facts.append(entry)
        sections.append({"id": f"s{n:02d}", "title": _cut(text["title"], TITLE_MAX), "kind": "text", "facts": section_facts})
    brief = {"schema": "vsp.brief/1", "title": analysis["title"], "subtitle": analysis["subtitle"],
             "disclosure": free.get("disclosure") or DEFAULT_DISCLOSURE,
             "audience": free.get("audience", ""), "consumer": free.get("consumer", ""), "wishes": free.get("wishes", ""),
             "sources": [{"id": "brief", "description": free.get("source") or "Свободный бриф пользователя"}]
             + ([{"id": "model", "description": MODEL_SOURCE}] if added_ids else []), "sections": sections}
    charts(brief, language, provider, record_dir, root, report)
    report["skipped"] = outline.get("skipped", [])
    # A slide with one fact looks empty; arithmetic allows max(0, 2 * slides - facts) of them (outline_planner v2, rule 4).
    used = sum(len(s["facts"]) for s in sections)
    report["single_fact_slides"] = {"count": sum(len(s["facts"]) == 1 for s in sections), "minimum": max(0, 2 * len(sections) - used)}
    return brief, report
