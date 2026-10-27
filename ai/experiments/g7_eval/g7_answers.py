"""G7 답 생성 (GPU 세션, S15P31A101-35, 설계 docs/design/g7-eval.md).

표본 묶음(g7_samples.py)의 표본마다 프레임을 270 관문(`prepare_vlm_frames`, 얼굴 처리)에 넣고, 나온 `VlmFrame` 만
22 클라이언트(`VlmClient`)로 같은 Pod 의 vLLM 에 보낸다. 답·지연·토큰을 answers.jsonl 에 한 줄씩 남긴다.

- 프롬프트는 주입한다(`--prompt 모듈:이름`, `Prompt` 꼴). 근거 설명 프롬프트·JSON 스키마는 S15P31A101-25 몫이다.
  25 전에는 자리 표시 프롬프트(PLACEHOLDER)만 쓰고, 그 답으로는 G7 판정을 내지 않는다(answers 의 prompt.placeholder).
- 채점표에 보일 흐린 그림(`VlmFrame.pixels`)을 blurred/<sample_id>/ 에 쓴다. 사람이 찍힌 그림이라 커밋하지 않는다.
- LIVE 관제 시간에 돌리지 않는다(오프라인 EXP Job, AGENTS 작업 방식). Tier 0 와 같이 띄운 Pod 면 vLLM 동시 1 로만 부른다.

    python experiments/g7_eval/g7_answers.py --bundle <묶음> --out <답 폴더> --pose-model <yolo26x-pose 경로> [--prompt 모듈:이름]
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from simai_ai.vlm.client import VlmClient, VlmError, VlmSettings
from simai_ai.vlm.face_blur import KeypointProvider, prepare_vlm_frames, summarize

FORMAT = "simai.g7_answers"
ANSWERS_FILE = "answers.jsonl"


@dataclass(frozen=True)
class PromptParts:
    text: str
    system: str | None = None
    response_format: Mapping[str, Any] | None = None


class Prompt(Protocol):
    prompt_id: str
    placeholder: bool

    def build(self, sample_input: Mapping[str, Any]) -> PromptParts:
        """표본 입력(vlm_rationale input 모양, frames 제외 값) → 질문. 프레임은 클라이언트가 붙인다."""
        ...


@dataclass(frozen=True)
class _Placeholder:
    """도구 시험·리허설용. 25 의 프롬프트가 아니다."""

    prompt_id: str = "g7-placeholder-0"
    placeholder: bool = True
    keys: tuple[str, ...] = field(default=("rule_id", "severity", "confidence_reasons", "zone_id", "metrics", "structured_state"))

    def build(self, sample_input: Mapping[str, Any]) -> PromptParts:
        facts = {k: sample_input.get(k) for k in self.keys}
        return PromptParts(text="다음 구조화 상태와 그림을 보고 위험 근거를 한국어 두 문장으로 설명하라. 등급은 바꾸지 않는다.\n"
                                + json.dumps(facts, ensure_ascii=False, sort_keys=True))


PLACEHOLDER = _Placeholder()


def load_prompt(spec: str) -> Prompt:
    """'placeholder' 또는 '모듈:이름'(설치된 패키지, 예: simai_ai.vlm.rationale:PROMPT)."""
    if spec == "placeholder":
        return PLACEHOLDER
    module, _, name = spec.partition(":")
    if not module or not name:
        raise ValueError("--prompt 는 placeholder 또는 모듈:이름")
    prompt = getattr(importlib.import_module(module), name)
    for attr in ("prompt_id", "placeholder", "build"):
        if not hasattr(prompt, attr):
            raise ValueError(f"{spec}: Prompt 에 {attr} 이 없다")
    return prompt


def _decode(data: bytes):
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("이미지를 디코드하지 못했다")
    return image


def run(bundle: Path, out: Path, client: VlmClient, prompt: Prompt, provider: KeypointProvider, *,
        decode: Callable[[bytes], Any] = _decode, save_blurred: Callable[[Path, Any], None] | None = None,
        only: set[str] | None = None) -> dict[str, Any]:
    """표본마다 한 번 부른다(동시 1). 실패도 한 줄로 남긴다(다음 표본으로 넘어간다)."""
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    path = out / ANSWERS_FILE
    stats = {"ok": 0, "error": 0, "no_frames": 0}
    with path.open("x", encoding="utf-8", newline="\n") as f:  # 있으면 덮어쓰지 않는다
        for entry in manifest["samples"]:
            sid = entry["sample_id"]
            if only and sid not in only:
                continue
            sdir = bundle / "samples" / sid
            inp = json.loads((sdir / "input.json").read_text(encoding="utf-8"))
            items = [((sdir / fr["file"]).as_posix(), (sdir / fr["file"]).read_bytes()) for fr in inp["frames"]]
            policy = inp["frames"][0]["face_policy"] if inp["frames"] else "BLURRED"
            gate = prepare_vlm_frames(items, policy, provider, decode=decode)
            row: dict[str, Any] = {"sample_id": sid, "prompt": {"id": prompt.prompt_id, "placeholder": prompt.placeholder},
                                   "gate": summarize(gate), "content": None, "error": None}
            if not gate.frames:
                row["error"] = {"code": "NO_FACE_SAFE_FRAMES", "retryable": False}
                stats["no_frames"] += 1
            else:
                if save_blurred is not None:
                    for k, fr in enumerate(gate.frames):
                        save_blurred(out / "blurred" / sid / f"{k:02d}.jpg", fr.pixels)
                parts = prompt.build({k: v for k, v in inp.items() if k != "frames"})
                t0 = time.time()
                try:
                    r = client.complete(parts.text, gate.frames, system=parts.system, response_format=parts.response_format)
                except VlmError as e:
                    row["error"] = {"code": e.code, "retryable": e.retryable, "message": str(e)[:300]}
                    row["latency_ms"] = round((time.time() - t0) * 1000)
                    stats["error"] += 1
                else:
                    row.update(content=r.content, model=r.model, latency_ms=r.latency_ms, input_tokens=r.input_tokens,
                               output_tokens=r.output_tokens, finish_reason=r.finish_reason)
                    stats["ok"] += 1
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
    meta = {"format": FORMAT, "bundle": manifest.get("source"), "product_metric": manifest.get("product_metric"),
            "prompt": {"id": prompt.prompt_id, "placeholder": prompt.placeholder}, **stats}
    (out / "run.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--pose-model", required=True, help="얼굴 처리용 pose 가중치(.pt 또는 .engine)")
    p.add_argument("--prompt", default="placeholder")
    a = p.parse_args(argv)
    import cv2

    from simai_ai.calibration.extrinsics.extract import UltralyticsPose
    from simai_ai.vlm.face_blur import PoseDetectorProvider

    def save(path: Path, bgr) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(path), bgr, [cv2.IMWRITE_JPEG_QUALITY, 90]):
            raise OSError(f"{path}: JPEG 를 쓰지 못했다")

    meta = run(a.bundle, a.out, VlmClient(VlmSettings.from_env(os.environ)), load_prompt(a.prompt),
               PoseDetectorProvider(UltralyticsPose(a.pose_model)), save_blurred=save)
    print(json.dumps(meta, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
