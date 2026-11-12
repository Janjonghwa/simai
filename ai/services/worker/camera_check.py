"""세팅 「카메라 확인」 실행기(S15P31A101-646): 같은 Pod 의 Tier 0 가 넘긴 view 로 새 보정 후보를 확인한다.

`start` 가 `<state>/camera-check/request.json` 을 쓰면 Tier 0(`services.tier0.camera_check_tap`)가 관제 경로의 공통 시각 view 를
`sessions/<setup_session_id>/views.jsonl` 에 덧붙인다. `poll` 은 새 줄만 읽어 `simai_ai.calibration.camera_check` 평가기에 넣고
계약 site_setup#/$defs/camera_check 요약을 돌려준다. 통과하면 검증된 기준선을 `baselines/` 에 써 두어 Tier 0 가 그 보정을
채택할 때(세팅 완료 C17 의 구성 묶음) 틀어짐 감시 기준선으로 쓴다. 끝나거나 멈추면 요청 파일을 지워 Tier 0 가 쓰기를 멈춘다.
조립과 파일 입출력만 하고 판정은 평가기가 한다.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from simai_ai.calibration.camera_check import (
    DEFAULT_CONFIG,
    FINAL_STATUSES,
    PASSED,
    CameraCheckConfig,
    CameraCheckEvaluator,
    save_baseline,
    views_from_json,
)

log = logging.getLogger("services.worker.camera_check")

REQUEST_FILE = "request.json"
MAX_LINES_PER_POLL = 2_000


class CameraCheckRunner:
    """세팅 작업 하나(대여 하나)의 카메라 확인. 한 번에 한 세션만 연다(같은 Pod 의 워커는 작업을 하나씩 돈다)."""

    def __init__(self, root: Path, *, clock_ms: Callable[[], int], config: CameraCheckConfig = DEFAULT_CONFIG) -> None:
        self.root = Path(root)
        self._clock_ms = clock_ms
        self.config = config
        self._evaluator: CameraCheckEvaluator | None = None
        self._session: str | None = None
        self._offset = 0
        self._partial = b""
        self.bad_lines = 0

    @property
    def active(self) -> bool:
        return self._evaluator is not None

    def _views_path(self, session: str) -> Path:
        return self.root / "sessions" / session / "views.jsonl"

    def start(self, setup_session_id: str, calibration_doc: Mapping[str, Any], *, expires_at_ms: int,
              required_pairs: Iterable[tuple[str, str]] = ()) -> dict[str, Any]:
        """새 보정 후보로 확인을 연다. 지난 같은 세션 view 는 지운다(재대여는 새 보정이라 섞지 않는다)."""
        self._evaluator = CameraCheckEvaluator(calibration_doc, self.config, required_pairs=required_pairs)
        self._session = setup_session_id
        self._offset = 0
        self._partial = b""
        self.bad_lines = 0
        session_dir = self.root / "sessions" / setup_session_id
        shutil.rmtree(session_dir, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        request = {
            "setup_session_id": setup_session_id,
            "calibration_version": self._evaluator.calibration_version,
            "requested_at_ms": int(self._clock_ms()),
            "expires_at_ms": int(expires_at_ms),
        }
        tmp = self.root / f"{REQUEST_FILE}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.root / REQUEST_FILE)
        log.info("카메라 확인 시작: 세션 %s, 보정 %s", setup_session_id, self._evaluator.calibration_version)
        return self._evaluator.summary()

    def _read_new_lines(self) -> list[bytes]:
        assert self._session is not None
        path = self._views_path(self._session)
        try:
            with path.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
        except OSError:
            return []
        self._offset += len(chunk)
        data = self._partial + chunk
        lines = data.split(b"\n")
        self._partial = lines.pop()  # 마지막 줄은 아직 쓰는 중일 수 있다
        return lines

    def poll(self) -> dict[str, Any] | None:
        """새 view 를 평가기에 넣고 요약을 돌려준다. 열려 있지 않으면 None. 통과하면 기준선을 쓰고 요청을 닫는다."""
        evaluator = self._evaluator
        if evaluator is None:
            return None
        if not evaluator.final:
            for raw in self._read_new_lines()[:MAX_LINES_PER_POLL]:
                if not raw.strip():
                    continue
                try:
                    t, views = views_from_json(json.loads(raw))
                except ValueError:
                    self.bad_lines += 1
                    continue
                evaluator.observe(t, views)
                if evaluator.final:
                    break
        summary = evaluator.summary()
        if summary["status"] in FINAL_STATUSES:
            self._close(summary)
        return summary

    def incomplete(self) -> dict[str, Any] | None:
        """작업 마감까지 판정하지 못했다. INCOMPLETE 로 고정하고 닫는다."""
        if self._evaluator is None:
            return None
        self.poll()
        summary = self._evaluator.incomplete()
        self._close(summary)
        return summary

    def _close(self, summary: Mapping[str, Any]) -> None:
        if summary["status"] == PASSED and self._evaluator is not None:
            try:
                path = save_baseline(self.root, self._evaluator.baseline_record())
                log.info("카메라 확인 통과: 틀어짐 감시 기준선 %s", path)
            except OSError as exc:
                log.warning("카메라 확인 기준선을 쓰지 못했다(관제는 즉석 학습으로 감시한다): %s", exc)
        elif summary["status"] in FINAL_STATUSES:
            log.warning("카메라 확인 %s: %s", summary["status"], [p.get("message") for p in summary.get("problems", [])])
        self.stop()

    def stop(self) -> None:
        """요청 파일을 지워 Tier 0 가 view 쓰기를 멈추게 한다(평가 결과는 남긴다)."""
        try:
            (self.root / REQUEST_FILE).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("카메라 확인 요청 파일을 지우지 못했다: %s", exc)


def evaluate_file(views_path: Path, calibration_doc: Mapping[str, Any], *, config: CameraCheckConfig = DEFAULT_CONFIG,
                  required_pairs: Iterable[tuple[str, str]] = ()) -> dict[str, Any]:
    """녹화 재생으로 모은 views.jsonl 을 GPU 없이 다시 평가한다(설계 문서 '녹화 재생 검증'). 끝까지 판정이 안 나면 INCOMPLETE."""
    evaluator = CameraCheckEvaluator(calibration_doc, config, required_pairs=required_pairs)
    with Path(views_path).open(encoding="utf-8") as fh:
        for raw in fh:
            if not raw.strip():
                continue
            try:
                t, views = views_from_json(json.loads(raw))
            except ValueError:
                continue
            evaluator.observe(t, views)
            if evaluator.final:
                break
    summary = evaluator.summary()
    return summary if summary["status"] in FINAL_STATUSES else evaluator.incomplete()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="세팅 카메라 확인을 views.jsonl 로 다시 평가한다(S15P31A101-646)")
    parser.add_argument("--views", type=Path, required=True, help="Tier 0 가 쓴 sessions/<id>/views.jsonl")
    parser.add_argument("--calibration", type=Path, required=True, help="평가할 보정 문서(JSON)")
    parser.add_argument("--required-pair", action="append", default=[], metavar="A,B",
                        help="세팅 품질 게이트 NEEDS_CHECK 쌍(카메라 ID 둘을 쉼표로)")
    args = parser.parse_args(argv)
    doc = json.loads(args.calibration.read_text(encoding="utf-8"))
    pairs = [tuple(p.split(",", 1)) for p in args.required_pair]
    summary = evaluate_file(args.views, doc, required_pairs=pairs)  # type: ignore[arg-type]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == PASSED else 1


if __name__ == "__main__":
    raise SystemExit(main())
