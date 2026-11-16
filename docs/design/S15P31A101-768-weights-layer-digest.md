# S15P31A101-768 통합 이미지 가중치 층 다이제스트 고정

관련: 명세 3.12(모델별 Docker 이미지, 이미지 다이제스트 = `env_hash`). 선행: -577(가중치 굽기 단계, `docs/design/bake-demo-weights.md`), -207(층 분할, `docs/design/exp-images.md`).
코드: `deploy/tier0/Dockerfile`(`weights-*` 단계, `unified` 의 `COPY --link`), `deploy/scripts/bake_weight.py`. 시험: `ai/tests/deploy/test_simai_bootstrap.py`(bake·baked).

## 현상

로컬 `simai/unified:e13d3e461549` 와 `simai/unified:66a639a0ccc1` 은 59층 중 1~23층만 diff_id 가 같다. 가중치 `COPY /out/ /opt/simai/baked/` 6층(풀린 크기 0.27·2.81·4.91·5.05·0.21·0.47GB, 합 13.7GB)도 달라서
코드만 바꿔도 13.7GB 를 Docker Hub 에 다시 올린다. `bake_weight.py` 는 출력의 mtime 을 0 으로 고정하고 이미 `COPY --link` 를 쓰는데도 그랬다.

## 원인 (확정)

두 이미지의 가중치 층 압축 blob 을 Docker Desktop VM 의 containerd 저장소(`/var/lib/desktop-containerd/daemon/io.containerd.content.v1.content`)에서
바로 읽어 tar 헤더를 비교했다(`docker save` 없이, 특권 컨테이너 + `nsenter -t 1 -m`. 절차는 `C:/Users/SSAFY/simai-local/768/{layers,tarlist}.sh`).

| 층 항목 | e13d3e461549 | 66a639a0ccc1 |
|---|---|---|
| `opt/`, `opt/simai/` | 2026-11-15 22:54:41 UTC | 2026-11-16 02:40:04 UTC |
| `opt/simai/baked/` 이하 폴더·파일 | 1970-01-01 00:00:00 | 1970-01-01 00:00:00 |
| 경로·크기·모드·uid/gid | 같음 | 같음 |

- 내용(HF 리비전·해시)·소유자·권한은 같다. **다른 것은 COPY 대상의 부모 폴더 `opt/`·`opt/simai/` 의 mtime 뿐**이다. yolo 층과 lama 층 모두 같은 양상이다.
- `COPY --link … /opt/simai/baked/` 는 빈 층 위에서 대상 경로를 만들며, 단계 출력에 없는 부모 폴더(`opt/`·`opt/simai/`)를 **복사를 실행한 시각**으로 만든다.
  66a639 의 02:40:04 UTC 는 그 빌드 시작(11:40:00 KST) 4초 뒤, 여섯 복사가 병렬로 시작한 시각이다.
- 그래서 캐시가 맞으면 같은 층이지만, 복사 단계를 다시 실행하면 내용이 같아도 다이제스트가 바뀐다. 두 릴리스 빌드 로그(`release-e13d3e461549.log`, `release-66a639a0ccc1.log`)에서
  `weights-*` 받기 단계는 `CACHED` 였고 `COPY --link` 여섯 개는 다시 실행(`DONE 1~20s`)됐다.
- 다시 실행된 이유는 BuildKit 캐시 GC 다: 이 노트북 builder 의 GC 기준은 20GiB(`daemon.json` `builder.gc.defaultKeepStorage`)인데 빌드 캐시가 35GB 라
  빌드 사이에 캐시 기록이 지워진다(같은 이유로 `wheels` 설치 층도 두 빌드 모두 다시 실행됐다, 아래 "남은 위험").
- 작은 Dockerfile 로 재현했다(`C:/Users/SSAFY/simai-local/768/t/`): 같은 단계 출력(mtime 0)을 `COPY --link --from=w /out/ /opt/simai/baked/` 로 `--no-cache` 두 번 빌드하면
  마지막 층 diff_id 가 다르다(`8f0cdeb6…` ≠ `5e387b4f…`). `--build-arg SOURCE_DATE_EPOCH=0` 만 줘도 다르다(`6b6ec8dd…` ≠ `405a145f…`).

## 고친 것

- `weights-*` 단계가 이미지 안 경로 그대로 출력한다: `python /tmp/bake_weight.py /out/opt/simai/baked …`.
- `unified` 는 `COPY --link --from=weights-* /out/ /` 로 넣는다. 대상 `/` 는 이미 있어 BuildKit 이 부모 폴더를 새로 만들지 않고, `opt/`·`opt/simai/` 는 단계 출력의 것(mtime 0)이 들어간다.
- `bake_weight.py` 는 출력 폴더뿐 아니라 **자기가 새로 만든 조상 폴더**(`/out/opt/simai`·`/out/opt`·`/out`)의 mtime 도 0 으로 둔다(원래 있던 폴더는 건드리지 않는다).
- 이미지 안 경로(`/opt/simai/baked/<목록 path>` + `.sha256`), 파일 내용·모드·소유자는 그대로다. 달라지는 것은 최종 이미지의 `/opt`·`/opt/simai` 폴더 mtime(빌드 시각 → 1970)뿐이고,
  그 뒤 층(`COPY contracts /opt/simai/contracts` 등)이 다시 그 폴더를 쓰면 그 층의 시각이 된다. 읽는 코드는 폴더 시각을 보지 않는다.
