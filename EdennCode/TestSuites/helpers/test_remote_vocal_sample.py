from EdennCode.TestSuites.helpers.paths import REMOTE_VOCAL_CLONE_SAMPLE_PATH
from EdennCode.TestSuites.helpers.remote_vocal_sample import (
    REMOTE_VOCAL_SAMPLE_PATH_ENV,
    REMOTE_VOCAL_SAMPLE_URL_ENV,
    remote_vocal_sample_form_data,
    remote_vocal_sample_upload_tuple,
    require_remote_vocal_sample,
)


def test_remote_vocal_sample_upload_tuple_uses_bundled_sample_when_env_missing(monkeypatch) -> None:
    monkeypatch.delenv(REMOTE_VOCAL_SAMPLE_PATH_ENV, raising=False)
    monkeypatch.delenv(REMOTE_VOCAL_SAMPLE_URL_ENV, raising=False)

    upload = remote_vocal_sample_upload_tuple()

    assert upload is not None
    assert upload[0] == "vocal_sample"
    assert upload[1][0] == REMOTE_VOCAL_CLONE_SAMPLE_PATH.name
    assert upload[1][2] == "audio/mp4"
    assert len(upload[1][1]) > 0


def test_remote_vocal_sample_form_data_prefers_bundled_upload(monkeypatch) -> None:
    monkeypatch.delenv(REMOTE_VOCAL_SAMPLE_PATH_ENV, raising=False)
    monkeypatch.setenv(REMOTE_VOCAL_SAMPLE_URL_ENV, "https://example.com/sample.m4a")

    assert remote_vocal_sample_form_data() == {}


def test_require_remote_vocal_sample_accepts_bundled_sample(monkeypatch) -> None:
    monkeypatch.delenv(REMOTE_VOCAL_SAMPLE_PATH_ENV, raising=False)
    monkeypatch.delenv(REMOTE_VOCAL_SAMPLE_URL_ENV, raising=False)

    require_remote_vocal_sample()
