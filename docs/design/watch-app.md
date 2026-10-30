# 워치 앱 뼈대와 개발 환경 (S15P31A101-335)

갤럭시워치(Wear OS) 전용 최소 앱(기획서 부록 C.6, FR-43~47, NFR-12)의 첫 단계. 저장소 최상위 `watch/` 에 Kotlin·Gradle
프로젝트를 두고, EC2 주소·기기 토큰을 넣고 상태를 보는 화면까지 만든다. EC2 연결(I4)·진동·ACK 는 S15P31A101-337, 걸음 특징은 -339.
빌드·설치 절차는 `watch/README.md`.

## 구성

| 위치 | 하는 일 |
|---|---|
| `watch/gradle/libs.versions.toml` | 판 고정(아래 표) |
| `watch/app/src/main/AndroidManifest.xml` | 워치 전용(`android.hardware.type.watch`), 휴대폰 없이 혼자 동작(`wearable.standalone`), 백업 끔 |
| `config/WatchConfig.kt` | EC2 주소·기기 토큰 검증. 토큰을 `toString`·예외 문장에 넣지 않음 |
| `config/WatchConfigStore.kt` | 앱 전용 SharedPreferences 에 저장·읽기·지우기 |
| `ConfigActivity.kt` | adb `am start` 로 설정을 넣는 화면 없는 입구 |
| `MainActivity.kt` | 설정 상태 화면(Wear Compose Material 3) |
| `app/src/test/.../WatchConfigTest.kt` | 주소·토큰 검증 단위 시험(JVM) |

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 위치 | 저장소 최상위 `watch/`, Android Studio 는 이 폴더를 프로젝트로 연다 | 파트별 최상위 폴더 관례(`server/`·`front/`·`gateway/`). 서버 Gradle 과 빌드를 섞지 않는다 |
| 패키지 | `com.ssafy.simai.watch` | 서버 `com.ssafy.simai` 아래 |
| minSdk / targetSdk / compileSdk | 30 / 36 / 37 | Wear OS 3 = API 30(워치4 이상). Wear Compose 1.7.1 이 compileSdk 37 이상을 요구한다(AAR 메타데이터 검사). 실행 동작은 targetSdk 36 으로 둔다 |
| 판 | AGP 9.4.1(2026-09-18), Kotlin 2.4.21(10-08), Wear Compose 1.7.1(10-07), activity-compose 1.13.0, JUnit 4.13.2, Gradle 9.8.1(서버와 같음) | 나온 지 2주 넘은 안정판. AGP 9.5.0(10-28)·Gradle 9.8.2(10-19)는 2주가 안 돼 뺐다. AGP 9 는 Kotlin 을 내장해 `kotlin-android` 플러그인을 쓰지 않고 Compose 컴파일러 플러그인만 단다 |
| 설정 넣는 방법 | adb `am start` 로 `ConfigActivity` 에 `ec2_base_url`·`device_token` 을 함께 넘긴다 | 워치 키보드로 긴 토큰을 칠 수 없다. 이 단계의 "설정 화면(최소)" 은 상태 표시 + adb 입력으로 한다 |
| 설정 입구 보호 | `ConfigActivity` 에 `android:permission="android.permission.DUMP"` | adb 가 부르려면 exported 여야 해 다른 앱도 부를 수 있다. DUMP 는 adb shell 이 갖고 일반 앱은 받을 수 없어, 다른 앱이 주소를 바꾸거나 설정을 지우지 못한다 |
| 주소·토큰을 함께만 바꿈 | 둘 중 하나만 오면 거부, `clear` 와 새 값을 같이 주면 거부 | 주소만 바꿀 수 있으면 저장된 토큰이 다른 서버로 간다. 지우기와 새 값이 같이 오면 의도를 알 수 없다 |
| 저장 실패 | `commit()` 이 false 면 "저장하지 못했다" 로 알린다 | 저장이 안 됐는데 성공으로 보이면 시연 워치가 연결되지 않는 까닭을 찾기 어렵다 |
| 규칙에 안 맞는 저장값 | 읽을 때 지운다 | 규칙이 강해져 저장값이 무효가 되면 화면은 "설정 안 됨" 인데 토큰만 남는다 |
| 주소 규칙 | https·호스트 필수, 계정·query·fragment·토큰 불가, 루프백 http 도 불가 | 노트북 게이트웨이 `validate_base_url`(gateway/site_alert heartbeat.py)과 같게 해 기기 토큰 다루는 규칙을 하나로 둔다 |
| 숨긴 토큰 찾기 | 주소 원문·unquote·unquote_plus 세 형태에서 토큰 원문·quote 형태를 찾는다. unquote 는 Python 과 같이 깨진 `%` 를 그대로 둔다 | 게이트웨이와 같은 형태들. `java.net.URLDecoder` 는 깨진 `%` 하나에 예외를 내고 `+` 를 공백으로만 풀어 숨긴 토큰을 놓친다(리뷰 지적) |
| 토큰 저장 | 앱 전용 SharedPreferences(MODE_PRIVATE), `allowBackup=false` | 다른 앱이 읽을 수 없고 백업으로 밖에 나가지 않는다. Jetpack Security(EncryptedSharedPreferences)는 지원 종료라 쓰지 않는다. 기기 분실 대비는 토큰 폐기(NFR-12)로 한다 |
| 화면 문구 | 호스트만 보이고 토큰 값은 보이지 않는다 | 화면 촬영·어깨 너머로 토큰이 새지 않게 |

