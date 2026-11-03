import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from simai_camera_helper.offline import digest, parser, percentile, run, selected_keyframes


class OfflineTests(unittest.TestCase):
    def test_existing_results_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "summary.json"
            report.write_text("original", encoding="utf-8")
            args = parser().parse_args(["--out", folder])
            with self.assertRaisesRegex(FileExistsError, "use a new --out"):
                run(args)
            self.assertEqual(report.read_text(encoding="utf-8"), "original")

    def test_matrix_does_not_reuse_previous_outputs(self):
        from simai_camera_helper.offline import main

        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "benchmark.json").write_text("original", encoding="utf-8")
            with patch.object(sys, "argv", ["helper", "--out", folder, "--matrix"]), \
                    patch("simai_camera_helper.offline.subprocess.run") as child, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                main()
            self.assertEqual(exc.exception.code, 2)
            child.assert_not_called()
            self.assertEqual((Path(folder) / "benchmark.json").read_text(encoding="utf-8"), "original")

    def test_failure_still_writes_auditable_run_record(self):
        with tempfile.TemporaryDirectory() as folder:
            args = parser().parse_args(["--out", folder])
            with patch("simai_camera_helper.offline.PeakMemory", side_effect=RuntimeError("not installed")), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run(args), 1)
            report = Path(folder) / "summary.json"
            record = json.loads((Path(folder) / "run_record.json").read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "FAILED")
            self.assertEqual(record["metrics"], {})
            self.assertIn("RuntimeError: not installed", record["failure_reason"])
            self.assertEqual(record["results"][0]["sha256"], digest(report))
            self.assertEqual(record["input_provenance"]["frames"]["source"], "FILE")

    def test_percentile_interpolation(self):
        self.assertEqual(percentile([10, 0, 20], 0.5), 10)
        self.assertEqual(percentile([10, 0, 20], 0.95), 19)
        self.assertEqual(percentile([3], 0.95), 3)
        with self.assertRaises(ValueError):
            percentile([], 0.5)

    def test_selected_keyframes_only_convert_selected_and_close_early(self):
        converted = []
        closed = []
        context = types.SimpleNamespace(skip_frame=None, thread_count=None)
        stream = types.SimpleNamespace(codec_context=context)

        class Frame:
            key_frame = True

            def __init__(self, index):
                self.index = index

            def to_ndarray(self, format):
                converted.append(self.index)
                return self.index

        class Container:
            streams = types.SimpleNamespace(video=[stream])

            def __enter__(self):
                return self

            def __exit__(self, *_):
                closed.append(True)

            def decode(self, requested_stream):
                # A very long source must not be exhausted to produce the first batch.
                for index in range(1_000_000):
                    yield Frame(index)

        with patch.dict(sys.modules, {"av": types.SimpleNamespace(open=lambda *a, **k: Container())}):
            frames = selected_keyframes(Path("test.h264"), start=1, stride=3)
            self.assertEqual(next(frames), 1)
            self.assertEqual(next(frames), 4)
            frames.close()
        self.assertEqual(converted, [1, 4])
        self.assertEqual(context.skip_frame, "NONKEY")
        self.assertEqual(context.thread_count, 1)
        self.assertEqual(closed, [True])

    def test_failed_run_records_all_four_cpu_matrix_combinations(self):
        # No inference on missing dependencies: verify failure accounting still produces reviewable records.
        from simai_camera_helper.offline import main

        calls = []

        def child(command, check):
            calls.append(command)
            return types.SimpleNamespace(returncode=1)

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(sys, "argv", ["helper", "--out", folder, "--matrix"]), \
                    patch("simai_camera_helper.offline.subprocess.run", side_effect=child), self.assertRaises(SystemExit) as exc:
                main()
            self.assertEqual(exc.exception.code, 1)
            result = json.loads((Path(folder) / "benchmark.json").read_text(encoding="utf-8"))
        self.assertEqual([(r["image_width_px"], r["max_keypoints"]) for r in result],
                         [(960, 1024), (960, 2048), (1024, 1024), (1024, 2048)])
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(r["status"] == "FAILED" for r in result))


if __name__ == "__main__":
    unittest.main()
