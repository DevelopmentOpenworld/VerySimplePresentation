"""Native charts (stage G1 of docs/SOLUTION_PLAN_2026-09-21.md; docs/CHARTS.md).

A chart shows the numbers of facts; it does not replace them: the facts keep their exact text on the slide. A text
section of vsp.brief/1 may carry `chart`: kind (bar, line, pie), unit, label of the value axis, categories, series
and the facts it cites. check_chart accepts it only when every value and every number of its labels is a number of the
cited facts and the unit is written in them, so a chart cannot bring a number the brief does not have.

The document element of type "chart" repeats this data (model.audit_document compares it with the brief: a changed
chart stops the output like a changed fact) and carries what the composer chose from the template: typeface, sizes,
text colour and the theme colours of the series (scheme names for the PPTX, RGB for the preview).

PPTX: a graphic frame on the slide, ppt/charts/chartN.xml with explicit axis scale (the preview draws the same ticks)
and ppt/embeddings/*.xlsx built with the standard library, so "Edit data" in PowerPoint opens the numbers.
"""
import io
import math
import re
import zipfile
from xml.sax.saxutils import escape, quoteattr

from .opc import OFFICE_REL

KINDS = ("bar", "line", "pie")
FIELDS = ("kind", "unit", "value_label", "categories", "series", "fact_ids")
MAX_CATEGORIES, MAX_SERIES, MAX_LABEL, MAX_UNIT, MAX_VALUE_LABEL = 8, 8, 40, 20, 60
# Appendix 1: more than 5 series is a warning of the audit (CHART_TOO_MANY_SERIES), not a refusal.
SERIES_WARNING = 5
# Data labels repeat a short unit ("31 %", "12 мин"); a longer one ("млн рублей") stays in the title of the value axis.
LABEL_UNIT_MAX = 4
CHART_URI = "http://schemas.openxmlformats.org/drawingml/2006/chart"
CHART_TYPE = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CHART_REL = OFFICE_REL + "/chart"
PACKAGE_REL = OFFICE_REL + "/package"
SHEET = "Лист1"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
AXES = ("500001", "500002")


# ---- data -------------------------------------------------------------------------------------------------------------
def number_key(value):
    """A value as planner.numbers normalizes numbers of text: no thousands separator, a point before decimals."""
    return ("%.6f" % value).rstrip("0").rstrip(".")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _text(value, limit):
    return isinstance(value, str) and 1 <= len(value.strip()) and len(value) <= limit


