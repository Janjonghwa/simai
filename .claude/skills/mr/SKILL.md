---
name: mr
description: 현재 작업 브랜치를 팀 Git 규칙(docs/git-convention.md)에 맞춰 GitLab MR 로 올린다. MR 템플릿의 To People / To AI 칸을 커밋·diff 근거로 채우고, 라벨과 Closes 키를 붙여 dev 로 보낸다. "MR 올려줘", "머지 리퀘스트 만들어줘", "/mr" 같은 요청에 사용한다.
---

# MR 올리기

규칙의 정본은 `docs/git-convention.md` 다. 이 문서와 스킬이 다르면 문서를 따르고, 사용자에게 차이를 알린다.

## 고정 값

- GitLab: `https://lab.ssafy.com`, 프로젝트 경로 `s15-final/S15P31A101` (API 에서는 `s15-final%2FS15P31A101`)
- 대상 브랜치: `dev`
- 템플릿: `.gitlab/merge_request_templates/default.md`
- 브랜치 형식: `S15P31A101-{번호}-{설명}`

라벨 (하나만 고른다, GitLab 에 등록된 이름 그대로 쓴다):

| 라벨 | 쓰는 경우 |
| --- | --- |
| ✨ feat | 새로운 기능 추가 |
| 🐛 fix | 버그 수정 |
| 📝 docs | 문서 수정 |
| 🎨 style | 코드 포맷팅, UI 스타일 등 |
| ♻️ refactor | 기능 변경 없는 코드 개선 |
| ⚡ perf | 성능 개선 |
| ✅ test | 테스트 추가/수정 |
| 🔧 config | 설정 파일, 환경 설정 변경 |
| 🔥 remove | 코드/파일 삭제 |
| 🚀 deploy | 배포 관련 변경 |
| 👷 ci | CI/CD 관련 변경 |

## 1. 브랜치 확인

```bash
git rev-parse --abbrev-ref HEAD
```

브랜치 이름이 `^S15P31A101-[0-9]+-[a-z0-9-]+$` 와 맞지 않으면 멈추고 규칙에 맞는 이름을 제안한다. `dev`·`master` 에서는 절대 MR 을 만들지 않는다.

브랜치 이름에서 **Jira 키**(`S15P31A101-{번호}`)를 뽑는다.

## 2. 변경 내용 모으기

```bash
git fetch origin dev
git status --short
git log --format='%h %s' origin/dev..HEAD
git diff --stat origin/dev...HEAD
git diff origin/dev...HEAD
```

- 커밋되지 않은 변경이 있으면 MR 에 포함되지 않는다고 알리고, 커밋할지 사용자에게 묻는다.
- `origin/dev..HEAD` 에 커밋이 없으면 멈춘다.
- 커밋 메시지가 `[S15P31A101-{번호}] ...` 형식이 아닌 것이 있으면 알린다(고치지는 않는다).
- Atlassian(Jira) 도구를 쓸 수 있으면 Jira 이슈 제목과 설명을 읽어 참고한다. 못 쓰면 건너뛴다.
- 이번 대화에서 작업한 내용(실행한 테스트·빌드와 결과, 검토한 대안, 따른 명세)도 근거로 쓴다.

## 3. 제목과 본문 쓰기

**제목**: `[S15P31A101-{번호}] {한국어 한 줄 설명}`. 커밋이 하나면 그 커밋 메시지를 그대로 쓴다.

**라벨**: 커밋과 diff 를 보고 위 표에서 변경의 주된 성격 하나를 고른다. 애매하면 후보 둘과 이유를 4단계에서 보여 주고 사용자가 고르게 한다.

**본문**: `git show origin/dev:.gitlab/merge_request_templates/default.md` 로 dev 에 있는 템플릿을 읽는다(없으면 로컬 파일). 양식의 제목·순서·`<details>` 구조를 그대로 두고 칸을 채운다.

- `<!-- -->` 안내 주석은 지운다.
- 맨 아래 `Closes S15P31A101-XX` 의 `XX` 를 실제 번호로 바꾼다.

**To People** — 리뷰어가 30초 안에 이해할 수 있게 쓴다.
- 작업 내용: 무엇을 했는지 1~3줄.
- As-Is: 변경 전 상태와 문제.
- To-Be: 변경 후 상태와 해결된 것.
- 파일 목록이나 함수 이름 나열이 아니라 동작과 결과로 쓴다.

