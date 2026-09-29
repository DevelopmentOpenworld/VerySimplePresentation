"""Coarse composition fingerprints: moving an edge by a pixel is not diversity."""

def composition_signature(boxes,title,width,height,boxed=False):
    if not boxes:return ('no-body',)
    centers=[(x+w/2,y+h/2) for x,y,w,h in boxes]
    relations=[]
    for i,(x,y) in enumerate(centers):
        for j,(xx,yy) in enumerate(centers[i+1:],i+1):
            # Row membership uses top edges so merely changing heights cannot
            # turn a row into an artificial diagonal layout.
            if abs(boxes[i][1]-boxes[j][1])<height*.06:relations.append('row')
            elif abs(boxes[i][0]-boxes[j][0])<width*.06:relations.append('column')
            else:relations.append('diagonal')
    x0=min(b[0] for b in boxes);x1=max(b[0]+b[2] for b in boxes)
    region='right' if x0>width*.35 else 'left' if x1<width*.65 else 'full'
    title_position='side' if title[2]<width*.48 and title[1]+title[3]/2>min(b[1] for b in boxes) else 'above'
    return (len(boxes),tuple(relations),region,title_position,bool(boxed))

def document_signature(doc):
    result=[]
    for slide in doc['slides']:
        if slide['role']!='text':continue
        bodies=[e for e in slide['elements'] if e['type']=='text' and e.get('fact_ids')]
        title=next(e for e in slide['elements'] if e['id']=='title')
        result.append((slide['id'],composition_signature([[e[k] for k in ('x','y','w','h')] for e in bodies],
            [title[k] for k in ('x','y','w','h')],doc['width'],doc['height'],any(e['type']=='shape' for e in slide['elements']))))
    return tuple(result)

def audit_diversity(documents):
    groups={}
    for doc in documents:groups.setdefault(document_signature(doc),[]).append(doc['variant'])
    duplicates=[v for v in groups.values() if len(v)>1]
    return {'method':'composition-structure-v1','distinct_compositions':len(groups),'variants':len(documents),
        'duplicates':duplicates,'passed':len(documents)==3 and not duplicates,
        'scope':'Body grouping, relative placement and card grouping. Covers and mere height changes do not count. Not a style-similarity score.'}
