import unittest

from EdennCode.TestSuites.suites import build_suite


SMOKE_MODULES = [
    "EdennCode.Deployment.Testing.test_api_common",
    "EdennCode.Deployment.Testing.test_api_audio_creative_edit",
    "EdennCode.Deployment.Testing.test_api_multi_image_generation",
    "EdennCode.Deployment.Testing.test_api_video_alignment",
    "EdennCode.Deployment.Testing.test_provider_c_callback",
    "EdennCode.Deployment.Testing.test_api_video_generation_compression",
    "EdennCode.MusicGenerationCore.Testing.test_provider_a_compose_payload",
    "EdennCode.MusicGenerationCore.Testing.test_models",
    "EdennCode.MusicGenerationCore.Testing.test_provider_branch_matrix",
    "EdennCode.MusicGenerationCore.Testing.test_service",
    "EdennCode.WorkflowFactory.MultiImageWorkflow.Testing.test_beat_alignment_stage",
    "EdennCode.WorkflowFactory.MultiImageWorkflow.Testing.test_image_sequence_planning_stage",
    "EdennCode.WorkflowFactory.MultiImageWorkflow.Testing.test_multi_image_generation_e2e_stage",
    "EdennCode.WorkflowFactory.MultiImageWorkflow.Testing.test_music_generation_stage",
    "EdennCode.WorkflowFactory.MultiImageWorkflow.Testing.test_preprocess_stage",
    "EdennCode.WorkflowFactory.MultiImageWorkflow.Testing.test_multi_image_workflow_surface",
    "EdennCode.WorkflowFactory.VideoAudioAlignmentWorkflow.Testing.test_audio_video_alignment",
    "EdennCode.WorkflowFactory.VideoMusicWorkflow.Testing.test_music_provider_routing",
    "EdennCode.WorkflowFactory.VideoMusicWorkflow.Testing.test_video_preprocess_stage",
    "EdennCode.WorkflowFactory.VideoMusicWorkflow.Testing.test_window_scorer",
    "EdennCode.Util.MediaUtils.Testing.test_ffmpeg_utils",
    "EdennCode.Util.MediaUtils.Testing.test_video_compression",
    "EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.tests.test_sound_effect_generation_stage",
    "EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.tests.test_user_prompt_understanding_stage",
    "EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.tests.test_video_event_analysis_stage",
    "EdennCode.WorkflowFactory.VideoSoundEffectWorkflow.tests.test_video_to_sound_effect_prompt",
]


def load_tests(
    loader: unittest.TestLoader,
    tests: unittest.TestSuite,
    pattern: str | None,
) -> unittest.TestSuite:
    return build_suite(loader, SMOKE_MODULES)
