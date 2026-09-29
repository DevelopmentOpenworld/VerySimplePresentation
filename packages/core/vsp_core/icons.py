"""G3: pictograms of the sample itself (docs/ICONS.md; plan, section G, item 3).

read_icons collects the pictograms of the sample slides: small one-colour PNG with transparency, not repeated furniture.
card_icon_slots marks in every card of an A7 sample the member that carries such a pictogram. The agent icon_tagger
names what every pictogram shows (a request per batch of pictures, cached by the SHA-256 of the request, since it depends
on the sample only); icon_picker chooses for the facts of every text slide a pictogram by its meaning, or none. Both
answers pass deterministic checks; without an answer the slides keep the pictograms of the sample. The composer puts a
chosen pictogram in the place of the sample one, recoloured in its ink (png.recolour).
"""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import struct

from . import llm, png
from .importer import digest, pictures
from .opc import parse_xml
from .reference import NS

# A pictogram covers at most this share of the slide where the sample shows it, has sides of 16..512 px of nearly
# square proportions, and a drawn part of one colour that does not fill its canvas (a filled square is a backing).
ICON_AREA = .03
PIXELS = (16, 512)
ASPECT = (.6, 1 / .6)
COVERAGE_MAX = .85
TAG_BATCH = 24
KINDS = ("icon", "marker", "logo", "shape", "number", "other")
# Kinds a card may carry in its icon place: a pictogram with a meaning or a neutral list marker (arrow, check, dot).
SLOT_KINDS = ("icon", "marker")
_MEMO = {}


def _png_size(raw):
    if raw[:8] != png.SIGNATURE or raw[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", raw[16:24])


def _measure(raw):
    key = digest(raw)
    if key not in _MEMO:
        try:
            _MEMO[key] = png.ink(*png.decode(raw))
        except Exception as error:  # malformed data: zlib.error, struct.error, IndexError
            _MEMO[key] = {"error": type(error).__name__}
    return _MEMO[key]


def read_icons(package, inventory):
    """Pictograms of the sample slides in the order of their first appearance (JSON-compatible)."""
    width, height = inventory["width"], inventory["height"]
    furniture = {target for item in inventory.get("furniture", []) if item["kind"] == "pic"
                 for _, target, external in item["rels"].values() if not external}
    found = {}
    for slide in inventory["slides"]:
        tree = parse_xml(package.parts[slide["part"]]).find("p:cSld/p:spTree", NS)
        rels = {r.id: r for r in package.rels(slide["part"])}
        for pic, box, _ in pictures(tree):
            blip = pic.find("p:blipFill/a:blip", NS)
            rel = rels.get(blip.get(f"{{{NS['r']}}}embed")) if blip is not None else None
            if rel is None or rel.external or box is None:
                continue
            media = package.resolve(slide["part"], rel.target)
            raw = package.parts.get(media)
            size = _png_size(raw) if raw else None
            if size is None or media in furniture or box[2] * box[3] > width * height * ICON_AREA:
                continue
            w, h = size
            if not (PIXELS[0] <= min(w, h) and max(w, h) <= PIXELS[1] and ASPECT[0] <= w / h <= ASPECT[1]):
                continue
            key = digest(raw)
            if key not in found:
                stats = _measure(raw)
                if stats is None or "error" in stats or stats["one_colour"] < png.ONE_COLOUR or stats["coverage"] > COVERAGE_MAX:
                    found[key] = None
                    continue
                found[key] = {"id": key[:12], "sha256": key, "mime": "image/png", "px": [w, h], **stats, "slides": [],
                              "data": base64.b64encode(raw).decode()}
            if found[key] is not None and slide["number"] not in found[key]["slides"]:
                found[key]["slides"].append(slide["number"])
    return [icon for icon in found.values() if icon is not None]


def card_icon_slots(samples, icons):
    """Every card of an A7 sample gets `icon_candidates`: its members that draw a pictogram of the catalogue, the
    largest first (a card may have a dot marker next to its icon), as {"member", "asset", "area"}."""
    known = {i["sha256"] for i in icons}
    for sample in samples:
        nodes = {n["id"]: n for n in sample["nodes"]}
        for card in sample["cards"]:
            found = []
            for member in card["members"]:
                for prim in nodes[member]["primitives"]:
                    if prim["type"] == "image" and prim["asset"] in known:
                        found.append({"member": member, "asset": prim["asset"], "area": round(prim["box"][2] * prim["box"][3], 1)})
            card["icon_candidates"] = sorted(found, key=lambda c: -c["area"])


def has_slots(samples):
    """Whether any sample of the reference has a place for a pictogram (only then the catalogue is worth tagging)."""
    return any(card.get("icon_candidates") for s in samples for card in s["cards"]) or any(s.get("kind") == "units" for s in samples)


# ---- icon_tagger -------------------------------------------------------------------------------------------------------
def tag_payloads(icons, language="ru"):
    """Inputs of icon_tagger: the catalogue in batches of TAG_BATCH pictures, numbered from 1 within a batch."""
    for start in range(0, len(icons), TAG_BATCH):
        batch = icons[start:start + TAG_BATCH]
        yield [i["id"] for i in batch], {"language": language, "pictures": [{"picture": n + 1} for n in range(len(batch))],
                                         "images": [{"mime": i["mime"], "data": i["data"]} for i in batch]}


def check_tags(value, count):
    """Violations of an icon_tagger answer: one answer per picture; a pictogram (icon) needs its meaning and keywords."""
    violations = []
    asked = sorted(a["picture"] for a in value["pictures"])
    if asked != list(range(1, count + 1)):
        violations.append({"code": "PICTURES_DO_NOT_MATCH", "pictures": asked})
    violations += [{"code": "ICON_WITHOUT_MEANING", "picture": a["picture"]} for a in value["pictures"]
                   if a["kind"] == "icon" and (not a["meaning"].strip() or not a["keywords"])]
    return violations


def _cached(cache, request):
    path = Path(cache) / "icon_tagger" / f"{request['sha256']}.json" if cache else None
    if path is not None and path.is_file():
        return path, json.loads(path.read_bytes().decode("utf-8"))
    return path, None


def tag_icons(icons, provider=None, record_dir=None, root=llm.ROOT, cache=None, workers=6):
    """Tags of the catalogue by icon id, the calls for run.json and the fallbacks. cache: a folder for checked answers of
    live calls by the SHA-256 of the request (the request depends on the pictures only); replay needs none."""
    report = {"calls": [], "fallbacks": [], "tags": {}}

    def one(item):
        ids, payload = item
        request = llm.build_request("icon_tagger", payload, root)
        path, hit = _cached(cache, request)
        if hit is not None:
            return ids, hit["response"], {"agent": "icon_tagger", "request_sha256": request["sha256"], "prompt_version": request["prompt_version"],
                                          "prompt_sha256": request["prompt_sha256"], "provider": "cache", "ok": True, "milliseconds": 0}, []
        value, record = llm.call("icon_tagger", payload, provider, record_dir, root)
        problems = [{"code": "NO_ANSWER", "reason": record.get("reason")}] if value is None else check_tags(value, len(ids))
        if value is not None:
            record["violations"] = problems
        if path is not None and value is not None and not problems and record.get("provider") != "replay":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(json.dumps({"request_sha256": request["sha256"], "response": value}, ensure_ascii=False).encode("utf-8"))
        return ids, (None if problems else value), record, problems

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, list(tag_payloads(icons))))
    for ids, value, record, problems in results:
        report["calls"].append(record)
        if value is None:
            report["fallbacks"].append({"agent": "icon_tagger", "icons": len(ids), "problems": problems})
            continue
        for a in value["pictures"]:
            report["tags"][ids[a["picture"] - 1]] = {"kind": a["kind"], "meaning": a["meaning"].strip(), "keywords": a["keywords"]}
    return report