**To AI** — diff 를 읽는 AI 가 diff 만으로는 알 수 없는 것을 쓴다.
- 의도와 근거: 이 구조를 고른 이유, 검토했다 버린 대안과 이유.
- 관련 명세·계약: 따른 SENTIA 명세 절, 인터페이스(I0~I5), 이벤트·저널 계약 등. 없으면 "해당 없음".
- 가정과 불변 조건: 이 코드가 전제하는 것, 깨지면 안 되는 것.
- 영향 범위: 바뀐 모듈·인터페이스와 영향받는 파트(front / server / ai / deploy).
- 검증: **실제로 실행한** 명령과 결과만 쓴다. 실행하지 않았으면 "실행한 검증 없음" 이라고 쓴다.
- 리뷰 중점 / 남은 일: 특히 봐야 할 부분, 알려진 한계, 후속 작업.

근거가 없는 칸은 지어내지 말고 "해당 없음" 또는 "확인 필요" 로 남긴다.

## 4. 사용자 확인

본문을 임시 파일로 저장한다(스크래치패드가 있으면 그곳, 없으면 OS 임시 폴더. 저장소 안에는 두지 않는다).

사용자에게 아래를 보여 주고 **명시적으로 승인받은 뒤에만** 다음 단계로 간다. MR 은 팀원에게 공개되는 작업이다.
- 소스 → 대상: `{브랜치}` → `dev`
- 제목
- 라벨과 고른 이유 한 줄
- 본문 전체

수정 요청이 있으면 반영해 다시 보여 준다.

## 5. push 와 MR 생성

```bash
git push -u origin HEAD
```

이미 열린 MR 이 있는지 먼저 확인한다(토큰이 있을 때):

```bash
curl -s --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "https://lab.ssafy.com/api/v4/projects/s15-final%2FS15P31A101/merge_requests?state=opened&source_branch={브랜치}"
```

열린 MR 이 있으면 새로 만들지 말고 링크를 알려 준 뒤, 본문을 갱신할지 묻는다(갱신은 `PUT .../merge_requests/{iid}` 에 `description`).

### 토큰이 있을 때 (`GITLAB_TOKEN` 환경변수)

```bash
curl -s --request POST --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "https://lab.ssafy.com/api/v4/projects/s15-final%2FS15P31A101/merge_requests" \
  --data-urlencode "source_branch={브랜치}" \
  --data-urlencode "target_branch=dev" \
  --data-urlencode "title={제목}" \
  --data-urlencode "description@{본문 파일 경로}" \
  --data-urlencode "labels={라벨}" \
  --data-urlencode "remove_source_branch=true" \
  --data-urlencode "squash=true"
```

응답 JSON 의 `web_url` 을 사용자에게 알려 준다. 오류 응답이면 메시지를 그대로 보여 주고 멈춘다.

### 토큰이 없을 때

push 옵션으로 MR 을 만든다. push 옵션에는 여러 줄 본문을 넣을 수 없으므로 본문은 `Closes` 한 줄만 넣는다.

```bash
git push -u origin HEAD \
  -o merge_request.create \
  -o merge_request.target=dev \
  -o "merge_request.title={제목}" \
  -o "merge_request.label={라벨}" \
  -o "merge_request.description=Closes S15P31A101-{번호}" \
  -o merge_request.remove_source_branch
```

그다음 사용자에게 알린다.
- 출력에 나온 MR 링크
- 본문 파일 경로: 이 내용을 MR 편집 화면에 붙여 넣어야 한다.
- 다음부터 본문까지 자동으로 넣으려면 토큰을 설정한다(아래).

## 토큰 설정 안내 (사용자가 물을 때)

1. GitLab → 프로필 → Preferences → **Access tokens** → `api` 범위로 토큰을 만든다.
2. 환경변수로 등록한다. 토큰을 저장소 파일이나 대화에 남기지 않는다.
   - PowerShell: `[Environment]::SetEnvironmentVariable("GITLAB_TOKEN", "<토큰>", "User")` 후 터미널 재시작
   - bash/zsh: `~/.bashrc` 또는 `~/.zshrc` 에 `export GITLAB_TOKEN=<토큰>`
