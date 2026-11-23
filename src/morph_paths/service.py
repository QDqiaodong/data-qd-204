import unicodedata
from .model import parse
from .compile import compile_forms

def process(data):
    accepting,outgoing,lemmas,queries=parse(data);forms=compile_forms(accepting,outgoing,lemmas);index={}
    for form in forms:
        # Prefix/stem/suffix joins may create a combining sequence across a boundary.
        form['surface']=unicodedata.normalize('NFC',form['surface'])
    forms.sort(key=lambda f:(f['surface'],f['lemma'],f['rules']))
    for form in forms:index.setdefault(form['surface'],[]).append(form)
    results=[{'id':key,'surface':surface,'analyses':index.get(surface,[])} for key,surface in queries]
    return {'forms':forms,'queries':results,'summary':{'lemmas':len(lemmas),'derivations':len(forms),'distinct_surfaces':len(index),'queries':len(queries),'ambiguous_queries':sum(len(q['analyses'])>1 for q in results)}}