def check_chart(chart, section):
    """Violations of the chart of a text section; [] when it may be drawn.

    Every value is a number of the facts the chart cites, every cited fact gives at least one value, numbers of the
    labels come from those facts too, and the unit is written in them. Values are not negative in version 1: a sign is
    not a number of the text, so a decline stays a smaller bar, not a negative one.
    """
    from .planner import numbers
    def bad(reason, **extra):
        return {"code": "CHART_DATA_INVALID", "reason": reason, **extra}
    if not isinstance(chart, dict) or set(chart) != set(FIELDS):
        return [bad("fields")]
    if section.get("kind") != "text":
        return [bad("section-not-text")]
    kind, unit, label, categories, series, cited = (chart[k] for k in FIELDS)
    violations = []
    if kind not in KINDS:
        violations.append(bad("kind"))
    if not _text(unit, MAX_UNIT) or not _text(label, MAX_VALUE_LABEL):
        violations.append(bad("unit-or-label"))
    if not (isinstance(categories, list) and 1 <= len(categories) <= MAX_CATEGORIES and all(_text(c, MAX_LABEL) for c in categories)
            and len(set(categories)) == len(categories)):
        violations.append(bad("categories"))
    if not (isinstance(series, list) and 1 <= len(series) <= MAX_SERIES and all(isinstance(s, dict) and set(s) == {"name", "values"} for s in series)):
        return violations + [bad("series")]
    if not all(_text(s["name"], MAX_LABEL) for s in series) or len({s["name"] for s in series}) != len(series):
        violations.append(bad("series-names"))
    if not all(isinstance(s["values"], list) and isinstance(categories, list) and len(s["values"]) == len(categories)
               and all(_number(v) and v >= 0 for v in s["values"]) for s in series):
        return violations + [bad("values")]
    facts = {f["id"]: f for f in section["facts"]}
    if not (isinstance(cited, list) and cited and len(set(cited)) == len(cited) and all(i in facts for i in cited)):
        return violations + [bad("fact_ids")]
    if violations:
        return violations
    values = [v for s in series for v in s["values"]]
    if len(values) < 2:
        violations.append(bad("points"))
    if kind == "line" and len(categories) < 3:
        violations.append(bad("line-needs-three-points"))
    if kind == "pie":
        if len(series) != 1 or len(categories) < 2 or min(values) <= 0:
            violations.append(bad("pie-shape"))
        elif unit.strip() == "%" and abs(sum(values) - 100) > 1:
            violations.append(bad("pie-percent-sum", sum=round(sum(values), 3)))
    texts = [facts[i]["text"] for i in cited]
    source = set(numbers(" ".join(texts)))
    foreign = sorted({number_key(v) for v in values} - source)
    if foreign:
        violations.append(bad("value-not-in-facts", numbers=foreign))
    for i, text in zip(cited, texts):
        if not {number_key(v) for v in values} & set(numbers(text)):
            violations.append(bad("fact-without-value", fact=i))
    labels = [unit, label, *categories, *(s["name"] for s in series)]
    foreign = sorted(set(numbers(" ".join(labels))) - source)
    if foreign:
        violations.append(bad("label-number-not-in-facts", numbers=foreign))
    # A unit of letters starts a word of the facts ("смен" in "смену", not "м" inside any word); "%" may follow a digit.
    # Session 15 (run L2): a word of 4+ letters may differ from the word of the facts by its ending — "смена" or "смены" for
    # "за 1 смену вместо 3" was refused 4 times of 4 in runs L and L2, a correct chart each time; "сек" for minutes still is.
    if not re.search(unit_pattern(unit), " ".join(texts).lower()):
        violations.append(bad("unit-not-in-facts"))
    return violations


UNIT_ENDINGS = "аяоеёьйыиую"


def unit_pattern(unit):
    """Words of a unit in order: each starts a word of the facts, a word of 4+ letters without its last vowel or soft sign."""
    parts = []
    for word in unit.strip().lower().split():
        if word[:1].isalpha():
            stem = word[:-1] if len(word) >= 4 and word[-1] in UNIT_ENDINGS else word
            parts.append(r"(?<![^\W\d_])" + re.escape(stem) + r"[^\W\d_]*")
        else:
            parts.append(re.escape(word))
    return r"\s+".join(parts)


# ---- G2: key numbers and steps ------------------------------------------------------------------------------------------
METRIC_MAX = 24


def value_pattern(value):
    """Session 16 (26.09.2026, MB of analysis/style-experiments/20260926-hn3-m7/process/research-charts-time.md): a key number
    is a piece of its fact up to the ending of a word of its unit ("22 минуты" of "с 35 до 22 минут", as the unit of a chart
    since session 15); the number itself is verbatim and starts no later than a number of the fact starts ("9 %" is no piece
    of "0,9 %", "2 недели" none of "12 недель")."""
    parts = []
    for n, token in enumerate(value.strip().split()):
        if re.fullmatch(r"[^\W\d_]+", token):
            parts.append(unit_pattern(token))
        else:
            parts.append((r"(?<![\w,.])" if n == 0 and token[:1].isdigit() else "") + re.escape(token.lower()))
    return r"\s+".join(parts)


def check_metrics(metrics, section):
    """Violations of the key numbers of a text section: one per fact in the order of the facts, each a verbatim piece of
    its fact with a digit, at most METRIC_MAX characters; 2 to 4 facts."""
    def bad(reason, **extra):
        return {"code": "METRIC_INVALID", "reason": reason, **extra}
    if section.get("kind") != "text":
        return [bad("section-not-text")]
    facts = section["facts"]
    if not (isinstance(metrics, list) and all(isinstance(m, dict) and set(m) == {"fact_id", "value"} for m in metrics)
            and [m["fact_id"] for m in metrics] == [f["id"] for f in facts]):
        return [bad("items-do-not-match-facts")]
    if not 2 <= len(facts) <= 4:
        return [bad("facts-count")]
    violations = []
    for m, f in zip(metrics, facts):
        value = m["value"]
        if not _text(value, METRIC_MAX) or value != value.strip():
            violations.append(bad("value", fact=f["id"]))
        elif not re.search(value_pattern(value), f["text"].lower()):
            violations.append(bad("value-not-in-fact", fact=f["id"]))
        elif not re.search(r"[0-9]", value):
            violations.append(bad("value-without-number", fact=f["id"]))
    return violations


