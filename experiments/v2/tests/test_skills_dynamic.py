from pathlib import Path
import tempfile
import unittest

from core.skill_loader import SkillManager

ROOT=Path(__file__).resolve().parents[3]


class DynamicSkillsTests(unittest.TestCase):
    def test_builtin_roles_and_task_skills_are_versioned_and_load_without_errors(self):
        manager=SkillManager(str(ROOT/'skills'))
        manager.load()
        self.assertEqual(manager.errors,[])
        self.assertEqual(len(manager.skills),7)
        self.assertTrue(all(s.version and s.content_hash for s in manager.skills))
        self.assertIn('admission-risk', {s.name for s in manager.active_skills('你好','risk')})

    def test_body_and_reference_are_disclosed_only_when_matched(self):
        manager=SkillManager(str(ROOT/'skills'))
        manager.load()
        plain=manager.prompt_for('你好','policy')
        self.assertIn('policy-comparison',plain)  # discovery metadata only
        self.assertNotIn('年份×专业×适用对象',plain)
        active=manager.prompt_for('比较两个年份政策','policy')
        self.assertIn('年份×专业×适用对象',active)
        self.assertNotIn('同名专业也要核对学院',active)
        reference=manager.read_reference('policy-comparison','references/comparison-checklist.md',
            message='比较两个年份政策',agent_type='policy')
        self.assertIn('同名专业也要核对学院',reference['content'])
        with self.assertRaises(ValueError):
            manager.read_reference('policy-comparison','references/comparison-checklist.md',message='你好',agent_type='policy')

    def test_yaml_objects_aliases_duplicate_metadata_and_paths_are_rejected(self):
        cases=[
            'name: demo\ndescription: test\nmetadata: !!python/object:object {}',
            'name: demo\nname: other\ndescription: test\nmetadata:\n  version: "1.0.0"',
            'name: demo\ndescription: &a test\nmetadata:\n  version: *a',
            'name: demo\ndescription: test\nmetadata:\n  version: "1.0.0"\n  references: "references/../../secret"',
            'name: wrong\ndescription: test\nmetadata:\n  version: "1.0.0"',
        ]
        for header in cases:
            with self.subTest(header=header), tempfile.TemporaryDirectory(dir=ROOT/'experiments/v2/tmp') as directory:
                path=Path(directory)/'demo'
                path.mkdir()
                (path/'SKILL.md').write_text('---\n'+header+'\n---\nBody',encoding='utf-8')
                manager=SkillManager(directory)
                self.assertEqual(manager.load(),[])
                self.assertTrue(manager.errors)

    def test_reference_resources_are_not_discovered_as_skills(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'experiments/v2/tmp') as directory:
            path=Path(directory)/'demo'
            (path/'references').mkdir(parents=True)
            (path/'SKILL.md').write_text('---\nname: demo\ndescription: Test\nmetadata:\n  version: "1.0.0"\n---\nBody',encoding='utf-8')
            (path/'references'/'notes.md').write_text('untrusted notes',encoding='utf-8')
            (path/'references'/'SKILL.md').write_text('not an independently trusted skill',encoding='utf-8')
            manager=SkillManager(directory)
            self.assertEqual(len(manager.load()),1)
            self.assertEqual(manager.errors,[])
            with self.assertRaises(ValueError):
                manager.read_reference('demo','references/notes.md',message='hello',agent_type='general')

    def test_prompt_budget_omits_whole_skill_not_partial_instruction(self):
        manager=SkillManager(str(ROOT/'skills'),max_prompt_chars=350)
        manager.load()
        text=manager.prompt_for('比较政策','policy')
        self.assertLessEqual(len(text),350)
        self.assertNotIn('\n...',text)
