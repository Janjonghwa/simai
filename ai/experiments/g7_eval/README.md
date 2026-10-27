# ai/experiments/g7_eval

G7(VLM·보고서) 평가 도구 (S15P31A101-35). 설계·채점 기준표·실행 순서는 `docs/design/g7-eval.md`, 규칙은 `ai/CONVENTION.md`.

- `g7_samples.py`(노트북): Tier 0 재생 출력 + 녹화 세션 → 층을 나눈 표본 묶음(`samples/<id>/`, `manifest.json`).
- `g7_answers.py`(GPU 세션): 묶음 → 270 관문 → 22 클라이언트 → `answers.jsonl`. 프롬프트는 `--prompt 모듈:이름` 으로 주입(25 전에는 `placeholder`, 판정 안 함).
- `g7_score.py`(노트북): 채점표 내보내기(`sheet`, `cite-sheet`)·집계(`aggregate`, `cite-aggregate`) → `g7_result.json`.

묶음·답·흐린 그림·채점 원본은 사람이 찍힌 자료라 커밋하지 않는다(저장소 밖 또는 `runs/` 아래).
