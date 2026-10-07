import random
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
from unittest.mock import patch

from EdennCode.Util.MediaUtils import ffmpeg_utils
from EdennCode.Util.MediaUtils.ffmpeg_utils import (
    SUPPORTED_TRANSITIONS,
    XFADE_TRANSITIONS,
    _escape_ffmpeg_path,
    _slideshow_filter_complex,
    build_slideshow_video,
    effective_transition_duration_s,
    extract_audio_window,
    get_video_duration,
    mux_image_with_audio,
    normalize_transition_list,
    overlay_music_on_video,
    parse_transition_spec,
    pick_random_transitions,
    resolve_ffmpeg_binary,
)


def _capture_filter_complex(image_paths, **kwargs) -> str:
    """Run build_slideshow_video with ffmpeg stubbed; return its -filter_complex."""
    captured = {}

    def _fake_run(cmd, **_kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    output_path = Path(image_paths[0]).parent / "captured_out.mp4"
    with patch.object(ffmpeg_utils, "_run_ffmpeg_cmd", side_effect=_fake_run):
        build_slideshow_video(image_paths, output_path=output_path, **kwargs)
    cmd = captured["cmd"]
    return cmd[cmd.index("-filter_complex") + 1]


def _simple_fit(idx: int, trim_length: float, out_label: str) -> str:
    """Minimal fit_builder for exercising _slideshow_filter_complex in isolation."""
    return (
        f"[{idx}:v]scale=100:100,"
        f"trim=duration={trim_length:.6f},setpts=PTS-STARTPTS,fps=30[{out_label}]"
    )


def _moov_is_before_mdat(mp4_path: Path) -> bool:
    """Return True if the moov atom precedes the mdat atom (i.e. faststart is active)."""
    data = mp4_path.read_bytes()
    moov_pos = data.find(b"moov")
    mdat_pos = data.find(b"mdat")
    return moov_pos != -1 and mdat_pos != -1 and moov_pos < mdat_pos


class FFmpegUtilsTests(unittest.TestCase):
    def test_escape_ffmpeg_path_escapes_single_quotes(self) -> None:
        escaped = _escape_ffmpeg_path(Path("/tmp/it's fine/image.png"))
        self.assertEqual(escaped, "/tmp/it'\\''s fine/image.png")

    def test_build_slideshow_video_outputs_h264_mp4_even_dimensions(self) -> None:
        with TemporaryDirectory(prefix="ffmpeg-utils-") as tmp:
            tmp_dir = Path(tmp)
            ffmpeg_bin = resolve_ffmpeg_binary()
            ffprobe_bin = ffmpeg_bin.replace("ffmpeg", "ffprobe")
            first = tmp_dir / "frame1.png"
            second = tmp_dir / "frame2.png"
            output = tmp_dir / "slideshow.mp4"
            for color, path in (("red", first), ("blue", second)):
                subprocess.run(
                    [
                        ffmpeg_bin,
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        f"color=c={color}:s=127x95",
                        "-frames:v",
                        "1",
                        str(path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )

            build_slideshow_video(
                [first, second],
                duration_per_image=1.5,
                output_path=output,
                custom_durations=[1.0, 2.0],
            )

            duration = get_video_duration(output)
            self.assertAlmostEqual(duration, 3.0, delta=0.05)

            probe = subprocess.run(
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
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            width_text, height_text = probe.stdout.strip().split("x", maxsplit=1)
            self.assertEqual(int(width_text) % 2, 0)
            self.assertEqual(int(height_text) % 2, 0)

            # Multi-image output is uniformly H.264 video in an MP4 container.
            codec = subprocess.run(
                [
                    ffprobe_bin, "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(output),
                ],
                check=True, capture_output=True, text=True,
            )
            self.assertEqual(codec.stdout.strip(), "h264")
            container = subprocess.run(
                [
                    ffprobe_bin, "-v", "error", "-show_entries", "format=format_name",
                    "-of", "csv=p=0", str(output),
                ],
                check=True, capture_output=True, text=True,
            )
            self.assertIn("mp4", container.stdout.strip())

    def test_build_slideshow_video_normalizes_mixed_orientation_images_for_concat(self) -> None:
        with TemporaryDirectory(prefix="ffmpeg-utils-mixed-") as tmp:
            tmp_dir = Path(tmp)
            ffmpeg_bin = resolve_ffmpeg_binary()
            ffprobe_bin = ffmpeg_bin.replace("ffmpeg", "ffprobe")
            portrait = tmp_dir / "portrait.png"
            landscape = tmp_dir / "landscape.png"
            output = tmp_dir / "mixed.mp4"
            for source, path in (
                ("color=c=yellow:s=96x128", portrait),
                ("color=c=green:s=128x96", landscape),
            ):
                subprocess.run(
                    [
                        ffmpeg_bin,
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        source,
                        "-frames:v",
                        "1",
                        str(path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )

            build_slideshow_video(
                [portrait, landscape],
                duration_per_image=1.0,
                output_path=output,
            )

            probe = subprocess.run(
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
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            width_text, height_text = probe.stdout.strip().split("x", maxsplit=1)
            self.assertEqual((int(width_text), int(height_text)), (128, 128))
            self.assertAlmostEqual(get_video_duration(output), 2.0, delta=0.05)

    def _first_frame_bgr(self, mp4_path: Path, at_s: float, ffmpeg_bin: str, out_png: Path):
        import cv2

        subprocess.run(
            [ffmpeg_bin, "-y", "-ss", f"{at_s:.3f}", "-i", str(mp4_path),
             "-frames:v", "1", str(out_png)],
            check=True, capture_output=True, text=True,
        )
        return cv2.imread(str(out_png))

    def test_build_slideshow_video_blur_fill_avoids_black_bars(self) -> None:
        """A portrait image on a square canvas must fill the side gaps with a blurred
        copy of itself (default), not black letterbox bars."""
        with TemporaryDirectory(prefix="ffmpeg-utils-blur-") as tmp:
            tmp_dir = Path(tmp)
            ffmpeg_bin = resolve_ffmpeg_binary()
            portrait = tmp_dir / "portrait.png"   # 96x128 red
            landscape = tmp_dir / "landscape.png"  # 128x96 blue -> canvas 128x128
            for source, path in (
                ("color=c=red:s=96x128", portrait),
                ("color=c=blue:s=128x96", landscape),
            ):
                subprocess.run(
                    [ffmpeg_bin, "-y", "-f", "lavfi", "-i", source,
                     "-frames:v", "1", str(path)],
                    check=True, capture_output=True, text=True,
                )

            blur_out = tmp_dir / "blur.mp4"
            build_slideshow_video([portrait, landscape], duration_per_image=1.0,
                                  output_path=blur_out)  # background_mode="blur" default
            frame = self._first_frame_bgr(blur_out, 0.5, ffmpeg_bin, tmp_dir / "blur.png")
            self.assertIsNotNone(frame, "failed to extract blur frame")
            # cols 0..11 fall inside the 16px side gap of the 96-wide portrait.
            left_gap = frame[:, 0:12]
            self.assertGreater(float(left_gap.mean()), 20.0,
                               "blur fill left a near-black side gap")
            self.assertGreater(float(left_gap[:, :, 2].mean()), 60.0,
                               "blur fill side gap is not reddish (should mirror the image)")

            black_out = tmp_dir / "black.mp4"
            build_slideshow_video([portrait, landscape], duration_per_image=1.0,
                                  output_path=black_out, background_mode="black")
            frame_b = self._first_frame_bgr(black_out, 0.5, ffmpeg_bin, tmp_dir / "black.png")
            self.assertIsNotNone(frame_b, "failed to extract black frame")
            left_gap_b = frame_b[:, 0:12]
            self.assertLess(float(left_gap_b.mean()), 12.0,
                            "legacy black mode should letterbox with near-black bars")


class SlideshowTransitionTests(unittest.TestCase):
    def test_pick_random_transitions_is_deterministic_with_seed_and_in_pool(self) -> None:
        first = pick_random_transitions(8, rng=random.Random(42))
        second = pick_random_transitions(8, rng=random.Random(42))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 8)
        self.assertTrue(set(first).issubset(XFADE_TRANSITIONS))
        self.assertEqual(pick_random_transitions(0), [])

    def test_effective_transition_duration_clamps_to_half_min_clip(self) -> None:
        self.assertAlmostEqual(
            effective_transition_duration_s([3.0, 2.0, 3.0], 0.4), 0.4
        )
        self.assertAlmostEqual(
            effective_transition_duration_s([3.0, 0.5, 3.0], 0.4), 0.25
        )
        # Under two frames of blend the xfade is pointless: report hard cut.
        self.assertEqual(effective_transition_duration_s([0.12, 0.12], 0.4), 0.0)
        self.assertEqual(effective_transition_duration_s([], 0.4), 0.0)
        self.assertEqual(effective_transition_duration_s([3.0], 0.0), 0.0)

    def test_filter_complex_xfade_offsets_center_on_concat_boundaries(self) -> None:
        # Durations 2.0/2.0/2.0 with d=0.4: boundaries at 2.0 and 4.0, so the
        # transitions must start at 1.8 and 3.8 (midpoint anchored on the beat)
        # and the trims must extend by the overlap to keep the total at 6.0.
        graph = _slideshow_filter_complex(
            sanitized_durations=[2.0, 2.0, 2.0],
            fit_builder=_simple_fit,
            fps=30.0,
            transitions=["fade", "wipeleft"],
            transition_durations=[0.4, 0.4],
        )
        self.assertIn("xfade=transition=fade:duration=0.400000:offset=1.800000", graph)
        self.assertIn("xfade=transition=wipeleft:duration=0.400000:offset=3.800000", graph)
        # First clip: offset + d + 1 frame; last clip: total - last offset.
        self.assertIn("trim=duration=2.233333", graph)
        self.assertIn("trim=duration=2.200000", graph)
        self.assertIn("[vout]", graph)

    def test_filter_complex_rejects_an_unsupported_transition_name(self) -> None:
        # A name ffmpeg's xfade does not implement is a hard error: silently
        # hard-cutting would ship a video the caller did not ask for.
        with self.assertRaises(ValueError):
            _slideshow_filter_complex(
                sanitized_durations=[2.0, 2.0],
                fit_builder=_simple_fit,
                fps=30.0,
                transitions=["not_a_real_effect"],
                transition_durations=[0.4],
            )

    def test_filter_complex_hard_cuts_when_the_transition_count_mismatches(self) -> None:
        # 2 images is 1 boundary, so a 2-name list does not describe this
        # slideshow. xfade is all-or-nothing (one running chain), so the graph
        # falls back to concat rather than guessing which name to drop.
        graph = _slideshow_filter_complex(
            sanitized_durations=[2.0, 2.0],
            fit_builder=_simple_fit,
            fps=30.0,
            transitions=["fade", "fade"],
            transition_durations=[0.4],
        )

        self.assertIn("concat=n=2:v=1:a=0[vout]", graph)
        self.assertNotIn("xfade", graph)

    def test_build_slideshow_video_with_transitions_preserves_total_duration(self) -> None:
        with TemporaryDirectory(prefix="ffmpeg-xfade-") as tmp:
            tmp_dir = Path(tmp)
            ffmpeg_bin = resolve_ffmpeg_binary()
            paths = []
            for idx, color in enumerate(("red", "blue", "green")):
                path = tmp_dir / f"frame{idx}.png"
                subprocess.run(
                    [
                        ffmpeg_bin,
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        f"color=c={color}:s=128x96",
                        "-frames:v",
                        "1",
                        str(path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                paths.append(path)
            output = tmp_dir / "xfade.mp4"

            build_slideshow_video(
                paths,
                duration_per_image=1.0,
                output_path=output,
                custom_durations=[1.0, 1.2, 0.8],
                transitions=["fade", "circleopen"],
                transition_duration_s=0.3,
            )

            self.assertAlmostEqual(get_video_duration(output), 3.0, delta=0.1)

    def test_build_slideshow_video_falls_back_to_cuts_for_tiny_durations(self) -> None:
        with TemporaryDirectory(prefix="ffmpeg-xfade-fallback-") as tmp:
            tmp_dir = Path(tmp)
            ffmpeg_bin = resolve_ffmpeg_binary()
            paths = []
            for idx, color in enumerate(("red", "blue")):
                path = tmp_dir / f"frame{idx}.png"
                subprocess.run(
                    [
                        ffmpeg_bin,
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        f"color=c={color}:s=64x64",
                        "-frames:v",
                        "1",
                        str(path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                paths.append(path)
            output = tmp_dir / "fallback.mp4"

            build_slideshow_video(
                paths,
                duration_per_image=0.12,
                output_path=output,
                transitions=["fade"],
                transition_duration_s=0.4,
            )

            self.assertAlmostEqual(get_video_duration(output), 0.24, delta=0.08)


class TransitionSelectionTests(unittest.TestCase):
    def test_random_pool_is_subset_of_supported(self) -> None:
        self.assertTrue(set(XFADE_TRANSITIONS).issubset(SUPPORTED_TRANSITIONS))
        # SUPPORTED is the full xfade set; zoomin exists, zoomout does not.
        self.assertIn("zoomin", SUPPORTED_TRANSITIONS)
        self.assertNotIn("zoomout", SUPPORTED_TRANSITIONS)

    def test_parse_transition_spec_modes_single_and_list(self) -> None:
        self.assertEqual(parse_transition_spec("none"), ("none", None))
        self.assertEqual(parse_transition_spec("random"), ("random", None))
        self.assertEqual(parse_transition_spec("fade"), ("fade", None))
        self.assertEqual(parse_transition_spec(""), ("none", None))
        self.assertEqual(parse_transition_spec(None), ("none", None))
        # Comma list -> custom mode + normalized names (case/space-insensitive).
        self.assertEqual(
            parse_transition_spec("Fade, Dissolve , WipeLeft"),
            ("custom", ["fade", "dissolve", "wipeleft"]),
        )

    def test_normalize_transition_list_cycles_and_truncates(self) -> None:
        self.assertEqual(
            normalize_transition_list(["fade", "dissolve"], 4),
            ["fade", "dissolve", "fade", "dissolve"],
        )
        self.assertEqual(
            normalize_transition_list(["fade", "dissolve", "wipeleft"], 2),
            ["fade", "dissolve"],
        )
        self.assertEqual(normalize_transition_list(["fade"], 0), [])
        self.assertEqual(normalize_transition_list([], 3), [])

    def test_normalize_transition_list_rejects_unsupported(self) -> None:
        with self.assertRaises(ValueError):
            normalize_transition_list(["fade", "zoomout"], 2)

    def test_filter_complex_accepts_supported_non_pool_effect(self) -> None:
        # coverleft is a supported effect that is NOT in the random pool.
        self.assertIn("coverleft", SUPPORTED_TRANSITIONS)
        self.assertNotIn("coverleft", XFADE_TRANSITIONS)
        graph = _slideshow_filter_complex(
            sanitized_durations=[2.0, 2.0],
            fit_builder=_simple_fit,
            fps=30.0,
            transitions=["coverleft"],
            transition_durations=[0.4],
        )
        self.assertIn("xfade=transition=coverleft", graph)


class FilterGraphOrderingTests(unittest.TestCase):
    """Lock the per-image chain ordering that FFmpeg 7.x (n7.1) requires.

    xfade rejects non-constant-frame-rate inputs, and on 7.x a setpts after fps
    drops the frame-rate metadata. fps must therefore be the LAST filter of each
    per-image chain. These assert on the constructed graph so they hold
    regardless of the local ffmpeg version.
    """

    def _make_pngs(self, tmp_dir: Path, count: int) -> list[Path]:
        ffmpeg_bin = resolve_ffmpeg_binary()
        paths = []
        for idx in range(count):
            p = tmp_dir / f"f{idx}.png"
            subprocess.run(
                [ffmpeg_bin, "-y", "-f", "lavfi", "-i",
                 f"color=c=gray:s={90 + idx}x{80 + idx}", "-frames:v", "1", str(p)],
                check=True, capture_output=True, text=True,
            )
            paths.append(p)
        return paths

    def test_xfade_chain_ends_each_clip_with_fps(self) -> None:
        with TemporaryDirectory(prefix="fps-order-") as tmp:
            imgs = self._make_pngs(Path(tmp), 3)
            graph = _capture_filter_complex(
                imgs, duration_per_image=2.0,
                transitions=["fade", "circleopen"], transition_duration_s=0.4,
            )
            # fps is the final filter before each clip label; the broken order
            # (fps before trim) and settb must both be absent.
            self.assertIn("setpts=PTS-STARTPTS,fps=30[v0]", graph)
            self.assertIn("setpts=PTS-STARTPTS,fps=30[v1]", graph)
            self.assertNotIn("settb", graph)
            self.assertNotIn("fps=30,trim", graph)
            self.assertIn("xfade=transition=fade", graph)

    def test_concat_chain_also_ends_with_fps(self) -> None:
        with TemporaryDirectory(prefix="fps-order-concat-") as tmp:
            imgs = self._make_pngs(Path(tmp), 2)
            graph = _capture_filter_complex(imgs, duration_per_image=2.0)
            self.assertIn("setpts=PTS-STARTPTS,fps=30[v0]", graph)
            self.assertNotIn("fps=30,trim", graph)
            self.assertIn("concat=n=2", graph)


class FaststartSeekabilityTests(unittest.TestCase):
    def test_build_slideshow_video_moov_before_mdat(self) -> None:
        with TemporaryDirectory(prefix="ffmpeg-faststart-slideshow-") as tmp:
            tmp_dir = Path(tmp)
            ffmpeg_bin = resolve_ffmpeg_binary()
            img = tmp_dir / "frame.png"
            subprocess.run(
                [ffmpeg_bin, "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64",
                 "-frames:v", "1", str(img)],
                check=True, capture_output=True,
            )
            output = tmp_dir / "out.mp4"
            build_slideshow_video([img], duration_per_image=1.0, output_path=output)
            self.assertTrue(_moov_is_before_mdat(output),
                            "build_slideshow_video: moov atom must precede mdat for seekability")

    def test_mux_image_with_audio_moov_before_mdat(self) -> None:
        with TemporaryDirectory(prefix="ffmpeg-faststart-mux-") as tmp:
            tmp_dir = Path(tmp)
            ffmpeg_bin = resolve_ffmpeg_binary()
            img = tmp_dir / "frame.png"
            audio = tmp_dir / "tone.aac"
            subprocess.run(
                [ffmpeg_bin, "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x64",
                 "-frames:v", "1", str(img)],
                check=True, capture_output=True,
            )
            subprocess.run(
                [ffmpeg_bin, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                 "-c:a", "aac", str(audio)],
                check=True, capture_output=True,
            )
            output = tmp_dir / "out.mp4"
            mux_image_with_audio(img, audio, output)
            self.assertTrue(_moov_is_before_mdat(output),
                            "mux_image_with_audio: moov atom must precede mdat for seekability")

    def test_overlay_music_on_video_ignores_embedded_thumbnail_stream(self) -> None:
        # Regression: phone/app exports embed a second video stream (a single-frame
        # mjpeg thumbnail). Mapping all video streams (0:v) + -shortest truncated the
        # remix to that ~0-duration thumbnail, producing a silent video. The mux must
        # map only the primary video stream so the full music track survives.
        with TemporaryDirectory(prefix="ffmpeg-thumbnail-") as tmp:
            tmp_dir = Path(tmp)
            ffmpeg_bin = resolve_ffmpeg_binary()
            ffprobe_bin = ffmpeg_bin[:-6] + "ffprobe" if ffmpeg_bin.endswith("ffmpeg") else "ffprobe"
            # A source video with THREE streams: h264 (3s) + aac (3s) + mjpeg thumbnail
            # (1 frame), built in two steps so the short thumbnail doesn't truncate the
            # full-length main streams.
            main = tmp_dir / "main.mp4"
            subprocess.run(
                [ffmpeg_bin, "-y",
                 "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=10:duration=3",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(main)],
                check=True, capture_output=True,
            )
            thumb = tmp_dir / "thumb.png"
            subprocess.run(
                [ffmpeg_bin, "-y", "-f", "lavfi", "-i", "color=c=red:size=32x32",
                 "-frames:v", "1", str(thumb)],
                check=True, capture_output=True,
            )
            source = tmp_dir / "source_with_thumb.mp4"
            subprocess.run(
                [ffmpeg_bin, "-y", "-i", str(main), "-i", str(thumb),
                 "-map", "0:v:0", "-map", "0:a:0", "-map", "1:v:0",
                 "-c:v:0", "copy", "-c:a", "copy", "-c:v:1", "mjpeg",
                 "-disposition:v:1", "0", str(source)],
                check=True, capture_output=True,
            )
            music = tmp_dir / "music.wav"   # pcm/.wav forces the re-encode branch
            subprocess.run(
                [ffmpeg_bin, "-y", "-f", "lavfi", "-i", "sine=frequency=660:duration=3",
                 "-c:a", "pcm_s16le", str(music)],
                check=True, capture_output=True,
            )
            output = tmp_dir / "out.mp4"
            overlay_music_on_video(source, music, output,
                                   music_volume=1.0, preserve_original_audio=False)

            def _audio_duration(path: Path) -> float:
                res = subprocess.run(
                    [ffprobe_bin, "-v", "error", "-select_streams", "a",
                     "-show_entries", "stream=duration", "-of", "default=nk=1:nw=1", str(path)],
                    check=True, capture_output=True, text=True,
                )
                return float((res.stdout.strip().splitlines() or ["0"])[0])

            # The muxed audio must span the whole track (~3s), not be truncated to the
            # thumbnail's ~0 duration.
            self.assertGreaterEqual(
                _audio_duration(output), 2.5,
                "remixed audio was truncated (embedded thumbnail stream leaked into the mux)",
            )

    def test_overlay_music_on_video_reencodes_non_h264_source_to_h264(self) -> None:
        # Delivered videos must always carry an H.264 stream: a source that
        # bypassed upstream normalization (e.g. compression opt-out) must be
        # re-encoded during the mux instead of stream-copied. Odd dimensions
        # on purpose — the mux re-encode must coerce them even for libx264.
        with TemporaryDirectory(prefix="ffmpeg-h264-guarantee-") as tmp:
            tmp_dir = Path(tmp)
            ffmpeg_bin = resolve_ffmpeg_binary()
            ffprobe_bin = ffmpeg_bin[:-6] + "ffprobe" if ffmpeg_bin.endswith("ffmpeg") else "ffprobe"
            video = tmp_dir / "input.mp4"
            music = tmp_dir / "music.aac"
            subprocess.run(
                [ffmpeg_bin, "-y", "-f", "lavfi",
                 "-i", "color=c=green:size=97x99:rate=1:duration=2",
                 "-an", "-c:v", "mpeg4", "-pix_fmt", "yuv420p", str(video)],
                check=True, capture_output=True,
            )
            subprocess.run(
                [ffmpeg_bin, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                 "-c:a", "aac", str(music)],
                check=True, capture_output=True,
            )
            output = tmp_dir / "out.mp4"
            overlay_music_on_video(video, music, output)

            res = subprocess.run(
                [ffprobe_bin, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(output)],
                check=True, capture_output=True, text=True,
            )
            self.assertEqual(res.stdout.strip().lower(), "h264")

    def test_overlay_music_on_video_moov_before_mdat(self) -> None:
        with TemporaryDirectory(prefix="ffmpeg-faststart-overlay-") as tmp:
            tmp_dir = Path(tmp)
            ffmpeg_bin = resolve_ffmpeg_binary()
            video = tmp_dir / "input.mp4"
            music = tmp_dir / "music.aac"
            subprocess.run(
                [ffmpeg_bin, "-y", "-f", "lavfi",
                 "-i", "color=c=green:size=64x64:rate=1:duration=2",
                 "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)],
                check=True, capture_output=True,
            )
            subprocess.run(
                [ffmpeg_bin, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                 "-c:a", "aac", str(music)],
                check=True, capture_output=True,
            )
            output = tmp_dir / "out.mp4"
            overlay_music_on_video(video, music, output)
            self.assertTrue(_moov_is_before_mdat(output),
                            "overlay_music_on_video: moov atom must precede mdat for seekability")


class ExtractAudioWindowTests(unittest.TestCase):
    def _make_source(self, path: Path, *, duration: float) -> None:
        ffmpeg_bin = resolve_ffmpeg_binary()
        subprocess.run(
            [ffmpeg_bin, "-y", "-f", "lavfi",
             "-i", f"sine=frequency=440:duration={duration}",
             "-c:a", "libmp3lame", "-b:a", "128k", str(path)],
            check=True, capture_output=True,
        )

    def test_cuts_requested_window_and_stays_compact(self) -> None:
        with TemporaryDirectory(prefix="ffmpeg-audio-window-") as tmp:
            tmp_dir = Path(tmp)
            source = tmp_dir / "track.mp3"
            self._make_source(source, duration=20.0)
            clip = tmp_dir / "track_window.mp3"

            extract_audio_window(source, clip, start_s=5.0, duration_s=6.0)

            self.assertTrue(clip.exists())
            # Window length matches the request, not the full track.
            self.assertAlmostEqual(get_video_duration(clip), 6.0, delta=0.3)
            # Stream copy preserves the compressed codec, so a 6s slice of a 20s
            # track is markedly smaller than the source (not bloated to PCM).
            self.assertLess(clip.stat().st_size, source.stat().st_size)

    def test_no_duration_runs_to_end(self) -> None:
        with TemporaryDirectory(prefix="ffmpeg-audio-window-end-") as tmp:
            tmp_dir = Path(tmp)
            source = tmp_dir / "track.mp3"
            self._make_source(source, duration=12.0)
            clip = tmp_dir / "tail.mp3"

            extract_audio_window(source, clip, start_s=4.0)

            self.assertTrue(clip.exists())
            # From 4s to the end of a 12s track ~= 8s.
            self.assertAlmostEqual(get_video_duration(clip), 8.0, delta=0.4)


if __name__ == "__main__":
    unittest.main()

class FfmpegRunnerGuardTests(unittest.TestCase):
    """The runner must be un-wedgeable: no stdin reads, no unbounded waits."""

    def _run(self, cmd, **kwargs):
        captured = {}
        def fake_run(actual_cmd, **actual_kwargs):
            captured["cmd"] = list(actual_cmd)
            captured["kwargs"] = actual_kwargs
            return subprocess.CompletedProcess(actual_cmd, 0)
        with patch.object(ffmpeg_utils.subprocess, "run", side_effect=fake_run):
            ffmpeg_utils._run_ffmpeg_cmd(cmd, **kwargs)
        return captured

    def test_ffmpeg_gets_nostdin_and_a_timeout(self):
        got = self._run(["/usr/bin/ffmpeg", "-y", "-i", "in.mp4", "out.mp4"])
        self.assertEqual(got["cmd"][1], "-nostdin")
        self.assertGreater(got["kwargs"].get("timeout", 0), 0)

    def test_ffprobe_never_gets_nostdin(self):
        """ffprobe exits 1 on -nostdin; adding it broke every duration probe."""
        got = self._run(["/usr/bin/ffprobe", "-v", "error", "in.mp4"])
        self.assertNotIn("-nostdin", got["cmd"])
        self.assertGreater(got["kwargs"].get("timeout", 0), 0)

    def test_caller_timeout_wins(self):
        got = self._run(["ffmpeg", "-y", "out.mp4"], timeout=42.0)
        self.assertEqual(got["kwargs"]["timeout"], 42.0)

    def test_nostdin_not_duplicated(self):
        got = self._run(["ffmpeg", "-nostdin", "-y", "out.mp4"])
        self.assertEqual(got["cmd"].count("-nostdin"), 1)


class PadAudioToPictureTests(unittest.TestCase):
    """The pad is bounded by the picture and disappears when unmeasurable."""

    def test_pad_is_bounded_by_the_video_duration(self):
        with patch.object(ffmpeg_utils, "get_video_duration", return_value=137.021):
            self.assertEqual(
                ffmpeg_utils._pad_audio_to_picture(Path("v.mp4")),
                ",apad=whole_dur=137.021",
            )

    def test_unreadable_duration_drops_the_pad_not_the_graph(self):
        with patch.object(ffmpeg_utils, "get_video_duration", side_effect=RuntimeError):
            self.assertEqual(ffmpeg_utils._pad_audio_to_picture(Path("v.mp4")), "")