def check_process(process, section):
    """Violations of a process flag: a text section of 3 or 4 facts, its steps in the order of the facts."""
    if process is not True:
        return [{"code": "METRIC_INVALID", "reason": "process-flag"}]
    if section.get("kind") != "text" or not 3 <= len(section["facts"]) <= 4:
        return [{"code": "METRIC_INVALID", "reason": "process-steps"}]
    return []


def decimals(values):
    return max((len(number_key(v).partition(".")[2]) for v in values), default=0)


def label_unit(unit):
    unit = unit.strip()
    return unit if len(unit) <= LABEL_UNIT_MAX else ""


def format_code(values, unit=""):
    """Number format of the data labels (with a short unit) and of the axis (without): grouping and decimals of the locale."""
    places = decimals(values)
    code = "#,##0" + ("." + "0" * places if places else "")
    unit = label_unit(unit)
    return code + ('" ' + unit.replace('"', "") + '"' if unit else "")


def scale(values):
    """Value axis from zero with a round step, room above the highest value for its label: (maximum, step)."""
    top = max(values) or 1
    raw = top / 5
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(k * magnitude for k in (1, 2, 2.5, 5, 10) if k * magnitude >= raw - 1e-12)
    maximum = math.ceil(top / step - 1e-9) * step
    if top > maximum - step * .15:
        maximum += step
    return round(maximum, 10), round(step, 10)


# ---- chart part --------------------------------------------------------------------------------------------------------
def _run_props(e, size_pt, bold=False, tag="a:defRPr"):
    """Typeface, size and colour of chart text: a:defRPr of a text body, or a:rPr of a run (which also names the language)."""
    font = quoteattr(e["font"])
    lang = f' lang="{e.get("lang", "ru-RU")}"' if tag == "a:rPr" else ""
    return (f'<{tag}{lang} sz="{round(size_pt*100)}" b="{int(bold)}"><a:solidFill><a:srgbClr val="{e["color"]}"/></a:solidFill>'
            f'<a:latin typeface={font}/><a:ea typeface={font}/><a:cs typeface={font}/></{tag}>')


def _txpr(e, size_pt, bold=False):
    return f'<c:txPr><a:bodyPr/><a:lstStyle/><a:p><a:pPr>{_run_props(e, size_pt, bold)}</a:pPr><a:endParaRPr lang="{e.get("lang", "ru-RU")}"/></a:p></c:txPr>'


def _fill(scheme, rgb):
    return f'<a:solidFill><a:schemeClr val="{scheme}"/></a:solidFill>' if scheme else f'<a:solidFill><a:srgbClr val="{rgb}"/></a:solidFill>'


def _column(n):
    return chr(ord("B") + n)


def _str_ref(ref, values):
    points = "".join(f'<c:pt idx="{i}"><c:v>{escape(v)}</c:v></c:pt>' for i, v in enumerate(values))
    return f'<c:strRef><c:f>{ref}</c:f><c:strCache><c:ptCount val="{len(values)}"/>{points}</c:strCache></c:strRef>'


def _num_ref(ref, values, code):
    points = "".join(f'<c:pt idx="{i}"><c:v>{number_key(v)}</c:v></c:pt>' for i, v in enumerate(values))
    return f'<c:numRef><c:f>{ref}</c:f><c:numCache><c:formatCode>{escape(code)}</c:formatCode><c:ptCount val="{len(values)}"/>{points}</c:numCache></c:numRef>'


