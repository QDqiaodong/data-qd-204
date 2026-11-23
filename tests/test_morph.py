import copy,json,unittest
from pathlib import Path
from morph_paths import process

class MorphTests(unittest.TestCase):
    def setUp(self):self.d=json.loads(Path('fixtures/input.json').read_text())
    def test_feature_dependency_and_inverse(self):
        r=process(self.d);self.assertEqual(r['summary']['derivations'],6)
        q=r['queries'];self.assertEqual([(a['lemma'],a['rules']) for a in q[0]['analyses']],[('a',['plural']),('b',['singular'])])
        self.assertEqual(q[1]['analyses'],[]);self.assertEqual(q[2]['analyses'][0]['features']['size'],'small')
    def test_changed_fact_blocks_rule(self):
        self.d['lemmas'][0]['features']['number']='many';r=process(self.d)
        self.assertFalse(any(f['lemma']=='a' and 'singular' in f['rules'] for f in r['forms']))
    def test_distinct_paths_not_deduplicated(self):
        alternate=copy.deepcopy(self.d['rules'][0]);alternate['id']='other';self.d['rules'].append(alternate)
        q=process(self.d)['queries'][0];self.assertEqual(len(q['analyses']),3)
    def test_nfc_boundary_composition(self):
        self.d['lemmas']=self.d['lemmas'][:1];self.d['lemmas'][0]['stem']='e'
        self.d['rules'][1]['suffix']='\u0301';self.d['queries']=[{'id':'q','surface':'e\u0301'}]
        self.assertEqual(process(self.d)['queries'][0]['analyses'][0]['surface'],'é')
    def test_unused_cycle_rejected(self):
        self.d['states'].append('unused');r=copy.deepcopy(self.d['rules'][0]);r.update(id='loop',**{'from':'unused','to':'unused'});self.d['rules'].append(r)
        with self.assertRaisesRegex(ValueError,'cyclic'):process(self.d)
    def test_unknown_state_and_oversize_word(self):
        self.d['rules'][0]['to']='absent'
        with self.assertRaises(ValueError):process(self.d)
        self.setUp();self.d['lemmas'][0]['stem']='x'*64
        with self.assertRaisesRegex(ValueError,'surface'):process(self.d)