## Jenkins

넣는다. `watch/` 가 바뀐 MR 과 모든 dev push 빌드에서 `watch` 단계가 `assembleDebug testDebugUnitTest` 를 돈다(`docs/design/ci-pipeline.md`).

- Jenkins 는 Docker 소켓 없이 컨테이너 안에서 바로 검사하므로, Android SDK(명령줄 도구·플랫폼 37·빌드 도구 36.0.0)를 Jenkins 이미지에 넣었다.
  판은 `docker-bake.hcl` 의 `ANDROID_*` 이고 `판 점검` 이 watch `compileSdk` 와 맞는지 본다.
- 처음에는 이미지가 커지고 관제 서비스와 같은 EC2 에서 디스크·빌드 시간을 같이 써 넣지 않으려 했다. 워치 코드도 다른 파트처럼 MR 마다
  검사하기로 바꿨다(2026-10-30).
- 바뀐 Jenkins 이미지는 EC2 에서 다시 빌드해 띄워야 watch 단계가 돈다(`ci-pipeline.md` "Jenkins 준비" 1). 그 전 빌드는 `ANDROID_HOME` 이
  없어 watch 단계를 건너뛰고 빌드를 불안정(GitLab 상태 failed)으로 남긴다. 실패로 끝내면 dev 자동 배포까지 막혀서다.
- 워치가 서버 계약(I4)을 읽게 되면(-337) 단계 조건에 `contracts/` 를 더한다.

## 시험

`watch/app/src/test/java/com/ssafy/simai/watch/config/WatchConfigTest.kt` (JVM 단위 시험 5개)

- https 주소를 받고 끝의 `/` 를 뗀다
- 평문 http(루프백 포함)·ftp·계정·query·fragment·경로의 토큰·퍼센트 인코딩한 토큰(깨진 `%` 가 섞여도)·토큰의 quote 형태·
  호스트 없음·형식 오류를 거부하고, 예외 문장에 주소·토큰이 없다
- `+` 가 든 토큰을 `%61b+cd` 처럼 숨겨도 거부
- 빈 토큰·공백 든 토큰 거부
- `toString` 에 토큰이 없다

### 실기기 (2026-10-30, 갤럭시워치6 SM-R940, Android 14 / API 34)

- 무선 디버깅 페어링·연결, `app-debug.apk` 설치·실행
- adb `am start` 로 ConfigActivity 를 불러 DUMP 권한 검사를 통과하고 "설정을 저장했다", 화면에 호스트와 "기기 토큰 설정됨"
- `&`·`;`·`\`·`$` 가 든 토큰이 그대로 저장된다
- clear+값 동시, `http://` 주소, 주소만 넣기는 거부되고 저장값이 그대로다
- clear 로 저장값이 비고 화면이 "설정 안 됨" 으로 바뀐다
- 이 Wi-Fi 에서는 mDNS 로 워치가 보이지 않아 페어링·연결 주소를 손으로 넣었다(README 절차 그대로)
- Windows PowerShell 5.1 에서 부르면 토큰의 `"` 가 adb 에 넘어가기 전에 빠진다. Git Bash 에서 작은따옴표로 넣는다

## 남은 일

- 나머지 워치 2대(갤럭시워치5 1대, 워치6 1대) 설치·실행 확인(이 스토리 완료 조건). 한 대로 먼저 시험을 이어 가고 나중에 맞춘다
- 다른 앱이 ConfigActivity 를 부르지 못하는지는 매니페스트의 DUMP 권한 선언으로만 막았고, 실기기에서 다른 앱으로 불러 보지는 않았다
- 토큰이 아주 짧으면(예: `ec2`) 정상 주소에도 들어 있어 거부된다. 게이트웨이와 같은 동작이고, 서버가 발급하는 토큰은 길어 두었다
- 포그라운드 서비스·wake lock·Wi-Fi 고정(`requestNetwork`)은 -337 에서 붙이고 화면 꺼짐 30분 수신을 잰다