# ---- icon_picker -------------------------------------------------------------------------------------------------------
def pick_payload(brief, icons, tags):
    """Input of icon_picker: the text slides of the brief with their items and the pictograms with a meaning, or None."""
    offered = [{"icon": i["id"], "meaning": tags[i["id"]]["meaning"], "keywords": tags[i["id"]]["keywords"]}
               for i in icons if tags.get(i["id"], {}).get("kind") == "icon"]
    slides = [{"slide": s["id"], "title": s["title"],
               "items": [{"item": n + 1, "label": f.get("label", ""), "text": f["text"]} for n, f in enumerate(s["facts"])]}
              for s in brief["sections"] if s["kind"] == "text" and 2 <= len(s["facts"]) <= 4]
    if not offered or not slides:
        return None
    return {"deck_title": brief["title"], "language": brief.get("language", "ru"), "slides": slides, "icons": offered}


def check_picks(value, payload):
    """Violations of an icon_picker answer by slide (None for the whole answer): every slide once; a slide gets a
    pictogram for every item or for none; pictograms are offered ones and differ within a slide."""
    offered = {i["icon"] for i in payload["icons"]}
    items = {s["slide"]: len(s["items"]) for s in payload["slides"]}
    violations = []
    answered = [s["slide"] for s in value["slides"]]
    if sorted(answered) != sorted(items):
        violations.append({"code": "SLIDES_DO_NOT_MATCH", "slide": None, "slides": answered})
    for s in value["slides"]:
        if s["slide"] not in items:
            continue
        chosen = s["icons"]
        if chosen and sorted(c["item"] for c in chosen) != list(range(1, items[s["slide"]] + 1)):
            violations.append({"code": "ICON_FOR_SOME_ITEMS", "slide": s["slide"]})
        if any(c["icon"] not in offered for c in chosen):
            violations.append({"code": "ICON_NOT_OFFERED", "slide": s["slide"]})
        if len({c["icon"] for c in chosen}) != len(chosen):
            violations.append({"code": "ICON_REPEATED_ON_SLIDE", "slide": s["slide"]})
    return violations


