import unicodedata
from .checks import fields,array,ident,unique

def text(value,empty=False):
    if not isinstance(value,str) or not (0 if empty else 1)<=len(value)<=64 or any(unicodedata.category(c) in ('Cc','Cs') for c in value):raise ValueError('bounded Unicode text required')
    return unicodedata.normalize('NFC',value)

def features(value):
    if not isinstance(value,dict) or len(value)>20:raise ValueError('feature mapping required')
    return {ident(k):ident(v) for k,v in value.items()}

def parse(data):
    fields(data,('states','accepting','rules','lemmas','queries'))
    states=[ident(v) for v in array(data['states'],1,50)];unique(states);accepting=[ident(v) for v in array(data['accepting'],1,50)];unique(accepting)
    if not set(accepting)<=set(states):raise ValueError('unknown accepting state')
    rules=[]
    for r in array(data['rules'],0,300):
        fields(r,('id','from','to','prefix','suffix','requires','adds'))
        rule={'id':ident(r['id']),'from':ident(r['from']),'to':ident(r['to']),'prefix':text(r['prefix'],True),'suffix':text(r['suffix'],True),'requires':features(r['requires']),'adds':features(r['adds'])}
        if rule['from'] not in states or rule['to'] not in states:raise ValueError('unknown rule state')
        rules.append(rule)
    unique([r['id'] for r in rules]);outgoing={s:[] for s in states};indegree={s:0 for s in states}
    for r in rules:outgoing[r['from']].append(r);indegree[r['to']]+=1
    pending=[s for s in states if indegree[s]==0];visited=0
    while pending:
        state=pending.pop();visited+=1
        for r in outgoing[state]:
            indegree[r['to']]-=1
            if indegree[r['to']]==0:pending.append(r['to'])
    if visited!=len(states):raise ValueError('cyclic rule graph')
    lemmas=[]
    for l in array(data['lemmas'],1,200):
        fields(l,('id','stem','state','features'));state=ident(l['state'])
        if state not in states:raise ValueError('unknown lemma state')
        lemmas.append({'id':ident(l['id']),'stem':text(l['stem']),'state':state,'features':features(l['features'])})
    unique([l['id'] for l in lemmas]);queries=[]
    for q in array(data['queries'],0,500):
        fields(q,('id','surface'));queries.append((ident(q['id']),text(q['surface'])))
    unique([q[0] for q in queries]);return set(accepting),outgoing,lemmas,queries
