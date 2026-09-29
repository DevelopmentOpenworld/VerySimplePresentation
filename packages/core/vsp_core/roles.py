"""Explainable text-region classification. No model or brand-specific rules."""
import re

FILLER=re.compile(r'^\W*(?:(?:текст|text)\W+)+(?:текст|text)\W*$')
# Session 16 (26.09.2026), cause 1 of the M7 review of holdout-next3: deck 20 opens with "Как работать с шаблоном презентации
# ... удалите этот слайд-инструкцию", deck 28 with "How to use this template"; the real cover follows, and the instruction slide
# was the sample of our cover. A short line (a title or a heading) that names the slide so marks it; a note box on a real cover
# does not (deck 15: "Delete this text box prior to submitting your pitch deck").
INSTRUCTION_LINE=re.compile(r'\bhow to use (?:this|the)\b.{0,60}?\btemplate\b|\bкак (?:работать|пользоваться) (?:с )?(?:этим |данным )?шаблон'
                            r'|\b(?:delete|remove) this slide\b|\bудалите (?:этот |данный )?слайд',re.I)
INSTRUCTION_LINE_MAX=100


def leading_instructions(lines_by_slide):
    """Numbers (from 1) of the slides of instructions for the author at the start of the sample: each has a line of at most
    INSTRUCTION_LINE_MAX characters that INSTRUCTION_LINE finds. A slide must follow them; a sample of instructions alone
    keeps its slides."""
    count=0
    for lines in lines_by_slide:
        if not any(len(line)<=INSTRUCTION_LINE_MAX and INSTRUCTION_LINE.search(line) for line in lines):break
        count+=1
    return list(range(1,count+1)) if count<len(lines_by_slide) else []


def infer_role(frame, width, height, visuals):
    text=' '.join(frame.get('source_text','').lower().split())
    x,y,w,h=frame['box'];ph=frame.get('placeholder')
    def result(role,*evidence):return {'role':role,'evidence':list(evidence),'method':'rules-v1'}
    if ph and ph[0] in ('sldNum','ftr','dt'):
        return result('service','placeholder:'+ph[0])
    if y>height*.88:
        return result('service','bottom-margin')
    if ph and ph[0]=='pic':return result('caption','picture-placeholder')
    if (len(text)<180 or h<height*.12) and re.search(r'https?://|www\.|\S+@\S+|(?:телефон|e-mail|email|должность|имя фамилия)',text):
        return result('service','contact-or-profile-label')
    if any(word in frame['font'].lower() for word in ('consolas','courier','mono')) or re.search(r'\b(?:def |class |import |function\s*\(|select .+ from )',text):
        return result('code','monospace-or-code-syntax')
    if re.search(r'подпись|под (?:тематической )?иконкой|caption|insert (?:photo|image)|вставить фото',text):
        return result('caption','explicit-visual-label')
    for visual in visuals:
        vx,vy,vw,vh=visual
        shared=max(0,min(x+w,vx+vw)-max(x,vx))
        if 0<=y-(vy+vh)<=height*.12 and shared>=min(w,vw)*.6 and vw*vh>=width*height*.004 and h<height*.28:
            return result('caption','directly-below-source-visual')
    if re.fullmatch(r'[\d\s.,+%\-–]+(?:млн|тыс|million|млрд)?',text) and frame['font_size']>=30:
        return result('metric','large-number')
    if re.fullmatch(r'(?:заголовок|heading|title)(?:\s+\d+)?',text):
        return result('label','heading-label')
    if re.fullmatch(r'(?:заметка|примечание|note|источник|source).*',text) and len(text)<90:
        return result('note','note-label')
    if ph and ph[0] in ('body','obj','subTitle'):
        return result('body','content-placeholder')
    if text in ('текст','text','текстовый блок','body text'):
        return result('body','body-label')
    # Item 37, cause 3 (holdout-next 25.09.2026, deck 19): filler that only repeats the word ("Текст текст текст…" in five
    # lines of a 93 px frame) marks the body as the single word does; left ambiguous, the only content sample slide of the
    # template held no fact, and 26 of 36 slides went to plain white fallback layouts.
    if FILLER.match(text):
        return result('body','filler-text')
    if frame.get('body_sample'):
        return result('body','paragraph-with-body-style')
    if h<height*.065 and len(text)<55:
        return result('label','short-shallow-region')
    if len(text)>=60 or h>=height*.18 and w>=width*.23:
        return result('body','prose-or-large-text-region')
    return result('ambiguous','insufficient-role-evidence')


def infer_pattern_intent(title):
    text=title.get('source_text','').lower()
    if re.search(r'имя фамилия|speaker|контакт|contact|спасибо|thank you',text):return 'profile_or_closing'
    if re.search(r'скриншот|screenshot|схем|diagram|диаграмм|chart|логотип|logo|иконк|illustration|цитат|quote',text):return 'visual_dependent'
    if re.search(r'\bкод\b|\bcode\b',text):return 'code'
    return 'text'
