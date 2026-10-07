import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from EdennCode.Util.MediaUtils.ffmpeg_utils import (
    compress_video_to_max_height,
    ensure_h264_video,
    resolve_ffmpeg_binary,
)
from EdennCode.TestSuites.helpers.paths import ROTATED_SMOKE_VIDEO_PATH

_FAKE_RESULT = subprocess.CompletedProcess(args=[], returncode=0)
_MODULE = "EdennCode.Util.MediaUtils.ffmpeg_utils"


def _write_test_video(
    path: Path,
    *,
    width: int,
    height: int,
    duration_s: float = 1.0,
    codec: str = "libx264",
) -> None:
    ffmpeg_bin = resolve_ffmpeg_binary()
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=blue:size={width}x{height}:rate=1:duration={duration_s}",
            "-an",
            "-c:v",
            codec,
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _probe_dimensions(video_path: Path) -> tuple[int, int]:
    ffprobe_bin = str(Path(resolve_ffmpeg_binary()).with_name("ffprobe"))
    result = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(video_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    width_raw, height_raw = result.stdout.strip().split("x", 1)
    return int(width_raw), int(height_raw)


def _probe_video_codec(video_path: Path) -> str:
    ffprobe_bin = str(Path(resolve_ffmpeg_binary()).with_name("ffprobe"))
    result = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            str(video_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().lower()


class VideoCompressionIntegrationTests(unittest.TestCase):
    def test_taller_video_is_downscaled_to_requested_height(self) -> None:
        with tempfile.TemporaryDirectory(prefix="video-compression-") as tmp:
            tmp_dir = Path(tmp)
            source_path = tmp_dir / "tall_input.mp4"
            _write_test_video(source_path, width=96, height=1400)

            output_path = compress_video_to_max_height(source_path, max_height=1280)

            self.assertEqual(output_path.name, "tall_input_1280h.mp4")
            self.assertNotEqual(output_path, source_path.resolve())
            self.assertTrue(output_path.exists())

            width, height = _probe_dimensions(output_path)
            self.assertEqual(height, 1280)
            self.assertLess(width, 96)

    def test_shorter_video_skips_compression_and_keeps_original_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="video-compression-") as tmp:
            tmp_dir = Path(tmp)
            source_path = tmp_dir / "short_input.mp4"
            skipped_output_path = tmp_dir / "short_input_1280h.mp4"
            _write_test_video(source_path, width=96, height=1200)

            output_path = compress_video_to_max_height(
                source_path,
                output_path=skipped_output_path,
                max_height=1280,
            )

            self.assertEqual(output_path, source_path.resolve())
            self.assertFalse(skipped_output_path.exists())

            width, height = _probe_dimensions(output_path)
            self.assertEqual((width, height), (96, 1200))

    def test_non_h264_video_within_height_is_reencoded_to_h264(self) -> None:
        with tempfile.TemporaryDirectory(prefix="video-compression-") as tmp:
            tmp_dir = Path(tmp)
            source_path = tmp_dir / "hevc_like_input.mp4"
            _write_test_video(source_path, width=96, height=1200, codec="mpeg4")
            self.assertNotEqual(_probe_video_codec(source_path), "h264")

            output_path = compress_video_to_max_height(source_path, max_height=1280)

            self.assertNotEqual(output_path, source_path.resolve())
            self.assertTrue(output_path.exists())
            self.assertEqual(_probe_video_codec(output_path), "h264")

            width, height = _probe_dimensions(output_path)
            self.assertEqual((width, height), (96, 1200))

    def test_rotated_video_uses_display_height_for_compression_threshold(self) -> None:
        with tempfile.TemporaryDirectory(prefix="video-compression-") as tmp:
            tmp_dir = Path(tmp)
            source_path = tmp_dir / ROTATED_SMOKE_VIDEO_PATH.name
            source_path.write_bytes(ROTATED_SMOKE_VIDEO_PATH.read_bytes())

            output_path = compress_video_to_max_height(source_path, max_height=500)

            self.assertEqual(output_path.name, "Videos2026-04-08_104426_347_500h.mp4")
            self.assertNotEqual(output_path, source_path.resolve())
            self.assertTrue(output_path.exists())

            width, height = _probe_dimensions(output_path)
            self.assertEqual(height, 500)
            self.assertLess(width, height)


class CompressionFlagsTests(unittest.TestCase):
    """
    Unit tests that verify the exact ffmpeg flags built by compress_video_to_max_height.
    _run_ffmpeg_cmd is mocked so no real ffmpeg process is needed.
    """

    def _run(self, **kwargs):
        """Run compress_video_to_max_height with dimensions mocked to force compression."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            src = tmp_dir / "input.mp4"
            src.touch()
            out = tmp_dir / "out.mp4"
            with (
                patch(f"{_MODULE}.resolve_ffmpeg_binary", return_value="ffmpeg"),
                patch(f"{_MODULE}._get_video_dimensions", return_value=(96, 1400)),
                patch(f"{_MODULE}.has_audio_stream", return_value=kwargs.pop("has_audio", False)),
                patch(f"{_MODULE}._run_ffmpeg_cmd", return_value=_FAKE_RESULT) as mock_run,
            ):
                compress_video_to_max_height(src, out, **kwargs)
        return mock_run

    def test_default_codec_is_libx264(self) -> None:
        mock_run = self._run()
        cmd = mock_run.call_args[0][0]
        self.assertIn("libx264", cmd)

    def test_default_crf_is_23(self) -> None:
        mock_run = self._run()
        cmd = mock_run.call_args[0][0]
        crf_idx = cmd.index("-crf")
        self.assertEqual(cmd[crf_idx + 1], "23")

    def test_default_preset_is_veryfast(self) -> None:
        mock_run = self._run()
        cmd = mock_run.call_args[0][0]
        preset_idx = cmd.index("-preset")
        self.assertEqual(cmd[preset_idx + 1], "veryfast")

    def test_h264_profile_high_level_31(self) -> None:
        mock_run = self._run()
        cmd = mock_run.call_args[0][0]
        self.assertIn("-profile:v", cmd)
        self.assertEqual(cmd[cmd.index("-profile:v") + 1], "high")
        self.assertIn("-level", cmd)
        self.assertEqual(cmd[cmd.index("-level") + 1], "3.1")

    def test_pix_fmt_yuv420p(self) -> None:
        mock_run = self._run()
        cmd = mock_run.call_args[0][0]
        self.assertIn("-pix_fmt", cmd)
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "yuv420p")

    def test_video_encoder_threads_auto(self) -> None:
        mock_run = self._run()
        cmd = mock_run.call_args[0][0]
        self.assertIn("-threads", cmd)
        self.assertEqual(cmd[cmd.index("-threads") + 1], "0")

    def test_movflags_faststart(self) -> None:
        mock_run = self._run()
        cmd = mock_run.call_args[0][0]
        self.assertIn("-movflags", cmd)
        self.assertEqual(cmd[cmd.index("-movflags") + 1], "+faststart")

    def test_custom_crf_respected(self) -> None:
        mock_run = self._run(crf=18)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("-crf") + 1], "18")

    def test_custom_preset_respected(self) -> None:
        mock_run = self._run(preset="slow")
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("-preset") + 1], "slow")

    def test_audio_copy_included_when_audio_present(self) -> None:
        mock_run = self._run(has_audio=True)
        cmd = mock_run.call_args[0][0]
        self.assertIn("-c:a", cmd)
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")

    def test_no_audio_flags_when_no_audio_stream(self) -> None:
        mock_run = self._run(has_audio=False)
        cmd = mock_run.call_args[0][0]
        self.assertNotIn("-c:a", cmd)

    def test_force_reencode_bypasses_height_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            src = tmp_dir / "input.mp4"
            src.touch()
            out = tmp_dir / "out.mp4"
            with (
                patch(f"{_MODULE}.resolve_ffmpeg_binary", return_value="ffmpeg"),
                patch(f"{_MODULE}._get_video_dimensions", return_value=(96, 1200)),
                patch(f"{_MODULE}.has_audio_stream", return_value=False),
                patch(f"{_MODULE}._run_ffmpeg_cmd", return_value=_FAKE_RESULT) as mock_run,
            ):
                output = compress_video_to_max_height(
                    src,
                    out,
                    max_height=1280,
                    force_reencode=True,
                )

        self.assertEqual(output, out.resolve())
        self.assertEqual(mock_run.call_count, 1)

    def test_repair_decode_errors_forces_reencode_when_decode_scan_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            src = tmp_dir / "input.mp4"
            src.touch()
            out = tmp_dir / "out.mp4"
            with (
                patch(f"{_MODULE}.resolve_ffmpeg_binary", return_value="ffmpeg"),
                patch(f"{_MODULE}._get_video_dimensions", return_value=(96, 1200)),
                patch(f"{_MODULE}.get_video_codec", return_value="h264"),
                patch(f"{_MODULE}.has_video_decode_errors", return_value=True),
                patch(f"{_MODULE}.has_audio_stream", return_value=False),
                patch(f"{_MODULE}._run_ffmpeg_cmd", return_value=_FAKE_RESULT) as mock_run,
            ):
                output = compress_video_to_max_height(
                    src,
                    out,
                    max_height=1280,
                    repair_decode_errors=True,
                )

        self.assertEqual(output, out.resolve())
        self.assertEqual(mock_run.call_count, 1)

    def _run_within_height(self, codec_probe_result, **kwargs):
        """Run compress_video_to_max_height on a source already within max_height."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            src = tmp_dir / "input.mp4"
            src.touch()
            out = tmp_dir / "out.mp4"
            with (
                patch(f"{_MODULE}.resolve_ffmpeg_binary", return_value="ffmpeg"),
                patch(f"{_MODULE}._get_video_dimensions", return_value=(96, 1200)),
                patch(f"{_MODULE}.get_video_codec", return_value=codec_probe_result),
                patch(f"{_MODULE}.has_audio_stream", return_value=False),
                patch(f"{_MODULE}._run_ffmpeg_cmd", return_value=_FAKE_RESULT) as mock_run,
            ):
                output = compress_video_to_max_height(src, out, max_height=1280, **kwargs)
        return output, src.resolve(), out.resolve(), mock_run

    def test_h264_source_within_height_skips_reencode(self) -> None:
        output, src, _, mock_run = self._run_within_height("h264")
        self.assertEqual(output, src)
        self.assertEqual(mock_run.call_count, 0)

    def test_hevc_source_within_height_forces_h264_reencode(self) -> None:
        output, _, out, mock_run = self._run_within_height("hevc")
        self.assertEqual(output, out)
        self.assertEqual(mock_run.call_count, 1)
        cmd = mock_run.call_args[0][0]
        self.assertIn("libx264", cmd)

    def test_unknown_codec_within_height_forces_h264_reencode(self) -> None:
        output, _, out, mock_run = self._run_within_height(None)
        self.assertEqual(output, out)
        self.assertEqual(mock_run.call_count, 1)
        cmd = mock_run.call_args[0][0]
        self.assertIn("libx264", cmd)

    def test_reencode_failure_can_return_original(self) -> None:
        libx264_error = subprocess.CalledProcessError(1, "ffmpeg", stderr="bad stream")
        mpeg4_error = subprocess.CalledProcessError(1, "ffmpeg", stderr="still bad")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            src = tmp_dir / "input.mp4"
            src.touch()
            out = tmp_dir / "out.mp4"
            with (
                patch(f"{_MODULE}.resolve_ffmpeg_binary", return_value="ffmpeg"),
                patch(f"{_MODULE}._get_video_dimensions", return_value=(96, 1200)),
                patch(f"{_MODULE}.has_audio_stream", return_value=False),
                patch(
                    f"{_MODULE}._run_ffmpeg_cmd",
                    side_effect=[libx264_error, mpeg4_error],
                ) as mock_run,
            ):
                output = compress_video_to_max_height(
                    src,
                    out,
                    max_height=1280,
                    force_reencode=True,
                    return_original_on_failure=True,
                )

        self.assertEqual(output, src.resolve())
        self.assertEqual(mock_run.call_count, 2)

    def test_reencode_validation_failure_can_return_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            src = tmp_dir / "input.mp4"
            src.touch()
            out = tmp_dir / "out.mp4"
            with (
                patch(f"{_MODULE}.resolve_ffmpeg_binary", return_value="ffmpeg"),
                patch(f"{_MODULE}._get_video_dimensions", return_value=(96, 1200)),
                patch(f"{_MODULE}.has_audio_stream", return_value=False),
                patch(f"{_MODULE}._run_ffmpeg_cmd", return_value=_FAKE_RESULT) as mock_run,
                patch(
                    f"{_MODULE}._reencoded_video_validation_error",
                    return_value="output video stream duration is too short",
                ) as mock_validate,
            ):
                output = compress_video_to_max_height(
                    src,
                    out,
                    max_height=1280,
                    force_reencode=True,
                    return_original_on_failure=True,
                    validate_reencode=True,
                )

        self.assertEqual(output, src.resolve())
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(mock_validate.call_count, 2)

    def test_mpeg4_fallback_when_libx264_fails(self) -> None:
        libx264_error = subprocess.CalledProcessError(1, "ffmpeg", stderr="libx264 not found")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            src = tmp_dir / "input.mp4"
            src.touch()
            out = tmp_dir / "out.mp4"
            with (
                patch(f"{_MODULE}.resolve_ffmpeg_binary", return_value="ffmpeg"),
                patch(f"{_MODULE}._get_video_dimensions", return_value=(96, 1400)),
                patch(f"{_MODULE}.has_audio_stream", return_value=False),
                patch(f"{_MODULE}._run_ffmpeg_cmd", side_effect=[libx264_error, _FAKE_RESULT]) as mock_run,
            ):
                compress_video_to_max_height(src, out)

        self.assertEqual(mock_run.call_count, 2)
        first_cmd = mock_run.call_args_list[0][0][0]
        second_cmd = mock_run.call_args_list[1][0][0]
        self.assertIn("libx264", first_cmd)
        self.assertIn("mpeg4", second_cmd)
        self.assertIn("-q:v", second_cmd)
        self.assertEqual(second_cmd[second_cmd.index("-q:v") + 1], "4")
        self.assertNotIn("-crf", second_cmd)
        self.assertNotIn("-preset", second_cmd)

    def test_genpts_fflags_present(self) -> None:
        mock_run = self._run()
        cmd = mock_run.call_args[0][0]
        self.assertIn("-fflags", cmd)
        self.assertEqual(cmd[cmd.index("-fflags") + 1], "+genpts")


class EnsureH264VideoTests(unittest.TestCase):
    """ensure_h264_video: codec normalization without downscaling."""

    def _run(self, codec_probe_result, *, dimensions=(1080, 1920)):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            src = tmp_dir / "input.mov"
            src.touch()
            with (
                patch(f"{_MODULE}.resolve_ffmpeg_binary", return_value="ffmpeg"),
                patch(f"{_MODULE}._get_video_dimensions", return_value=dimensions),
                patch(f"{_MODULE}.get_video_codec", return_value=codec_probe_result),
                patch(f"{_MODULE}.has_audio_stream", return_value=False),
                patch(f"{_MODULE}._run_ffmpeg_cmd", return_value=_FAKE_RESULT) as mock_run,
            ):
                output = ensure_h264_video(src)
        return output, src.resolve(), mock_run

    def test_h264_source_is_returned_untouched(self) -> None:
        output, src, mock_run = self._run("h264")
        self.assertEqual(output, src)
        self.assertEqual(mock_run.call_count, 0)

    def test_non_h264_source_is_reencoded_without_level_cap(self) -> None:
        output, src, mock_run = self._run("hevc", dimensions=(2160, 3840))
        self.assertEqual(output.name, "input_h264.mp4")
        self.assertNotEqual(output, src)
        self.assertEqual(mock_run.call_count, 1)
        cmd = mock_run.call_args[0][0]
        self.assertIn("libx264", cmd)
        # No downscale cap → x264 must pick a conformant level on its own; a
        # hardcoded 3.1 would be violated by 4K sources.
        self.assertNotIn("-level", cmd)

    def test_integration_non_h264_source_becomes_h264_at_original_size(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ensure-h264-") as tmp:
            tmp_dir = Path(tmp)
            source_path = tmp_dir / "raw_upload.mp4"
            _write_test_video(source_path, width=96, height=1200, codec="mpeg4")

            output_path = ensure_h264_video(source_path)

            self.assertEqual(output_path.name, "raw_upload_h264.mp4")
            self.assertEqual(_probe_video_codec(output_path), "h264")
            self.assertEqual(_probe_dimensions(output_path), (96, 1200))

    def test_integration_odd_dimensions_are_coerced_even_not_mpeg4(self) -> None:
        # Regression: odd-sized sources made libx264 (yuv420p) fail in the
        # no-downscale re-encode, silently falling back to MPEG-4 — producing a
        # file named *_h264.mp4 that wasn't H.264.
        with tempfile.TemporaryDirectory(prefix="ensure-h264-odd-") as tmp:
            tmp_dir = Path(tmp)
            source_path = tmp_dir / "odd_upload.mp4"
            _write_test_video(source_path, width=97, height=99, codec="mpeg4")

            output_path = ensure_h264_video(source_path)

            self.assertEqual(_probe_video_codec(output_path), "h264")
            self.assertEqual(_probe_dimensions(output_path), (96, 98))

    def test_integration_h264_source_untouched(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ensure-h264-") as tmp:
            tmp_dir = Path(tmp)
            source_path = tmp_dir / "already_fine.mp4"
            _write_test_video(source_path, width=96, height=1200)

            output_path = ensure_h264_video(source_path)

            self.assertEqual(output_path, source_path.resolve())
            self.assertFalse((tmp_dir / "already_fine_h264.mp4").exists())


if __name__ == "__main__":
    unittest.main()
