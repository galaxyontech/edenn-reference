import unittest

from EdennCode.TestSuites.suites import build_suite


PRODUCTION_MODULES = [
    "EdennCode.WorkflowFactory.AudioCreativeEditWorkflow.Testing.test_audio_creative_edit_workflow",
    "EdennCode.WorkflowFactory.VideoMusicWorkflow.Testing.test_video_music_workflow_e2e",
    "EdennCode.WorkflowFactory.VideoMusicWorkflow.Testing.video_music_e2e.test_video_music_e2e_output_fields",
    "EdennCode.WorkflowFactory.VideoMusicWorkflow.Testing.video_music_e2e.test_edenn_flow_e2e_latency",
]


def load_tests(
    loader: unittest.TestLoader,
    tests: unittest.TestSuite,
    pattern: str | None,
) -> unittest.TestSuite:
    return build_suite(loader, PRODUCTION_MODULES)
