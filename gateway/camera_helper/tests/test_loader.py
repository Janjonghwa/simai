import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from simai_camera_helper.judge import Params
from simai_camera_helper.matcher import CameraMatcher


class LoaderTests(unittest.TestCase):
    def test_missing_weights_fail_before_torch_import(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "missing.pth"
            with patch.dict(sys.modules, {"torch": None, "lightglue": None}):
                with self.assertRaisesRegex(FileNotFoundError, "automatic download is disabled"):
                    CameraMatcher(missing, missing)

    def test_empty_weights_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            empty = Path(folder) / "empty.pth"
            empty.touch()
            with self.assertRaisesRegex(ValueError, "weights are empty"):
                CameraMatcher(empty, empty)

    def test_explicit_local_weights_replace_hub_loader_and_restore_it(self):
        network = Mock(side_effect=AssertionError("network must never be used"))
        hub = types.SimpleNamespace(load_state_dict_from_url=network)
        loaded = []

        def load(path, **kwargs):
            loaded.append((path.name, kwargs))
            return {"local": path.name}

        torch = types.SimpleNamespace(hub=hub, load=load, set_num_threads=Mock(),
                                      cuda=types.SimpleNamespace(is_available=lambda: False))

        class Model:
            def eval(self):
                return self

            def to(self, device):
                assert device == "cpu"
                return self

        class Aliked(Model):
            checkpoint_url = "https://example.invalid/{}.pth"

            def __init__(self, **kwargs):
                self.state = torch.hub.load_state_dict_from_url(self.checkpoint_url.format("aliked-n16"))

        class Glue(Model):
            version = "v0.1_arxiv"
            url = "https://example.invalid/{}/{}.pth"

            def __init__(self, **kwargs):
                self.conf = types.SimpleNamespace(n_layers=0)
                self.state = torch.hub.load_state_dict_from_url(self.url.format(self.version, "aliked_lightglue"))

            def load_state_dict(self, state, strict):
                assert not strict
                assert state["local"] == "matcher.pth"
                return types.SimpleNamespace(missing_keys=["confidence_thresholds"], unexpected_keys=[])

        modules = {"torch": torch, "cv2": types.SimpleNamespace(setNumThreads=Mock()),
                   "lightglue": types.SimpleNamespace(ALIKED=Aliked, LightGlue=Glue)}
        with tempfile.TemporaryDirectory() as folder:
            a, b = Path(folder) / "extractor.pth", Path(folder) / "matcher.pth"
            a.write_bytes(b"stub")
            b.write_bytes(b"stub")
            with patch.dict(sys.modules, modules):
                CameraMatcher(a, b, Params(intrinsics_source="DEFAULT"))
        self.assertIs(torch.hub.load_state_dict_from_url, network)
        network.assert_not_called()
        self.assertEqual([name for name, _ in loaded], ["extractor.pth", "matcher.pth"])
        self.assertTrue(all(kw == {"map_location": "cpu", "weights_only": True} for _, kw in loaded))


if __name__ == "__main__":
    unittest.main()
