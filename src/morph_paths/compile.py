def compile_forms(accepting,outgoing,lemmas):
    forms=[];visited=0
    for lemma in sorted(lemmas,key=lambda l:l['id']):
        pending=[(lemma['state'],lemma['stem'],lemma['features'],[])]
        while pending:
            state,surface,features,path=pending.pop();visited+=1
            if visited>100000:raise ValueError('derivation visit limit exceeded')
            if state in accepting:
                forms.append({'lemma':lemma['id'],'surface':surface,'features':dict(sorted(features.items())),'rules':path})
                if len(forms)>20000:raise ValueError('accepted form limit exceeded')
            for rule in sorted(outgoing[state],key=lambda r:r['id'],reverse=True):
                if any(features.get(k)!=v for k,v in rule['requires'].items()):continue
                if any(k in features and features[k]!=v for k,v in rule['adds'].items()):continue
                word=rule['prefix']+surface+rule['suffix']
                if len(word)>64:raise ValueError('derived surface exceeds bound')
                combined={**features,**rule['adds']}
                if len(combined)>20:raise ValueError('derived features exceed bound')
                pending.append((rule['to'],word,combined,path+[rule['id']]))
    return sorted(forms,key=lambda f:(f['surface'],f['lemma'],f['rules']))
