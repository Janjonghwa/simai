"""세팅 「카메라 확인」 실행기(S15P31A101-646): 같은 Pod 의 Tier 0 가 넘긴 view 로 새 보정 후보를 확인한다.

`start` 가 `<state>/camera-check/request.json` 을 쓰면 Tier 0(`services.tier0.camera_check_tap`)가 관제 경로의 공통 시각 view 를
`sessions/<setup_session_id>/views.jsonl` 에 덧붙인다. `poll` 은 새 줄만 읽어 `simai_ai.calibration.camera_check` 평가기에 넣고
계약 site_setup#/$defs/camera_check 요약을 돌려준다. 통과하면 검증된 기준선을 `baselines/` 에 써 두어 Tier 0 가 그 보정을
채택할 때(세팅 완료 C17 의 구성 묶음) 틀어짐 감시 기준선으로 쓴다. 끝나거나 멈추면 요청 파일을 지워 Tier 0 가 쓰기를 멈춘다.
조립과 파일 입출력만 하고 판정은 평가기가 한다.

시도(S15P31A101-687): 같은 보정으로 「확인만 다시」 하면 `start` 를 새 `attempt_id` 로 다시 부른다. 요청 파일에 시도 ID 를 실으면
Tier 0 는 그 시도의 view 를 `sessions/<setup_session_id>/<attempt_id>/views.jsonl` 에 따로 적는다. 그래서 대기열에 남아 늦게 쓰인
이전 시도 view 가 새 시도에 섞이지 않는다. `lines_read` 는 받은 줄 수(사람 없는 빈 시각 포함)로, 시도를 열고도 줄이 하나도 오지
않으면 확인 채널(같은 Pod 의 Tier 0)이 없다는 뜻이다.

걷기 다듬기(S15P31A101-760): 시도가 기하 실패로 끝나면 세팅 처리기가 `refine_walk` 로 그 시도의 views.jsonl 전체를 읽어
`simai_ai.calibration.walk_refinement` 에 넘긴다(판정은 그 모듈이 한다). 기록은 같은 시도 폴더의 `walk_refinement.json` 에 남긴다.
처리기가 새 보정을 발행하면 `adopt_walk` 가 그 버전의 Holdout 기준선을 `baselines/` 에 쓴다. 다듬기를 채택하지 않으면 처리기가
`recheck_walk` 로 같은 시도 view 를 -759 규칙(품질 게이트 옆 쌍만 막고 대각은 참고, 카메라별 재투영 표본 대기)으로 다시 판정한다.
통과하면 지금 보정 그대로의 기준선을 쓰고 기록에 「대각 미해결로 -759 기준 통과」를 남긴다.
"""

from __future__ import annotations

import json
import logging
import os
import re
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
    evaluate_views,
    save_baseline,
    views_from_json,
)
from simai_ai.calibration.walk_refinement import WalkRefinementResult, refine_extrinsics_from_walk
from simai_ai.tracking.cross_view import LocalView

log = logging.getLogger("services.worker.camera_check")

REQUEST_FILE = "request.json"
MAX_LINES_PER_POLL = 2_000
ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # 계약 site_setup#/$defs/camera_check/attempt/id, Tier 0 탭과 같은 규칙


