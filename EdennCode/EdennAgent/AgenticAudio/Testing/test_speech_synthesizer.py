"""The hosted speech provider adapter, tested at its boundaries.

What matters here, in order: no vendor name can leak out of the adapter (global
rule — logs, exceptions, anything a client could see); the engine selection is
configuration, not code; and the request the adapter sends actually reflects
the direction and speed the director chose.
"""

from __future__ import annotations

import io
import json
import urllib.error
import re
from pathlib import Path
from typing import Any

import pytest

from EdennCode.ModelFactory.VoiceOverModelFactory.hosted_speech_synthesizer import (
    HostedSpeechSynthesizer,
    _scrubbed,
    _settings_for_test_hook,
)


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #


class _FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):  # noqa: D401
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def _synth(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> HostedSpeechSynthesizer:
    monkeypatch.setenv("PROVIDER_A_API_KEY", "test-key")
    return HostedSpeechSynthesizer(**kwargs)


# --------------------------------------------------------------------------- #
# construction and configuration                                              #
# --------------------------------------------------------------------------- #


def test_no_credential_is_a_loud_refusal(monkeypatch) -> None:
    monkeypatch.delenv("PROVIDER_A_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as exc:
        HostedSpeechSynthesizer()
    assert "credential" in str(exc.value)
    # The refusal itself must not name the vendor.
    assert "eleven" not in str(exc.value).lower()


def test_model_is_configurable_without_code(monkeypatch) -> None:
    monkeypatch.setenv("AGENTIC_AUDIO_SPEECH_MODEL", "some_newer_model")
    synth = _synth(monkeypatch)
    assert synth._model == "some_newer_model"


# --------------------------------------------------------------------------- #
# direction mapping                                                           #
# --------------------------------------------------------------------------- #


def test_restrained_direction_stays_alive_but_steady() -> None:
    """Above the base stability, below the "monotonous" ceiling — restrained
    is a performance, not flatness (the 0.68 mapping read as detached)."""
    settings = _settings_for_test_hook("restrained, warm — land softly", 1.0)
    assert 0.5 <= settings["stability"] <= 0.6
    assert settings["style"] >= 0.2


def test_energetic_direction_raises_style() -> None:
    settings = _settings_for_test_hook("upbeat and punchy", 1.0)
    assert settings["style"] > 0.4


def test_speed_is_clamped_to_the_providers_range() -> None:
    assert _settings_for_test_hook("", 3.0)["speed"] == 1.2
    assert _settings_for_test_hook("", 0.1)["speed"] == 0.7
    assert _settings_for_test_hook("", "not-a-number")["speed"] == 1.0


# --------------------------------------------------------------------------- #
# voice resolution                                                            #
# --------------------------------------------------------------------------- #


def test_every_preset_voice_resolves_without_any_network_call(monkeypatch) -> None:
    """The account's keys are scoped to synthesis only — resolution must never
    need a listing endpoint. EVERY catalog voice must resolve distinctly: the
    roster is a casting sheet, and two presets landing on one voice would make
    the director's casting a lie."""
    monkeypatch.delenv("AGENTIC_AUDIO_SPEECH_VOICE_MAP", raising=False)
    from EdennCode.EdennAgent.AgenticAudio.models import VOICE_CATALOG

    synth = _synth(monkeypatch)
    seen = set()
    for preset in VOICE_CATALOG:
        vid = synth._resolve_voice(preset["voice"])
        assert vid and len(vid) == 20, preset["id"]
        seen.add(vid)
    assert len(seen) == len(VOICE_CATALOG), "each preset needs its own voice"


def test_an_unknown_voice_falls_back_to_the_default_preset(monkeypatch) -> None:
    monkeypatch.delenv("AGENTIC_AUDIO_SPEECH_VOICE_MAP", raising=False)
    synth = _synth(monkeypatch)
    assert synth._resolve_voice("no-such-voice") == synth._resolve_voice("shimmer")


def test_the_voice_map_override_wins_without_a_code_change(monkeypatch) -> None:
    monkeypatch.setenv("AGENTIC_AUDIO_SPEECH_VOICE_MAP", '{"shimmer": "custom-id-123"}')
    synth = _synth(monkeypatch)
    assert synth._resolve_voice("shimmer") == "custom-id-123"
    # Un-overridden presets keep their premade ids.
    assert synth._resolve_voice("onyx") != "custom-id-123"


def test_a_malformed_voice_map_is_ignored_not_fatal(monkeypatch) -> None:
    monkeypatch.setenv("AGENTIC_AUDIO_SPEECH_VOICE_MAP", "not json at all")
    synth = _synth(monkeypatch)
    assert len(synth._resolve_voice("shimmer")) == 20


# --------------------------------------------------------------------------- #
# the request itself                                                          #
# --------------------------------------------------------------------------- #


def test_the_request_carries_text_model_and_settings(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENTIC_AUDIO_SPEECH_VOICE_MAP", '{"shimmer": "id-rachel"}')
    synth = _synth(monkeypatch)
    captured: dict[str, Any] = {}

    import base64

    def fake_post(url: str, body: bytes, **_kw: Any) -> bytes:
        captured["url"] = url
        captured["body"] = json.loads(body)
        return json.dumps({
            "audio_base64": base64.b64encode(b"fake-audio").decode(),
            "alignment": {
                "characters": ["h", "i"],
                "character_start_times_seconds": [0.0, 0.2],
                "character_end_times_seconds": [0.2, 0.4],
            },
        }).encode()

    monkeypatch.setattr(synth, "_post", fake_post)
    monkeypatch.setattr(
        type(synth), "_write_wav",
        staticmethod(lambda audio, out: Path(out).write_bytes(audio)),
    )

    out = tmp_path / "line.wav"
    import asyncio

    asyncio.run(synth.synthesize(
        script="Every glance lingers.", voice="shimmer",
        instructions="restrained, warm", speed=0.95, out_path=out,
    ))
    assert "id-rachel" in captured["url"]
    assert "/with-timestamps" in captured["url"], "the timing win is the endpoint"
    assert captured["body"]["text"] == "Every glance lingers."
    assert captured["body"]["voice_settings"]["speed"] == 0.95
    assert out.read_bytes() == b"fake-audio"
    # Measured character times land in the sidecar for the fit pipeline.
    sidecar = out.with_suffix(out.suffix + ".alignment.json")
    align = json.loads(sidecar.read_text())
    assert align["character_end_times_seconds"][-1] == 0.4


def test_a_raw_audio_body_still_synthesizes_without_timing(monkeypatch, tmp_path) -> None:
    """Timing is an upgrade, not a dependency — a rollback to the raw endpoint
    must not break narration."""
    monkeypatch.setenv("AGENTIC_AUDIO_SPEECH_VOICE_MAP", '{"shimmer": "id-rachel"}')
    synth = _synth(monkeypatch)
    monkeypatch.setattr(synth, "_post", lambda url, body, **_kw: b"\xff\xf3raw-mp3-bytes")
    monkeypatch.setattr(
        type(synth), "_write_wav",
        staticmethod(lambda audio, out: Path(out).write_bytes(audio)),
    )
    out = tmp_path / "line.wav"
    synth._synthesize_sync("hi", "shimmer", "", 1.0, out)
    assert out.exists()
    assert not out.with_suffix(out.suffix + ".alignment.json").exists()


def test_a_speed_dropping_model_warns_loudly(monkeypatch, tmp_path, caplog) -> None:
    """Measured 2026-08-26: the expressive model accepts speed and ignores it.
    A future model swap must not silently break timing fit."""
    monkeypatch.setenv("AGENTIC_AUDIO_SPEECH_MODEL", "eleven_v3")
    monkeypatch.setenv("AGENTIC_AUDIO_SPEECH_VOICE_MAP", '{"shimmer": "id-x"}')
    synth = _synth(monkeypatch)
    monkeypatch.setattr(synth, "_post", lambda url, body, **_kw: b"\xffaudio")
    monkeypatch.setattr(
        type(synth), "_write_wav",
        staticmethod(lambda audio, out: Path(out).write_bytes(audio)),
    )
    import logging as _logging

    with caplog.at_level(_logging.WARNING):
        synth._synthesize_sync("hi", "shimmer", "", 0.9, tmp_path / "x.wav")
    assert any("ignores speed" in r.message for r in caplog.records)
    # The warning itself must stay vendor-neutral.
    assert not any("eleven" in r.message.lower() for r in caplog.records)


def test_an_empty_script_is_refused_before_any_spend(monkeypatch, tmp_path) -> None:
    synth = _synth(monkeypatch)
    with pytest.raises(ValueError):
        synth._synthesize_sync("   ", "shimmer", "", 1.0, tmp_path / "x.wav")


# --------------------------------------------------------------------------- #
# vendor-name containment                                                     #
# --------------------------------------------------------------------------- #


def test_provider_error_text_is_scrubbed(monkeypatch, tmp_path) -> None:
    """An upstream error body full of vendor strings must come out neutral."""
    synth = _synth(monkeypatch, timeout_s=0.1)

    def boom(req, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError(
            req.full_url, 401,
            "Unauthorized", {},
            io.BytesIO(b'{"detail": "ProviderA xi-api-key invalid for eleven_multilingual_v2"}'),
        )

    monkeypatch.setattr(
        "EdennCode.ModelFactory.VoiceOverModelFactory."
        "hosted_speech_synthesizer.urllib.request.urlopen", boom,
    )
    with pytest.raises(RuntimeError) as exc:
        synth._synthesize_sync("hi", "shimmer", "", 1.0, tmp_path / "x.wav")
    text = str(exc.value).lower()
    assert "eleven" not in text
    assert "xi-api-key" not in text
    assert "401" in text  # the useful part survives


def test_scrubber_catches_the_spellings_that_appear_in_the_wild() -> None:
    dirty = "ProviderA said eleven_turbo_v2_5 rejected your xi-api-key (Eleven Labs)"
    clean = _scrubbed(dirty).lower()
    assert "eleven" not in clean
    assert "xi-api-key" not in clean


_ALIAS = re.compile(r"(?<![a-z])provider_a(?![a-z])")


def test_nothing_outside_the_adapter_names_the_vendor() -> None:
    """The devserver's narration path speaks neutrally; the vendor's name may
    appear only in the adapter module and in configuration keys."""
    src = Path("EdennCode/EdennAgent/AgenticAudio/design/devserver.py").read_text()
    for i, line in enumerate(src.splitlines(), 1):
        if "PROVIDER_A_API_KEY" in line:  # the config key itself is the boundary
            continue
        if '"provider_a"' in line:  # provider-registry config VALUE — boundary vocabulary
            continue
        lowered = line.lower()
        # The alias must not match inside an ordinary word: ``provider_audio_id``
        # is a field name, not the vendor. Same boundary rule as
        # Deployment/provider_vocabulary.py and the adapter's own scrubber.
        if _ALIAS.search(lowered) and not lowered.strip().startswith("#"):
            raise AssertionError(f"vendor name outside the adapter: devserver.py:{i}")


# --------------------------------------------------------------------------- #
# engine selection                                                            #
# --------------------------------------------------------------------------- #


def test_the_devserver_selects_engines_by_configuration() -> None:
    src = Path("EdennCode/EdennAgent/AgenticAudio/design/devserver.py").read_text()
    assert "_narration_synthesizer()" in src
    assert 'AGENTIC_AUDIO_TTS_ENGINE' in src
    # The platform engine stays wired as the fallback.
    assert "DefaultVoiceoverSynthesizer" in src


# --------------------------------------------------------------------------- #
# the expressive model line                                                   #
# --------------------------------------------------------------------------- #


def test_expressive_model_reads_its_direction_as_a_tag(monkeypatch, tmp_path) -> None:
    """On the expressive line the director's per-line delivery becomes an
    inline tag — the only engine whose read can follow the prose."""
    monkeypatch.setenv("AGENTIC_AUDIO_SPEECH_MODEL", "eleven_v3")
    monkeypatch.setenv("AGENTIC_AUDIO_SPEECH_VOICE_MAP", '{"onyx": "id-x"}')
    synth = _synth(monkeypatch)
    captured: dict[str, Any] = {}

    def fake_post(url: str, body: bytes, **_kw: Any) -> bytes:
        captured["body"] = json.loads(body)
        return b"\xffaudio"

    monkeypatch.setattr(synth, "_post", fake_post)
    monkeypatch.setattr(type(synth), "_write_wav",
                        staticmethod(lambda audio, out: Path(out).write_bytes(audio)))
    synth._synthesize_sync(
        "In the frozen silence... power awakens.", "onyx",
        "Narrate naturally. Delivery for this line: low, ominous, controlled.",
        1.0, tmp_path / "x.wav",
    )
    assert captured["body"]["text"].startswith("[low, ominous, controlled]")
    # The expressive schema takes stability only — no speed, no style.
    assert set(captured["body"]["voice_settings"].keys()) == {"stability"}


def test_expressive_model_reports_no_speed_support(monkeypatch) -> None:
    monkeypatch.setenv("AGENTIC_AUDIO_SPEECH_MODEL", "eleven_v3")
    assert _synth(monkeypatch).supports_speed is False
    monkeypatch.setenv("AGENTIC_AUDIO_SPEECH_MODEL", "eleven_multilingual_v2")
    assert _synth(monkeypatch).supports_speed is True


def test_calm_direction_is_no_longer_flat() -> None:
    """"Restrained" is a performance, not an absence of one: the old mapping
    (stability 0.68) sat in the provider's own "monotonous" territory and a
    listener called the result detached."""
    settings = _settings_for_test_hook("low, ominous, controlled", 1.0)
    assert 0.45 <= settings["stability"] <= 0.6
    assert settings["style"] >= 0.2
