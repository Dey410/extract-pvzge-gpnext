import unittest
from pathlib import Path


WORKFLOW_PATH = (
    Path(__file__).parents[1]
    / ".github"
    / "workflows"
    / "extract-pvzge-tauri.yml"
)


class HybridAudioWorkflowTests(unittest.TestCase):
    def test_provisions_ffprobe_before_running_the_hybrid_audio_patch(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        provision_step = workflow.index("Ensure ffprobe is available")
        patch_step = workflow.index("Patch runtime for hybrid audio loading")

        self.assertLess(provision_step, patch_step)
        self.assertIn("command -v ffprobe", workflow[provision_step:patch_step])
        self.assertIn("apt-get install", workflow[provision_step:patch_step])
        self.assertIn("ffprobe -version", workflow[provision_step:patch_step])


if __name__ == "__main__":
    unittest.main()
