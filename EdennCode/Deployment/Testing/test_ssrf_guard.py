"""Caller-supplied URLs must not make us fetch from our own network.

Three staging paths (video, image, vocal sample) hand a request-body URL to
``download_public_file_to_disk``. The guard lives in the downloader itself
rather than at each call site, so these tests cover every current caller and any
future one — which is the point: the call sites already existed, none of them
checked, and the next one would not have either.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from EdennCode.Deployment.api_common import (
    EdennUnsafeAssetUrlError,
    assert_public_asset_url,
    download_public_file_to_disk,
)


@pytest.mark.parametrize(
    "url,why",
    [
        ("http://169.254.169.254/latest/meta-data/iam/security-credentials/", "cloud metadata"),
        ("http://metadata.google.internal/computeMetadata/v1/", "cloud metadata by name"),
        ("http://localhost:8800/dev/uploads/x.mp4", "localhost by name"),
        ("http://127.0.0.1/x.mp4", "loopback literal"),
        ("http://[::1]/x.mp4", "loopback v6"),
        ("http://10.1.2.3/x.mp4", "private class A"),
        ("http://192.168.0.10/x.mp4", "private class C"),
        ("http://172.16.5.4/x.mp4", "private class B"),
        ("http://0.0.0.0/x.mp4", "unspecified"),
        ("http://224.0.0.1/x.mp4", "multicast"),
    ],
)
def test_internal_targets_are_refused(url: str, why: str) -> None:
    with pytest.raises(EdennUnsafeAssetUrlError):
        assert_public_asset_url(url, asset_label="video")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://x/1",
        "ftp://example.com/a.mp4",
        "",
        "not-a-url",
        "//example.com/a.mp4",
    ],
)
def test_non_http_schemes_are_refused(url: str) -> None:
    with pytest.raises(EdennUnsafeAssetUrlError):
        assert_public_asset_url(url, asset_label="video")


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.com/clip.mp4",
        "http://example.com:8080/clip.mp4",
        "https://storage.blob.core.windows.net/c/b.mp4?sig=abc",
    ],
)
def test_ordinary_public_urls_still_pass(url: str) -> None:
    assert assert_public_asset_url(url, asset_label="video") == url


def test_the_error_names_the_asset_so_the_client_message_makes_sense() -> None:
    with pytest.raises(EdennUnsafeAssetUrlError) as exc:
        assert_public_asset_url("http://127.0.0.1/x", asset_label="vocal sample")
    assert "vocal sample" in str(exc.value)


def test_a_trailing_dot_does_not_smuggle_localhost_past_the_check() -> None:
    """``localhost.`` resolves the same and must not read as a different name."""
    with pytest.raises(EdennUnsafeAssetUrlError):
        assert_public_asset_url("http://localhost./x.mp4", asset_label="video")


def test_the_downloader_refuses_before_making_any_request(tmp_path: Path) -> None:
    """The guard must run BEFORE the fetch — the request itself is the attack,
    so a post-hoc check would already have leaked the credential."""
    with pytest.raises(EdennUnsafeAssetUrlError):
        asyncio.run(
            download_public_file_to_disk(
                url="http://169.254.169.254/latest/meta-data/",
                destination=tmp_path / "out.bin",
                asset_label="video",
            )
        )
    assert not (tmp_path / "out.bin").exists()