def _labels(e, code, position, size_pt):
    return (f'<c:dLbls><c:numFmt formatCode={quoteattr(code)} sourceLinked="0"/><c:spPr><a:noFill/><a:ln><a:noFill/></a:ln></c:spPr>'
            f'{_txpr(e, size_pt)}<c:dLblPos val="{position}"/><c:showLegendKey val="0"/><c:showVal val="1"/><c:showCatName val="0"/>'
            '<c:showSerName val="0"/><c:showPercent val="0"/><c:showBubbleSize val="0"/></c:dLbls>')


def _axes(e, values, size_pt):
    top, step = scale(values)
    alpha = e.get("grid_alpha", 1)
    grid = f'<a:srgbClr val="{e["grid_color"]}"><a:alpha val="{round(alpha*100000)}"/></a:srgbClr>' if alpha < 1 else f'<a:srgbClr val="{e["grid_color"]}"/>'
    line = f'<a:ln w="9525"><a:solidFill>{grid}</a:solidFill></a:ln>'
    title = (f'<c:title><c:tx><c:rich><a:bodyPr rot="-5400000" vert="horz"/><a:lstStyle/><a:p><a:pPr>{_run_props(e, size_pt)}</a:pPr>'
             f'<a:r>{_run_props(e, size_pt, tag="a:rPr")}<a:t>{escape(e["chart"]["value_label"])}</a:t></a:r></a:p></c:rich></c:tx><c:overlay val="0"/></c:title>')
    category = (f'<c:catAx><c:axId val="{AXES[0]}"/><c:scaling><c:orientation val="minMax"/></c:scaling><c:delete val="0"/><c:axPos val="b"/>'
                '<c:numFmt formatCode="General" sourceLinked="1"/><c:majorTickMark val="none"/><c:minorTickMark val="none"/><c:tickLblPos val="nextTo"/>'
                f'<c:spPr>{line}</c:spPr>{_txpr(e, size_pt)}<c:crossAx val="{AXES[1]}"/><c:crosses val="autoZero"/><c:auto val="1"/>'
                '<c:lblAlgn val="ctr"/><c:lblOffset val="100"/><c:noMultiLvlLbl val="0"/></c:catAx>')
    # Every bar and point carries its value, so the value axis shows the scale by its gridlines and its title with the
    # unit, without tick numbers: PowerPoint 2013 draws them in Arial when the typeface of the template is only embedded
    # in the file (Play of template A), while data labels, categories and titles keep the embedded typeface (docs/CHARTS.md,
    # journal item 10).
    value = (f'<c:valAx><c:axId val="{AXES[1]}"/><c:scaling><c:orientation val="minMax"/><c:max val="{number_key(top)}"/><c:min val="0"/></c:scaling>'
             f'<c:delete val="0"/><c:axPos val="l"/><c:majorGridlines><c:spPr>{line}</c:spPr></c:majorGridlines>{title}'
             f'<c:numFmt formatCode={quoteattr(format_code(values))} sourceLinked="0"/><c:majorTickMark val="none"/><c:minorTickMark val="none"/>'
             f'<c:tickLblPos val="none"/><c:spPr><a:ln><a:noFill/></a:ln></c:spPr>{_txpr(e, size_pt)}<c:crossAx val="{AXES[0]}"/>'
             f'<c:crosses val="autoZero"/><c:crossBetween val="between"/><c:majorUnit val="{number_key(step)}"/></c:valAx>')
    return category + value


