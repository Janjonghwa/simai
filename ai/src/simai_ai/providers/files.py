"""파일을 읽는 참조 Provider 구현.

`contracts/` 형식 그대로 저장된 파일을 읽어 그대로 낸다. 값을 고치거나 채우지 않는다.
계약 시험 묶음의 참조 구현이며, 녹화한 Tier 0 출력(TIER0_LOG)이나 변환해 둔 데이터셋 정답을 읽는 데도 쓴다.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from simai_ai.providers.base import CalibrationDoc, CalibrationProvider, WorldTrackProvider, WorldTracksDoc


class FileCalibrationProvider(CalibrationProvider):
    """`calibration.schema.json` 문서 한 개를 담은 JSON 파일을 읽는다."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise TypeError(f"{self.path}: 보정 문서는 JSON 객체여야 한다")
        self._doc: CalibrationDoc = doc

    def current(self) -> CalibrationDoc:
        # 호출한 쪽이 고쳐도 다음 호출 결과가 바뀌지 않게 복사본을 준다.
        return json.loads(json.dumps(self._doc))


class FileWorldTrackProvider(WorldTrackProvider):
    """`world_tracks.schema.json` 문서를 담은 파일을 읽는다.

    - `.jsonl`: 한 줄에 문서 하나, 시각 순서 (계약의 저장 형식). 빈 줄은 건너뛴다.
    - 그 밖(`.json`): 문서 하나 또는 문서 배열.

    source·provider_version 은 첫 문서에서 읽는다. 모든 문서가 같은 값인지는 계약 시험이 확인한다.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        first = next(self._read(), None)
        if first is None:
            raise ValueError(f"{self.path}: World 트랙 문서가 없다")
        self._source = first["provider_source"]
        self._provider_version = first["provider_version"]

    @property
    def source(self) -> str:
        return self._source

    @property
    def provider_version(self) -> str:
        return self._provider_version

    def frames(self) -> Iterator[WorldTracksDoc]:
        return self._read()

    def _read(self) -> Iterator[WorldTracksDoc]:
        if self.path.suffix == ".jsonl":
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        yield json.loads(line)
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        yield from data if isinstance(data, list) else [data]
