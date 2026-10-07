"""Static guard against vendor / model names leaking into client-visible signed URLs.

This test reads the source of generation-stage modules and asserts that no string
literal that names a media file (.wav / .m4a / .mp3 / .mp4 / .aac / .flac) carries
an upstream provider name. It is intentionally a static text scan rather than a
runtime test — the goal is to fail fast in CI when a future PR reintroduces a
vendor-named output filename.

The names to look for are not written down here. They come from the deployment's
configuration (``PROVIDER_SCRUB_TOKENS`` and friends, see
:mod:`EdennCode.Deployment.provider_vocabulary`), so a run that configures the real
vocabulary scans for the real names while this repository stays free of them. A
scan nobody has ever seen fail is worth nothing, so every case below also plants a
leak under a synthetic vocabulary and insists the same pass catches it, and the
collision cases pin the matching rules that keep the guard from crying wolf over an
ordinary name like ``fake_provider_audio.wav``.

If you add a new generation-stage module that writes media output, register its path
in MONITORED_MODULES below.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from EdennCode.Deployment import provider_vocabulary
from EdennCode.Deployment.output_naming import contains_provider_token, provider_tokens


REPO_ROOT = Path(__file__).resolve().parents[3]


MONITORED_MODULES = [
    "EdennCode/Deployment/api_common.py",
    "EdennCode/WorkflowFactory/AudioCreativeEditWorkflow/Stages/AudioCreativeGenerationStage/audio_creative_generation_stage.py",
    "EdennCode/WorkflowFactory/VideoMusicWorkflow/Stages/MusicGenerationStage/music_generation_stage.py",
    "EdennCode/WorkflowFactory/VideoMusicWorkflow/Stages/MusicGenerationStage/music_generation_stage_callback.py",
    "EdennCode/MusicGenerationCore/providers/provider_c.py",
    "EdennCode/MusicGenerationCore/providers/provider_b.py",
    "EdennCode/ModelFactory/MusicGenModelFactory/CloudMusicGen/provider_b_music.py",
    "EdennCode/ModelFactory/MusicGenModelFactory/CloudMusicGen/cloud_music_gen_util.py",
    "EdennCode/ModelFactory/VideoSFXModelFactory/CloudSoundEffectGen/cloud_sound_effect_gen_util.py",
]


_MEDIA_LITERAL_RE = re.compile(
    r'''(?:f?["'])([^"'\n]*\.(?:wav|m4a|mp3|mp4|aac|flac|m4v))(?:["'])'''
)


_VOCABULARY_VARS = (
    "PROVIDER_SCRUB_TOKENS",
    "PROVIDER_SCRUB_TOKENS_STRICT",
    "PROVIDER_SCRUB_MODEL_PREFIXES",
)

# Invented names. Proving the scan can fail needs a vendor vocabulary, and a real
# one may not be written down in this repository — not even in a test.
_SYNTHETIC_TOKENS = ("acmesound", "rift")  # "rift" is also the tail of "adrift"
_SYNTHETIC_STRICT = ("nimbus",)  # also the stem of "nimbuses"
_SYNTHETIC_MODELS = ("orbit",)  # a model family: a leak only with a version

_PLANTED_LEAK = "acmesound_take1.wav"


def _extract_media_literals(source: str) -> list[str]:
    return _MEDIA_LITERAL_RE.findall(source)


def _extend(monkeypatch: pytest.MonkeyPatch, env_var: str, extra: tuple[str, ...]) -> None:
    """Add the synthetic names to whatever this deployment already configured.

    Appending rather than replacing keeps whatever teeth CI gave the scan instead
    of trading its vocabulary for ours.
    """
    configured = os.environ.get(env_var, "").strip()
    monkeypatch.setenv(
        env_var, ",".join(part for part in (configured, ",".join(extra)) if part)
    )


@pytest.fixture
def vendor_vocabulary(monkeypatch: pytest.MonkeyPatch):
    _extend(monkeypatch, "PROVIDER_SCRUB_TOKENS", _SYNTHETIC_TOKENS)
    _extend(monkeypatch, "PROVIDER_SCRUB_TOKENS_STRICT", _SYNTHETIC_STRICT)
    _extend(monkeypatch, "PROVIDER_SCRUB_MODEL_PREFIXES", _SYNTHETIC_MODELS)
    provider_vocabulary.reset_cache()
    yield
    # Restore the environment *before* dropping the cache: the next reader must
    # rebuild from the real configuration, not memoise ours.
    monkeypatch.undo()
    provider_vocabulary.reset_cache()


@pytest.fixture
def no_vocabulary(monkeypatch: pytest.MonkeyPatch):
    for env_var in _VOCABULARY_VARS:
        monkeypatch.delenv(env_var, raising=False)
    provider_vocabulary.reset_cache()
    yield
    monkeypatch.undo()
    provider_vocabulary.reset_cache()


@pytest.mark.parametrize("module_relpath", MONITORED_MODULES)
def test_no_provider_token_in_media_filenames(
    module_relpath: str, vendor_vocabulary: None
) -> None:
    module_path = REPO_ROOT / module_relpath
    assert module_path.exists(), f"Monitored module missing: {module_relpath}"
    source = module_path.read_text(encoding="utf-8")
    leaked = [
        literal
        for literal in _extract_media_literals(source)
        if contains_provider_token(literal)
    ]
    assert not leaked, (
        f"Vendor token leaked into media filename literal in {module_relpath}: "
        f"{leaked}. Tokens: {sorted(provider_tokens())}."
    )

    # A clean scan proves nothing unless the same pass, over this same file, would
    # have caught a leak. Plant one and make it say so.
    planted = source + f'\n_PLANTED = "{_PLANTED_LEAK}"\n'
    caught = [
        literal
        for literal in _extract_media_literals(planted)
        if contains_provider_token(literal)
    ]
    assert caught == [_PLANTED_LEAK], (
        f"The scan cannot see a leak in {module_relpath}; it would pass no matter "
        f"what filename a future PR wrote. Caught: {caught}."
    )


@pytest.mark.parametrize(
    "filename, is_leak",
    [
        # A configured name is a leak in every surface form an author reaches for.
        ("acmesound_take1.wav", True),
        ("acmesound4.wav", True),  # glued version digit
        ("acmesound_v2.mp3", True),  # underscore version
        ("AcmeSoundAI_master.m4a", True),  # camel-cased
        ("rift_stem.wav", True),
        ("nimbus.m4a", True),  # strict token, bare
        ("orbit-4o_render.mp4", True),  # model family with a version
        # ...and ordinary words that merely contain those letters are not.
        ("adrift_stem.wav", False),  # glued to a preceding letter
        ("nimbuses_overview.mp3", False),  # strict token inside a longer word
        ("orbit_based_summary.mp4", False),  # family name, no version
        ("fake_provider_audio.wav", False),  # scaffolding: it names nobody
    ],
)
def test_matching_flags_leaks_without_eating_ordinary_words(
    filename: str, is_leak: bool, vendor_vocabulary: None
) -> None:
    assert contains_provider_token(filename) is is_leak


def test_configured_vocabulary_reaches_the_guard(vendor_vocabulary: None) -> None:
    tokens = provider_tokens()
    assert {"acmesound", "rift", "nimbus", "orbit"} <= tokens
    assert all(token.lower() == token for token in tokens)


def test_unconfigured_deployment_has_nothing_to_match(no_vocabulary: None) -> None:
    # The documented default: a deployment that has not said which names to hide
    # gets no free-text matching rather than a guess at one. The guard is only as
    # sharp as the vocabulary CI hands it, which is why the scan above plants a
    # leak instead of trusting a quiet run.
    assert provider_tokens() == frozenset()
    assert not contains_provider_token(_PLANTED_LEAK)
