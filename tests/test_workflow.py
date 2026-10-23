import unittest
from pathlib import Path


class WorkflowTests(unittest.TestCase):
    def test_versioned_inputs_and_all_core_gates_before_upload_and_publish(self):
        workflow = (Path(__file__).parents[1] / '.github/workflows/extract-pvzge-tauri.yml').read_text()
        self.assertEqual(workflow.count('workflow_dispatch:'), 1)
        self.assertIn("run-name: \"${{ inputs.release_tag || '最新' }} · ${{ inputs.publish && '发布' || '测试' }}\"", workflow)
        inputs = workflow.split('inputs:', 1)[1].split('permissions:', 1)[0]
        self.assertIn('release_tag:', inputs)
        self.assertIn('required: false', inputs)
        self.assertIn("--jq '.[0].tagName // empty'", workflow.split('- name: Download and unpack PvZGE Lite', 1)[1])
        self.assertIn('publish:', inputs)
        self.assertIn('default: false', inputs)
        self.assertIn('contents: write', workflow)
        self.assertNotIn('image_transcode_format', workflow)
        self.assertNotIn('enable_hybrid_audio_loading', workflow)
        self.assertNotIn('ffprobe', workflow)
        self.assertNotIn('rm -', workflow)
        steps = ['Test core pipeline', 'Patch verified Cocos audio runtime', 'Transcode native textures to ASTC', 'Download base resource Release', 'Assemble ASTC package and verify sealed ZIP', 'Upload verified resource package', 'Publish resource Release']
        self.assertEqual([workflow.index(step) for step in steps], sorted(workflow.index(step) for step in steps))
        self.assertIn('python3 -m unittest discover -s tests -t . -v', workflow)
        self.assertIn('mktemp -d', workflow)
        self.assertIn('--producer-commit "$GITHUB_SHA"', workflow)
        self.assertIn('${BASE_ZIP:+--base "$BASE_ZIP"}', workflow)
        self.assertIn('PACKAGE_TAG="$GAME_VERSION+${PACKAGE_ID:7:8}"', workflow)
        self.assertIn('GardendlessResources-$PACKAGE_TAG-from-$BASE_TAG.zip', workflow)
        publish = workflow.split('- name: Publish resource Release', 1)[1]
        self.assertIn('if: inputs.publish', publish)
        self.assertLess(publish.index('gh release view "$PACKAGE_TAG"'), publish.index('gh release create "$PACKAGE_TAG"'))
        self.assertNotIn('            docs/', workflow)
