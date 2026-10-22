"""Tier 0 기본 디코더 CPU, NVDEC 은 선택 (S15P31A101-236, docs/design/tier0-nvdec-throughput.md '결론').

- 기본: LIVE·file 입력에서 input.decoder 를 생략하면 cpu
- 선택: "nvdec" 를 적으면 그대로 nvdec(기본이 바뀌어도 선택은 남는다)
- 반례: 모르는 값(대소문자 다름, "gpu", 빈 문자열, null, 숫자)은 기본값으로 바꾸지 않고 설정 오류
- 반례: 기본 설정의 디코더 공장은 NVDEC 을 한 번도 열지 않는다(GPU 가 없어도 기본 경로가 선다)
- 반례: nvdec 을 고른 설정은 NVDEC 이 없을 때 CPU 로 몰래 떨어지지 않는다(공장 단위, LIVE 기동은 test_tier0_live_e2e)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

pytest.importorskip("av")

import services.tier0.service as service_module  # noqa: E402
from services.tier0.settings import DECODERS, DEFAULT_DECODER, Tier0ConfigError, load_settings  # noqa: E402
from simai_ai.io import DecoderUnavailable, PyAvDecoder  # noqa: E402

CAL = AI_DIR.parent / "contracts" / "examples" / "calibration.json"


def _load(tmp_path: Path, input_doc: dict):
    cfg = tmp_path / "tier0.json"
    cfg.write_text(json.dumps({"state_dir": str(tmp_path / "state"), "calibration_file": str(CAL), "input": input_doc}),
                   encoding="utf-8")
    return load_settings(cfg, env={})


def _live(**extra) -> dict:
    return {"mode": "live", "camera_ids": ["cam1", "cam2"], **extra}


def _file(**extra) -> dict:
    return {"mode": "file", "session_dir": "rec", **extra}


def test_default_decoder_is_cpu_for_live_and_file(tmp_path) -> None:
    assert DEFAULT_DECODER == "cpu" and set(DECODERS) == {"cpu", "nvdec"}
    assert _load(tmp_path, _live()).live.decoder == "cpu"
    assert _load(tmp_path, _file()).file.decoder == "cpu"


@pytest.mark.parametrize("make", [_live, _file])
@pytest.mark.parametrize("kind", ["nvdec", "cpu"])
def test_explicit_decoder_is_kept(tmp_path, make, kind) -> None:
    s = _load(tmp_path, make(decoder=kind))
    assert (s.live or s.file).decoder == kind


@pytest.mark.parametrize("make", [_live, _file])
@pytest.mark.parametrize("bad", ["NVDEC", "CPU", "gpu", "cuda", "", None, 0, True])
def test_unknown_decoder_is_rejected_not_defaulted(tmp_path, make, bad) -> None:
    with pytest.raises(Tier0ConfigError, match="input.decoder"):
        _load(tmp_path, make(decoder=bad))


def test_default_factory_never_opens_nvdec(tmp_path, monkeypatch) -> None:
    def no_nvdec(*_a, **_k):
        raise AssertionError("기본 디코더가 NVDEC 을 열었다")

    monkeypatch.setattr(service_module, "NvdecDecoder", no_nvdec)
    s = _load(tmp_path, _live())
    make = service_module.Tier0Service._decoder_factory(s.live.decoder)
    decs = [make(c) for c in ("cam1", "cam2")]
    assert all(type(d) is PyAvDecoder and d.hwaccel is None for d in decs)
    assert decs[0] is not decs[1]  # 카메라마다 디코더 하나(프레임 의존성·변환 문맥을 나눠 쓰지 않는다)


def test_chosen_nvdec_does_not_fall_back_to_cpu(tmp_path, monkeypatch) -> None:
    def no_nvdec(*_a, **_k):
        raise DecoderUnavailable("hwaccel 'cuda' 장치를 열지 못했다")

    monkeypatch.setattr(service_module, "NvdecDecoder", no_nvdec)
    s = _load(tmp_path, _live(decoder="nvdec"))
    make = service_module.Tier0Service._decoder_factory(s.live.decoder)
    with pytest.raises(DecoderUnavailable):
        make("cam1")