def chart_xml(e, workbook_rid):
    """ppt/charts/chartN.xml of a chart element; `workbook_rid` is the relationship of the embedded workbook."""
    chart = e["chart"]
    kind, categories, series = chart["kind"], chart["categories"], chart["series"]
    values = [v for s in series for v in s["values"]]
    size = e["font_size"] * .75
    code = format_code(values, chart["unit"])
    general = format_code(values)
    categories_ref = f"{SHEET}!$A$2:$A${len(categories)+1}"
    parts = []
    for n, s in enumerate(series):
        name = _str_ref(f"{SHEET}!${_column(n)}$1", [s["name"]])
        cat = f"<c:cat>{_str_ref(categories_ref, categories)}</c:cat>"
        val = f'<c:val>{_num_ref(f"{SHEET}!${_column(n)}$2:${_column(n)}${len(categories)+1}", s["values"], general)}</c:val>'
        head = f'<c:idx val="{n}"/><c:order val="{n}"/><c:tx>{name}</c:tx>'
        scheme = (e.get("scheme_colors") or [None]*8)
        if kind == "bar":
            parts.append(f'<c:ser>{head}<c:spPr>{_fill(scheme[n], e["series_colors"][n])}</c:spPr><c:invertIfNegative val="0"/>'
                         f'{_labels(e, code, "outEnd", size)}{cat}{val}</c:ser>')
        elif kind == "line":
            fill = _fill(scheme[n], e["series_colors"][n])
            parts.append(f'<c:ser>{head}<c:spPr><a:ln w="28575" cap="rnd">{fill}<a:round/></a:ln></c:spPr>'
                         f'<c:marker><c:symbol val="circle"/><c:size val="7"/><c:spPr>{fill}<a:ln w="9525">{fill}</a:ln></c:spPr></c:marker>'
                         f'{_labels(e, code, "t", size)}{cat}{val}<c:smooth val="0"/></c:ser>')
        else:
            # Slices are separated by lines of the background colour; on a picture background (None) there are none.
            border = f'<a:ln w="19050"><a:solidFill><a:srgbClr val="{e["background"]}"/></a:solidFill></a:ln>' if e.get("background") else '<a:ln><a:noFill/></a:ln>'
            points = "".join(f'<c:dPt><c:idx val="{i}"/><c:bubble3D val="0"/><c:spPr>{_fill(scheme[i], e["series_colors"][i])}{border}</c:spPr></c:dPt>'
                             for i in range(len(categories)))
            parts.append(f'<c:ser>{head}{points}{_labels(e, code, "outEnd", size)}{cat}{val}</c:ser>')
    if kind == "bar":
        plot = (f'<c:barChart><c:barDir val="col"/><c:grouping val="clustered"/><c:varyColors val="0"/>{"".join(parts)}'
                f'<c:gapWidth val="80"/><c:overlap val="-10"/><c:axId val="{AXES[0]}"/><c:axId val="{AXES[1]}"/></c:barChart>' + _axes(e, values, size))
    elif kind == "line":
        plot = (f'<c:lineChart><c:grouping val="standard"/><c:varyColors val="0"/>{"".join(parts)}<c:marker val="1"/>'
                f'<c:axId val="{AXES[0]}"/><c:axId val="{AXES[1]}"/></c:lineChart>' + _axes(e, values, size))
    else:
        plot = f'<c:pieChart><c:varyColors val="1"/>{"".join(parts)}<c:firstSliceAng val="0"/></c:pieChart>'
    legend = f'<c:legend><c:legendPos val="b"/><c:overlay val="0"/>{_txpr(e, size)}</c:legend>' if legend_shown(chart) else ""
    # A pie has no value axis: the label of the values with the unit is the title of the chart.
    title = (f'<c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:pPr>{_run_props(e, size)}</a:pPr><a:r>{_run_props(e, size, tag="a:rPr")}'
             f'<a:t>{escape(chart["value_label"])}</a:t></a:r></a:p></c:rich></c:tx><c:overlay val="0"/></c:title><c:autoTitleDeleted val="0"/>'
             if kind == "pie" else '<c:autoTitleDeleted val="1"/>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<c:chartSpace xmlns:c="{C}" xmlns:a="{A}" xmlns:r="{R}"><c:date1904 val="0"/><c:lang val="{e.get("lang", "ru-RU")}"/><c:roundedCorners val="0"/>'
            f'<c:chart>{title}<c:plotArea><c:layout/>{plot}<c:spPr><a:noFill/><a:ln><a:noFill/></a:ln></c:spPr></c:plotArea>'
            f'{legend}<c:plotVisOnly val="1"/><c:dispBlanksAs val="gap"/></c:chart><c:spPr><a:noFill/><a:ln><a:noFill/></a:ln></c:spPr>'
            f'{_txpr(e, size)}<c:externalData r:id="{workbook_rid}"><c:autoUpdate val="0"/></c:externalData></c:chartSpace>')


def legend_shown(chart):
    return chart["kind"] == "pie" or len(chart["series"]) > 1


