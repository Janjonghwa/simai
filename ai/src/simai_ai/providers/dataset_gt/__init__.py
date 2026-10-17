"""공개 다시점 데이터의 정답을 Provider 계약으로 옮기는 DATASET_GT 어댑터 (명세 20.4, 19).

- `panoptic`: CMU Panoptic Studio (3D 관절 정답 + 카메라 보정). 연구용·비상업 라이선스, 재배포 금지
  (`docs/data/cmu_panoptic.md`).

이 어댑터의 출력으로 얻은 지표는 "입력이 완벽할 때의 상한"이다(`input_provenance.is_upper_bound = true`).
제품 성능으로 쓰지 않고, 현장 데이터와 분리해 평가한다(명세 15.4, 20.4 규칙 3·7).
"""

from simai_ai.providers.dataset_gt.panoptic import (
    PanopticCalibrationProvider,
    PanopticWorldTrackProvider,
)

__all__ = ["PanopticCalibrationProvider", "PanopticWorldTrackProvider"]
