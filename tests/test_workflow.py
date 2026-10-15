import unittest
from pathlib import Path


class WorkflowTests(unittest.TestCase):
    def test_one_input_and_all_core_gates_before_formal_upload(self):
        workflow = (Path(__file__).parents[1] / '.github/workflows/extract-pvzge-tauri.yml').read_text()
        self.assertEqual(workflow.count('workflow_dispatch:'), 1)
        inputs = workflow.split('inputs:', 1)[1].split('permissions:', 1)[0]
        self.assertIn('release_tag:', inputs)
        self.assertNotIn('image_transcode_format', workflow)
        self.assertNotIn('enable_hybrid_audio_loading', workflow)
        self.assertNotIn('ffprobe', workflow)
        self.assertNotIn('rm -', workflow)
        steps = ['Test core pipeline', 'Patch verified Cocos audio runtime', 'Transcode native textures to ASTC', 'Assemble ASTC package and verify sealed ZIP', 'Upload verified resource package']
        self.assertEqual([workflow.index(step) for step in steps], sorted(workflow.index(step) for step in steps))
        self.assertIn('python3 -m unittest discover -s tests -t . -v', workflow)
        self.assertIn('mktemp -d', workflow)
        self.assertIn('--producer-commit "$GITHUB_SHA"', workflow)
        self.assertIn('package/GardendlessResources.zip', workflow)
        self.assertNotIn('            docs/', workflow)