def frame_xml(e, i, rid):
    """The graphic frame of a chart on a slide; `rid` is the relationship of the slide to the chart part."""
    box = f'<p:xfrm><a:off x="{round(e["x"]*9525)}" y="{round(e["y"]*9525)}"/><a:ext cx="{round(e["w"]*9525)}" cy="{round(e["h"]*9525)}"/></p:xfrm>'
    return (f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{i}" name={quoteattr(e["id"])}/><p:cNvGraphicFramePr><a:graphicFrameLocks noGrp="1"/></p:cNvGraphicFramePr>'
            f'<p:nvPr/></p:nvGraphicFramePr>{box}<a:graphic><a:graphicData uri="{CHART_URI}"><c:chart xmlns:c="{C}" r:id="{rid}"/></a:graphicData></a:graphic></p:graphicFrame>')


# ---- embedded workbook -------------------------------------------------------------------------------------------------
def workbook(chart):
    """xlsx of the chart data built with the standard library: series names in row 1, categories in column A."""
    strings = []
    def shared(value):
        if value not in strings:
            strings.append(value)
        return strings.index(value)
    rows = ['<row r="1">' + "".join(f'<c r="{_column(n)}1" t="s"><v>{shared(s["name"])}</v></c>' for n, s in enumerate(chart["series"])) + "</row>"]
    for i, category in enumerate(chart["categories"], 2):
        cells = f'<c r="A{i}" t="s"><v>{shared(category)}</v></c>' + "".join(f'<c r="{_column(n)}{i}"><v>{number_key(s["values"][i-2])}</v></c>' for n, s in enumerate(chart["series"]))
        rows.append(f'<row r="{i}">{cells}</row>')
    last = f"{_column(len(chart['series'])-1)}{len(chart['categories'])+1}"
    head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    sheet = (f'{head}<worksheet xmlns="{SML}" xmlns:r="{R}"><dimension ref="A1:{last}"/><cols><col min="1" max="1" width="36" customWidth="1"/>'
             f'<col min="2" max="{len(chart["series"])+1}" width="18" customWidth="1"/></cols><sheetData>{"".join(rows)}</sheetData></worksheet>')
    table = "".join(f'<si><t xml:space="preserve">{escape(s)}</t></si>' for s in strings)
    parts = {
        "[Content_Types].xml": f'{head}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>',
        "_rels/.rels": f'{head}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{OFFICE_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'{head}<workbook xmlns="{SML}" xmlns:r="{R}"><sheets><sheet name="{SHEET}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'{head}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{OFFICE_REL}/worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{OFFICE_REL}/styles" Target="styles.xml"/>'
            f'<Relationship Id="rId3" Type="{OFFICE_REL}/sharedStrings" Target="sharedStrings.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": sheet,
        "xl/styles.xml": f'{head}<styleSheet xmlns="{SML}"><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>',
        "xl/sharedStrings.xml": f'{head}<sst xmlns="{SML}" count="{len(strings)}" uniqueCount="{len(strings)}">{table}</sst>',
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts.items():
            # A fixed date keeps the bytes, and the checksums of a revision, the same from run to run.
            archive.writestr(zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)), text.encode("utf-8"), zipfile.ZIP_DEFLATED)
    return out.getvalue()


def write_chart(package, e, rel):
    """Adds the chart part of element `e` and its workbook to a carrier package; returns the relationship id of the slide.

    `rel(type, target)` adds a relationship of the slide and returns its id (carrier_export.export_carrier_pptx)."""
    from .opc import Rel
    n = 1
    while f"ppt/charts/chart{n}.xml" in package.parts or f"ppt/embeddings/Microsoft_Excel_Worksheet{n}.xlsx" in package.parts:
        n += 1
    part, book = f"ppt/charts/chart{n}.xml", f"ppt/embeddings/Microsoft_Excel_Worksheet{n}.xlsx"
    package.put(book, workbook(e["chart"]), XLSX_TYPE)
    package.put(part, chart_xml(e, "rId1"), CHART_TYPE)
    package.set_rels(part, [Rel("rId1", PACKAGE_REL, package.relative(part, book))])
    return rel(CHART_REL, part)