def pick_icons(brief, icons, tags, provider=None, record_dir=None, root=llm.ROOT):
    """{section id: {fact id: icon id}} for the slides the picker gave pictograms, the call and the fallbacks."""
    report = {"calls": [], "fallbacks": [], "picks": {}}
    payload = pick_payload(brief, icons, tags)
    if payload is None:
        return report
    value, record = llm.call("icon_picker", payload, provider, record_dir, root)
    report["calls"].append(record)
    if value is None:
        report["fallbacks"].append({"agent": "icon_picker", "problems": [{"code": "NO_ANSWER", "reason": record.get("reason")}]})
        return report
    problems = check_picks(value, payload)
    record["violations"] = problems
    if any(p["slide"] is None for p in problems):
        report["fallbacks"].append({"agent": "icon_picker", "problems": problems})
        return report
    rejected = {p["slide"] for p in problems}
    if rejected:
        report["fallbacks"].append({"agent": "icon_picker", "slides": sorted(rejected), "problems": problems})
    sections = {s["id"]: s for s in brief["sections"]}
    for s in value["slides"]:
        if s["icons"] and s["slide"] not in rejected:
            facts = sections[s["slide"]]["facts"]
            report["picks"][s["slide"]] = {facts[c["item"] - 1]["id"]: c["icon"] for c in s["icons"]}
    return report


# ---- the step of the pipeline ----------------------------------------------------------------------------------------
def choose(brief, style, provider=None, record_dir=None, root=llm.ROOT, cache=None):
    """Tags and picks for a brief on a reference, written into style["carrier"]["icon_choice"] for the composer; the
    report (calls, fallbacks, counts) for run.json. A reference without places for pictograms makes no calls."""
    carrier = style.get("carrier") or {}
    icons = carrier.get("icons") or []
    report = {"schema": "vsp.icon-choice/1", "icons": len(icons), "calls": [], "fallbacks": [], "tagged": 0, "slides": 0}
    if not icons or not has_slots(carrier.get("card_samples", [])):
        return report
    tagged = tag_icons(icons, provider, record_dir, root, cache)
    picked = pick_icons(brief, icons, tagged["tags"], provider, record_dir, root)
    report["calls"] = tagged["calls"] + picked["calls"]
    report["fallbacks"] = tagged["fallbacks"] + picked["fallbacks"]
    report["tagged"] = len(tagged["tags"])
    report["slides"] = len(picked["picks"])
    carrier["icon_choice"] = {"tags": tagged["tags"], "picks": picked["picks"]}
    return report


def slot_of(card, tags):
    """The place for a pictogram of a card: its largest catalogue picture tagged as a pictogram or a marker, or None."""
    return next((c for c in card.get("icon_candidates", []) if tags.get(c["asset"][:12], {}).get("kind") in SLOT_KINDS), None)


def recoloured(icons, icon_id, ink, aspect):
    """Base64 PNG of a pictogram of the catalogue in the ink of the place it takes, padded to the proportions of that
    place (width / height)."""
    icon = next(i for i in icons if i["id"] == icon_id)
    return base64.b64encode(png.recolour(base64.b64decode(icon["data"]), ink, icon["ink"], aspect)).decode()


def places_icons(sample, plan, style, section):
    """Whether every kept card of a planned card slide has a place for the pictogram the picker chose for its fact."""
    choice = (style.get("carrier") or {}).get("icon_choice") or {}
    picks = choice.get("picks", {}).get(section)
    cards = {c["container"]: c for c in sample["cards"]}
    return bool(picks) and all(kept["fact"] in picks and slot_of(cards[kept["container"]], choice.get("tags", {})) is not None
                               for kept in plan["cards"])


def card_icons(sample, plan, style, section):
    """G3 for a planned card slide: {card container: the chosen pictogram in the place of the sample one}, or None when
    the picker gave the section no pictograms or a kept card has no place for one (then the cards keep the sample's)."""
    carrier = style.get("carrier") or {}
    choice = carrier.get("icon_choice") or {}
    picks = choice.get("picks", {}).get(section)
    if not picks or not places_icons(sample, plan, style, section):
        return None
    catalogue = {i["sha256"]: i for i in carrier.get("icons", [])}
    cards = {c["container"]: c for c in sample["cards"]}
    nodes = {n["id"]: n for n in sample["nodes"]}
    chosen = {}
    for kept in plan["cards"]:
        slot = slot_of(cards[kept["container"]], choice.get("tags", {}))
        icon = picks.get(kept["fact"])
        if slot is None or icon is None:
            return None
        box = next(p["box"] for p in nodes[slot["member"]]["primitives"] if p["type"] == "image" and p["asset"] == slot["asset"])
        ink = catalogue[slot["asset"]]["ink"]
        chosen[kept["container"]] = {"asset": slot["asset"], "icon": icon, "fact": kept["fact"], "meaning": choice["tags"].get(icon, {}).get("meaning", ""),
                                     "ink": ink, "data": recoloured(carrier["icons"], icon, ink, box[2] / box[3])}
    return chosen