class CameraCheckRunner:
    """세팅 작업 하나(대여 하나)의 카메라 확인. 한 번에 한 세션만 연다(같은 Pod 의 워커는 작업을 하나씩 돈다)."""

    def __init__(self, root: Path, *, clock_ms: Callable[[], int], config: CameraCheckConfig = DEFAULT_CONFIG) -> None:
        self.root = Path(root)
        self._clock_ms = clock_ms
        self.config = config
        self._evaluator: CameraCheckEvaluator | None = None
        self._session: str | None = None
        self._attempt_id: str | None = None
        self._offset = 0
        self._partial = b""
        self.bad_lines = 0
        self.lines_read = 0
        self._walk_record: dict[str, Any] = {}

    @property
    def active(self) -> bool:
        return self._evaluator is not None

    def idle(self, calibration_doc: Mapping[str, Any], *, required_pairs: Iterable[tuple[str, str]] = (),
             blocking_pairs: Iterable[tuple[str, str]] = ()) -> dict[str, Any]:
        """시도를 열기 전(관리자 「걷기 시작」 대기) 화면에 보일 빈 요약. 요청 파일은 쓰지 않는다(Tier 0 는 아직 view 를 넘기지 않음)."""
        return CameraCheckEvaluator(calibration_doc, self.config, required_pairs=required_pairs,
                                    blocking_pairs=blocking_pairs).summary()

    def _views_path(self, session: str) -> Path:
        base = self.root / "sessions" / session
        return (base / self._attempt_id if self._attempt_id else base) / "views.jsonl"

    def start(self, setup_session_id: str, calibration_doc: Mapping[str, Any], *, expires_at_ms: int,
              required_pairs: Iterable[tuple[str, str]] = (), blocking_pairs: Iterable[tuple[str, str]] = (),
              attempt_id: str | None = None) -> dict[str, Any]:
        """새 보정 후보로 확인(시도 하나)을 연다. 지난 같은 세션 view 는 지운다(재대여는 새 보정이고, 다시 확인은 새 시도라 섞지 않는다).

        attempt_id 가 있으면 Tier 0 가 그 시도 폴더에 view 를 적는다(S15P31A101-687). 형식이 틀리면 ValueError."""
        if attempt_id is not None and not ATTEMPT_ID_RE.match(attempt_id):
            raise ValueError(f"카메라 확인 시도 ID 형식 오류: {attempt_id!r}")
        self._evaluator = CameraCheckEvaluator(calibration_doc, self.config, required_pairs=required_pairs,
                                               blocking_pairs=blocking_pairs)
        self._session = setup_session_id
        self._attempt_id = attempt_id
        self._offset = 0
        self._partial = b""
        self.bad_lines = 0
        self.lines_read = 0
        self._walk_record = {}
        session_dir = self.root / "sessions" / setup_session_id
        shutil.rmtree(session_dir, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        request = {
            "setup_session_id": setup_session_id,
            "calibration_version": self._evaluator.calibration_version,
            "requested_at_ms": int(self._clock_ms()),
            "expires_at_ms": int(expires_at_ms),
        }
        if attempt_id is not None:
            request["attempt_id"] = attempt_id
        tmp = self.root / f"{REQUEST_FILE}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.root / REQUEST_FILE)
        log.info("카메라 확인 시작: 세션 %s, 보정 %s, 시도 %s", setup_session_id, self._evaluator.calibration_version,
                 attempt_id or "-")
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
                self.lines_read += 1
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

    def refine_walk(self, calibration_doc: Mapping[str, Any], *, camera_heights_m: Mapping[str, float],
                    required_pairs: Iterable[tuple[str, str]] = (),
                    blocking_pairs: Iterable[tuple[str, str]] = ()) -> WalkRefinementResult | None:
        """마지막 시도의 views.jsonl 로 외부 파라미터 다듬기를 한 번 돌린다(S15P31A101-760). 연 시도가 없으면 None.

        calibration_doc 은 그 시도가 잰 보정(Stage A)이다. 결과 기록은 시도 폴더의 walk_refinement.json 에도 남긴다(쓰지 못해도 결과는 그대로)."""
        if self._session is None:
            return None
        path = self._views_path(self._session)
        result = refine_extrinsics_from_walk(calibration_doc, load_views(path), camera_heights_m=camera_heights_m,
                                             check_config=self.config, required_pairs=required_pairs,
                                             blocking_pairs=blocking_pairs)
        self._walk_record = dict(result.record)
        self._write_walk_record(path)
        return result

    def recheck_walk(self, calibration_doc: Mapping[str, Any], *, required_pairs: Iterable[tuple[str, str]] = (),
                     blocking_pairs: Iterable[tuple[str, str]] = ()) -> dict[str, Any] | None:
        """다듬기를 채택하지 않은 시도의 views.jsonl 전체를 주어진 막는 쌍(-759: 품질 게이트 옆 쌍)으로 다시 판정한다(-760 후속).

        PASSED 면 그 보정 버전 기준선을 쓴다. 결과는 walk_refinement.json 의 fallback_759 에 남긴다. 연 시도가 없으면 None."""
        if self._session is None:
            return None
        path = self._views_path(self._session)
        evaluation = evaluate_views(load_views(path), calibration_doc, self.config, required_pairs=required_pairs,
                                    blocking_pairs=blocking_pairs)
        summary = evaluation.summary
        passed = summary["status"] == PASSED
        self._walk_record["fallback_759"] = {
            "status": summary["status"],
            "calibration_version": summary["calibration_version"],
            "note": "대각 미해결로 -759 기준 통과" if passed else "-759 기준으로도 통과하지 못함",
            "problems": [p.get("reason") for p in summary.get("problems", [])],
            "pairs": {k: {"required": p["required"], "pass_rate": p["pass_rate"],
                          "residual_median_px": p["residual_median_px"], "verdict": p["verdict"]}
                      for k, p in summary.get("pairs", {}).items()},
        }
        self._write_walk_record(path)
        if passed and evaluation.baseline.values_px:
            try:
                log.info("카메라 확인 -759 기준 통과: 틀어짐 감시 기준선 %s", save_baseline(self.root, evaluation.baseline))
            except OSError as exc:
                log.warning("카메라 확인 기준선을 쓰지 못했다(관제는 즉석 학습으로 감시한다): %s", exc)
        return summary

    def _write_walk_record(self, views_path: Path) -> None:
        try:
            out = views_path.parent / "walk_refinement.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self._walk_record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            os.replace(tmp, out)
        except OSError as exc:
            log.warning("걷기 다듬기 기록을 쓰지 못했다: %s", exc)

    def adopt_walk(self, result: WalkRefinementResult) -> None:
        """채택한 다듬기의 Holdout 기준선을 쓴다(Tier 0 가 그 새 보정을 채택할 때 틀어짐 감시 기준선으로 읽는다)."""
        if result.baseline is None or not result.baseline.values_px:
            return
        try:
            path = save_baseline(self.root, result.baseline)
            log.info("걷기 다듬기 채택: 틀어짐 감시 기준선 %s", path)
        except OSError as exc:
            log.warning("걷기 다듬기 기준선을 쓰지 못했다(관제는 즉석 학습으로 감시한다): %s", exc)

    def stop(self) -> None:
        """요청 파일을 지워 Tier 0 가 view 쓰기를 멈추게 한다(평가 결과는 남긴다)."""
        try:
            (self.root / REQUEST_FILE).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("카메라 확인 요청 파일을 지우지 못했다: %s", exc)


def evaluate_file(views_path: Path, calibration_doc: Mapping[str, Any], *, config: CameraCheckConfig = DEFAULT_CONFIG,
                  required_pairs: Iterable[tuple[str, str]] = (),
                  blocking_pairs: Iterable[tuple[str, str]] = ()) -> dict[str, Any]:
    """녹화 재생으로 모은 views.jsonl 을 GPU 없이 다시 평가한다(설계 문서 '녹화 재생 검증'). 끝까지 판정이 안 나면 INCOMPLETE."""
    with Path(views_path).open(encoding="utf-8") as fh:
        records = _parse_views(fh)
    return evaluate_views(records, calibration_doc, config, required_pairs=required_pairs,
                          blocking_pairs=blocking_pairs).summary


def load_views(views_path: Path) -> list[tuple[int, list[LocalView]]]:
    """views.jsonl 전체 → [(t, views)]. 빈 줄·형식이 틀린 줄(쓰는 중인 마지막 줄 포함)은 건너뛴다. 파일을 읽지 못하면 빈 목록."""
    try:
        with Path(views_path).open(encoding="utf-8") as fh:
            return _parse_views(fh)
    except OSError:
        return []


def _parse_views(lines: Iterable[str]) -> list[tuple[int, list[LocalView]]]:
    out: list[tuple[int, list[LocalView]]] = []
    for raw in lines:
        if not raw.strip():
            continue
        try:
            out.append(views_from_json(json.loads(raw)))
        except ValueError:
            continue
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="세팅 카메라 확인을 views.jsonl 로 다시 평가한다(S15P31A101-646)")
    parser.add_argument("--views", type=Path, required=True, help="Tier 0 가 쓴 sessions/<id>/views.jsonl")
    parser.add_argument("--calibration", type=Path, required=True, help="평가할 보정 문서(JSON)")
    parser.add_argument("--required-pair", action="append", default=[], metavar="A,B",
                        help="세팅 품질 게이트 NEEDS_CHECK 쌍(카메라 ID 둘을 쉼표로)")
    parser.add_argument("--blocking-pair", action="append", default=[], metavar="A,B",
                        help="세팅 보정이 맞춘 쌍(품질 게이트의 쌍). 주면 그 밖의 쌍은 참고로만 잰다")
    args = parser.parse_args(argv)
    doc = json.loads(args.calibration.read_text(encoding="utf-8"))
    pairs = [tuple(p.split(",", 1)) for p in args.required_pair]
    blocking = [tuple(p.split(",", 1)) for p in args.blocking_pair]
    summary = evaluate_file(args.views, doc, required_pairs=pairs, blocking_pairs=blocking)  # type: ignore[arg-type]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == PASSED else 1


if __name__ == "__main__":
    raise SystemExit(main())
