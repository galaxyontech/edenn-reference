"""Compose working directories do not outlive their output.

Every mix downloads the music, the narration and the effects bed next to the
video it renders, then writes a video-sized output beside them. Nothing removed
any of it, so each mix cost several video-sized files on a container disk that is
wiped on deploy and unmonitored until it is full.

The rule is not "delete everything": with no blob storage configured the local
output IS what the caller returns, so it has to survive. What goes is whatever is
a copy of something that exists elsewhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from EdennCode.EdennAgent.AgenticAudio.tools.media import _release_workdir


def _populate(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    for name in ("music.audio", "voiceover.audio", "sfx.audio"):
        (root / name).write_bytes(b"x" * 128)
    output = root / "mixed_video.mp4"
    output.write_bytes(b"v" * 512)
    return root, output


def test_everything_goes_once_the_output_is_uploaded(tmp_path: Path) -> None:
    """After an upload nothing on disk here is the last copy of anything."""
    workdir, _ = _populate(tmp_path / "mix_1")
    _release_workdir(workdir)
    assert not workdir.exists()


def test_the_deliverable_survives_when_there_is_nowhere_to_upload_it(
    tmp_path: Path,
) -> None:
    """With no storage configured the local path is what the caller returns —
    deleting it would delete the mix the user just paid for."""
    workdir, output = _populate(tmp_path / "mix_2")
    _release_workdir(workdir, keep=output)
    assert output.exists(), "the deliverable was deleted"
    assert output.read_bytes() == b"v" * 512


def test_the_redundant_inputs_go_even_when_the_output_stays(tmp_path: Path) -> None:
    """They are copies of files that exist elsewhere, and they are the bulk of
    what a mix leaves behind."""
    workdir, output = _populate(tmp_path / "mix_3")
    _release_workdir(workdir, keep=output)
    left = {p.name for p in workdir.iterdir()}
    assert left == {"mixed_video.mp4"}, f"left behind: {sorted(left)}"


def test_nested_directories_are_removed(tmp_path: Path) -> None:
    workdir, output = _populate(tmp_path / "mix_4")
    nested = workdir / "frames"
    nested.mkdir()
    (nested / "0001.png").write_bytes(b"p")
    _release_workdir(workdir, keep=output)
    assert not nested.exists()


def test_cleanup_never_fails_the_render(tmp_path: Path) -> None:
    """A render that succeeded must not be reported as failed because tidying up
    afterwards did not work."""
    missing = tmp_path / "never_existed"
    _release_workdir(missing)  # must not raise

    workdir, output = _populate(tmp_path / "mix_5")
    # A path that cannot be resolved must not take the process down either.
    _release_workdir(workdir, keep=workdir / "not_here.mp4")
    assert workdir.exists()


def test_keeping_a_file_that_is_not_in_the_directory_is_survivable(
    tmp_path: Path,
) -> None:
    workdir, _ = _populate(tmp_path / "mix_6")
    elsewhere = tmp_path / "elsewhere.mp4"
    elsewhere.write_bytes(b"v")
    _release_workdir(workdir, keep=elsewhere)
    assert elsewhere.exists()


def test_both_compose_paths_release_their_workdir() -> None:
    """compose_mix and the remix path both leaked; a fix to one is not a fix."""
    source = Path(
        __file__
    ).resolve().parent.parent.joinpath("tools", "media.py").read_text(encoding="utf-8")
    assert source.count("_release_workdir(workdir)") >= 2, (
        "one of the compose paths still abandons its working directory"
    )
    assert source.count("_release_workdir(workdir, keep=output_path)") >= 2