- 시험: `test_baked_layers_sit_below_code_and_token_is_only_a_buildkit_secret` 가 새 COPY 줄과 출력 경로를 보고, `test_bake_weight_zeroes_mtime_of_ancestors_it_created` 를 더했다.

같은 재현 Dockerfile 을 이 방식(`/out/opt/simai/baked` 출력, 모두 mtime 0, `COPY --link --from=w /out/ /`)으로 `--no-cache` 두 번 빌드하면 diff_id 가 같다(`b67a697f…` = `b67a697f…`),
`/opt`·`/opt/simai`·`/opt/simai/baked` 는 0755 root, 1970-01-01 이다.

## 검증

| 항목 | 결과 |
|---|---|
| 원인(두 릴리스 이미지 tar 헤더) | 위 표: 부모 폴더 mtime 만 다름 |
| 재현 Dockerfile, 고치기 전 `--no-cache` ×2 | 다름 |
| 재현 Dockerfile, 고친 뒤 `--no-cache` ×2 | 같음 |
| `pytest tests/deploy/test_simai_bootstrap.py -k "bake or baked or dockerfile"` | 13 통과 |
| 실제 `unified` 빌드 768-a / 768-b 와 가중치 6층 diff_id 비교, `check_image.sh` | **못 함**: 768-a 를 16:02 에 시작했지만 약 80초 뒤 노트북 메모리 부족으로 빌드 셸이 강제 종료됐다(yolo-pose 0.13GB 만 받음). Lead 확인 필요 |

실제 빌드로 확인할 절차(Lead): 이 브랜치에서 `TAG=768-a docker buildx bake -f deploy/docker-bake.hcl unified`, 그다음 `ai/src` 에 주석 한 줄을 임시로 넣고
`TAG=768-b docker buildx bake -f deploy/docker-bake.hcl unified --set unified.no-cache-filter=unified`(받기 단계는 캐시, 가중치 COPY 는 강제로 다시 실행)로 빌드해
`docker image inspect --format '{{range .RootFS.Layers}}{{println .}}{{end}}'` 의 가중치 6층(위에서 32~37번째)을 비교한다. 빌드 셸: `C:/Users/SSAFY/simai-local/768/build.sh <태그> [bake 인자]`.

## 버린 대안

- **`SOURCE_DATE_EPOCH` 만.** 재현 시험에서 `COPY --link` 부모 폴더 시각이 고정되지 않았다.
- **이미지 출력 `rewrite-timestamp=true` + `SOURCE_DATE_EPOCH`.** 모든 층의 시각을 고치므로 RUN 층 재현성도 좋아지지만, 처음 한 번 59층 전부(약 50GB)가 새 다이제스트가 되어 다시 올려야 하고,
  내보낼 때 층을 다시 쓰는 비용이 매 빌드 붙는다. 이번 문제(가중치 6층)에는 과하다.
- **받기 단계는 그대로 두고 단계마다 폴더 모양만 바꾸는 단계 추가**(`/out` → `/out/opt/simai/baked` 복사). 다시 받기는 피하지만 단계 6개와 5GB 로컬 복사가 늘어난다.

## 남은 위험

- **처음 한 번 다시 받는다.** `weights-*` RUN 줄이 바뀌어 캐시가 깨진다. 768-a 에서 yolo-pose 0.13GB 에 약 77초(약 1.7MB/s)였으니 같은 속도면 13.4GB 에 2시간 남짓이다.
  가중치 6층은 지금도 빌드마다 새 다이제스트라 올리기 비용은 이번이 마지막이 된다.
- **가중치 외 층은 그대로 바뀐다.** 두 릴리스 이미지는 24층(`wheels` 설치, 0.78GB)부터 위가 모두 달랐다: wheels 0.78 + 잠금 sync 0.73 + apt 0.54 + worker 휠 2.89 + worker sync 2.35GB ≈ 7.3GB 와 그 위 작은 층.
  BuildKit 캐시 GC(20GiB)로 wheels 단계 캐시가 지워져 다시 실행됐고, RUN 결과는 파일 시각이 달라 내용이 같아도 다른 층이 된다(-207 에서 본 것과 같다).
  builder GC 기준을 키우면(예: Docker Desktop `builder.gc.defaultKeepStorage` 를 150GB) 줄어든다. 노트북 설정이라 이 티켓에서 바꾸지 않았다(Lead 판단).
- 다른 빌드 기계에서도 가중치 6층은 같은 다이제스트가 된다(내용·시각·모드가 모두 고정). 단 베이스(`WEIGHTS_IMAGE` python slim)의 umask 가 달라지면 폴더 모드가 바뀔 수 있다(다이제스트로 고정돼 있어 지금은 해당 없음).
