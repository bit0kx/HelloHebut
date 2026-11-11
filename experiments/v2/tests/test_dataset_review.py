from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

from experiments.v2.scripts.review_dataset import load_cases, validate_cases, freeze_cases

ROOT=Path(__file__).resolve().parents[3]


class ReviewDatasetTests(unittest.TestCase):
    def test_thirty_drafts_are_development_tasks_and_never_independent_truth(self):
        cases=load_cases(ROOT/'experiments/v2/datasets/human_review_template.jsonl')
        self.assertEqual(len(cases),30)
        self.assertEqual(len({c['family'] for c in cases}),10)
        self.assertTrue(validate_cases(cases,'development')['passed'])
        result=validate_cases(cases,'independent')
        self.assertFalse(result['passed'])
        self.assertFalse(result['eligible_for_independent_acceptance'])
        self.assertTrue(all(not c['review']['reviewer'] and not c['review']['verified_references'] for c in cases))

    def approved_fixture(self,root):
        path=root/'data/fixture.txt'
        path.parent.mkdir()
        path.write_text('Synthetic product requirement for validator tests only.',encoding='utf-8')
        case=deepcopy(load_cases(ROOT/'experiments/v2/datasets/human_review_template.jsonl')[0])
        case['id']='synthetic-validator-test'
        case['provenance']['case_origin']='independent_human'
        case['review_status']='approved'
        case['expected']={'response_kind':'clarify','must_include':[],'must_not_include':[],
                          'required_tools':[],'assertions':['Ask for missing scope.']}
        case['review'].update({'reviewer':'test-only-fixture','reviewed_at':'2026-11-11T10:00:00+00:00',
            'human_verified':True,'independent_from_implementation':True,'used_for_tuning':False,
            'verified_references':[{'reference_id':'fixture-1','kind':'synthetic_fixture','path':'data/fixture.txt',
            'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'locator':'whole synthetic fixture',
            'verified':True,'verified_by':'test-only-fixture','verified_at':'2026-11-11T10:00:00+00:00'}]})
        return case

    def test_frozen_identity_is_stable_and_mutation_is_not_overwritten(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'experiments/v2/tmp') as directory:
            root=Path(directory)
            case=self.approved_fixture(root)
            path,payload=freeze_cases([case],'independent',root/'experiments/v2/datasets/frozen',root)
            self.assertTrue(payload['eligible_for_independent_acceptance'])
            self.assertEqual(freeze_cases([case],'independent',path.parent,root)[0],path)
            path.write_text('tampered',encoding='utf-8')
            with self.assertRaises(ValueError):
                freeze_cases([case],'independent',path.parent,root)

    def test_unverified_reference_contamination_and_changed_snapshot_block_independent_freeze(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'experiments/v2/tmp') as directory:
            root=Path(directory)
            case=self.approved_fixture(root)
            self.assertTrue(validate_cases([case],'independent',root)['passed'])
            for mutation in ('unverified','contaminated','missing_reviewer','snapshot','outside'):
                changed=deepcopy(case)
                if mutation=='unverified':
                    changed['review']['verified_references'][0]['verified']=False
                elif mutation=='contaminated':
                    changed['review']['used_for_tuning']=True
                elif mutation=='missing_reviewer':
                    changed['review']['reviewer']=''
                elif mutation=='outside':
                    changed['review']['verified_references'][0]['path']='../../.env'
                else:
                    changed['review']['verified_references'][0]['sha256']='0'*64
                with self.subTest(mutation=mutation):
                    self.assertFalse(validate_cases([changed],'independent',root)['passed'])
                    with self.assertRaises(ValueError):
                        freeze_cases([changed],'independent',root/'experiments/v2/datasets/frozen',root)
