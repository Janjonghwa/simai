// MR·dev·master 자동 검사 (기획서 21.4, S15P31A101-28). 설명과 Jenkins 준비는 docs/design/ci-pipeline.md
// 바뀐 경로의 단계만 돈다. GPU 없이 돈다(GPU 시험은 pytest marker gpu 로 뺀다).
// 검사는 Docker 없이 Jenkins 컨테이너 안에서 직접 돈다. 도구(JDK 21·Node·pnpm·uv·Python 3.11·Android SDK)는 deploy/jenkins/Dockerfile 이 넣는다.
// dev 에 들어온 커밋은 검사를 모두 통과하면 EC2 에 배포한다(S15P31A101-254, docs/design/ec2-deploy.md "자동 배포").
// 아직 없는 단계: Risk Engine 공통 벡터(부록 D-G3-04), 통합(Compose)

// files 가 null 이면(MR 이 아닌 빌드) 모두 돈다
@NonCPS
boolean touched(List<String> files, List<String> prefixes) {
    return files == null || files.any { f -> prefixes.any { p -> f.startsWith(p) } }
}

pipeline {
    agent any

    options {
        gitLabConnection('gitlab')
        timeout(time: 90, unit: 'MINUTES')
        // 관제 서비스와 같은 EC2 에서 돌므로 한 번에 하나만 돈다
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '30'))
        timestamps()
    }

    // GitLab 트리거(Webhook)는 여기 두지 않고 Jenkins 작업 설정에 둔다. 여기 두면 빌드마다 트리거를 다시 만들어
    // 작업에 저장한 Webhook Secret token 이 지워진다(401 Invalid token). 설정 값은 docs/design/ci-pipeline.md

    environment {
        PYTHONUTF8 = '1'
        UV_LINK_MODE = 'copy'
        // 캐시는 저장소 밖(@tmp)에 둬 git 상태를 건드리지 않는다(ai 시험이 git 정보를 읽는다)
        UV_CACHE_DIR = "${env.WORKSPACE}@tmp/cache/uv"
        PNPM_STORE_DIR = "${env.WORKSPACE}@tmp/cache/pnpm"
        GRADLE_USER_HOME = "${env.WORKSPACE}@tmp/cache/gradle"
        // pnpm install 의 prepare 가 git 훅을 연결하지 않게 한다
        HUSKY = '0'
        // 이 작업에서 검사를 통과한 커밋의 표시(빈 파일, 이름 = sha). 대상 브랜치를 합친 커밋의 지름길(변경 경로 단계)이 본다
        VERIFIED = "${env.WORKSPACE}@tmp/verified"
    }

    stages {
        stage('변경 경로') {
            steps {
                updateGitlabCommitStatus name: 'jenkins', state: 'running'
                // 작업 공간은 빌드 사이에 재사용된다. 다른 브랜치 빌드가 남긴 추적 안 되는·무시 파일(__pycache__ 등)이
                // 남으면 시험이 엉뚱하게 실패한다(S15P31A101-258). 다시 받기 비싼 .venv·node_modules·.gradle 만 남긴다
                sh 'git clean -ffdx -e .venv -e node_modules -e .gradle'
                script {
                    List<String> files = null
                    if (env.gitlabActionType == 'MERGE' && env.gitlabTargetBranch) {
                        def base = sh(script: "git merge-base origin/${env.gitlabTargetBranch} HEAD", returnStdout: true).trim()
                        files = sh(script: "git diff --name-only ${base} HEAD", returnStdout: true).trim().readLines()
                        // 충돌을 풀려고 대상 브랜치를 합친 커밋만 새로 왔으면(두 부모가 모두 이 Jenkins 를 통과한 것, 아래 VERIFIED)
                        // MR 전체를 다시 돌리지 않고 두 부모 모두와 다른 파일(합친 자리: 충돌 해결·양쪽이 같이 고친 파일)만 본다.
                        // 명세 개정 이력(docs/spec/README.md) 충돌 때문에 서버 빌드를 통째로 다시 기다리는 일을 줄인다(S15P31A101-33).
                        // 코드 파일이 양쪽에서 함께 바뀌었으면 그 파트는 그대로 돈다
                        def parents = sh(script: 'git rev-list --parents -n 1 HEAD', returnStdout: true).trim().split(' ')
                        if (parents.size() == 3
                                && sh(script: "git merge-base --is-ancestor ${parents[2]} origin/${env.gitlabTargetBranch}", returnStatus: true) == 0
                                && fileExists("${env.VERIFIED}/${parents[1]}") && fileExists("${env.VERIFIED}/${parents[2]}")) {
                            files = sh(script: """
                                git diff --name-only ${parents[1]} HEAD | sort > "\$WORKSPACE@tmp/merged-a"
                                git diff --name-only ${parents[2]} HEAD | sort > "\$WORKSPACE@tmp/merged-b"
                                comm -12 "\$WORKSPACE@tmp/merged-a" "\$WORKSPACE@tmp/merged-b"
                            """, returnStdout: true).trim().readLines()
                            echo "대상 브랜치를 합친 커밋: 두 부모가 이미 통과했으므로 합친 자리만 본다 ${files}"
                        }
                    }
                    def always = ['Jenkinsfile', 'deploy/jenkins/']
                    // contracts/ 가 바뀌면 계약을 읽는 파트도 돈다: ai·gateway 시험, front 생성 타입, server 계약 클래스(contracts.gradle)
                    def parts = [
                        ['CONTRACTS', ['contracts/']],
                        // deploy/ 는 ai/tests/deploy 가 파일 경로로 읽어 검사한다(시험 고르기 스크립트가 잇는다, S15P31A101-260)
                        ['AI', ['ai/', 'contracts/', 'deploy/']],
                        ['GATEWAY', ['gateway/', 'contracts/']],
                        ['FRONT', ['front/', 'contracts/']],
                        ['SERVER', ['server/', 'contracts/']],
                        // 워치 앱 단위 시험이 I4 계약 예시(contracts/examples)를 읽는다(S15P31A101-337)
                        ['WATCH', ['watch/', 'contracts/']],
                    ]
                    for (int i = 0; i < parts.size(); i++) {
                        def run = touched(files, parts[i][1] + always) ? 'true' : 'false'
                        env.setProperty("RUN_${parts[i][0]}", run)
                        echo "${parts[i][0]}: ${run}"
                    }
                }
            }
        }

        stage('판 점검') {
            // 프로젝트 설정의 판(Node·Java·Python)이 배포·CI 이미지의 판과 같은지 본다. 다르면 이미지 빌드나 검사가 깨진다
            steps {
                sh 'sh deploy/scripts/check_versions.sh'
            }
        }

        stage('contracts') {
            when { environment name: 'RUN_CONTRACTS', value: 'true' }
            steps {
                sh 'uv run contracts/scripts/validate_examples.py'
            }
        }

        stage('server') {
            when { environment name: 'RUN_SERVER', value: 'true' }
            environment {
                // ci-db(compose, ci 망)의 시험 DB. 일회용이라 비밀 값이 아니다
                PGHOST = 'ci-db'
                PGUSER = 'postgres'
                PGPASSWORD = 'simai-ci'
                SPRING_DATASOURCE_USERNAME = 'postgres'
                SPRING_DATASOURCE_PASSWORD = 'simai-ci'
                // 시험 컨텍스트마다 풀을 따로 잡는다(Spring 시험 컨텍스트 캐시). 기본 10 이면 ci-db 연결 상한을 넘을 수 있다
                SPRING_DATASOURCE_HIKARI_MAXIMUM_POOL_SIZE = '5'
            }
            steps {
                // 빌드마다 빈 DB 에서 Flyway 가 V1 부터 적용하게 지우고 새로 만든다
                script {
                    // 작업마다 시험 DB 를 따로 쓴다. simai(MR·dev)와 simai-nightly-ai 는 서로 다른 작업이라
                    // disableConcurrentBuilds 로 막히지 않고 동시에 돌 수 있는데, 같은 DB 를 FORCE 로 지우면
                    // 다른 쪽 시험이 무더기로 깨진다(S15P31A101-466). 환경 블록에 두면 이 값보다 우선하므로 여기서만 정한다
                    env.CI_DB = 'simai_ci_' + env.JOB_NAME.replaceAll('[^A-Za-z0-9]', '_')
                    env.SPRING_DATASOURCE_URL = "jdbc:postgresql://ci-db:5432/${env.CI_DB}"
                }
                sh 'psql -v ON_ERROR_STOP=1 -d postgres -c "DROP DATABASE IF EXISTS $CI_DB WITH (FORCE)" -c "CREATE DATABASE $CI_DB"'
                dir('server') {
                    // gradlew 는 실행 권한 없이 커밋돼 있다
                    sh 'sh gradlew build --no-daemon'
                }
            }
            post {
                always {
                    junit allowEmptyResults: true, testResults: 'server/build/test-results/test/*.xml'
                }
            }
        }

        stage('front') {
            when { environment name: 'RUN_FRONT', value: 'true' }
            environment {
                // 관제 앱 빌드는 이 값이 없으면 실패한다(parseEnv). CI 에는 .env.local 이 없으므로 배포 이미지(docker-bake.hcl web)와 같은 값을 준다
                VITE_API_BASE_URL = 'https://simai.live'
                VITE_STOMP_URL = 'wss://simai.live/ws'
                VITE_API_MOCK = 'false'
            }
            steps {
                dir('front') {
                    sh 'pnpm install --frozen-lockfile --store-dir "$PNPM_STORE_DIR"'
                    sh 'pnpm format:check'
                    // check:generated 가 contracts/ 로 만든 타입이 최신인지 본다
                    sh 'pnpm lint'
                    sh 'pnpm typecheck'
                    sh 'pnpm test'
                    sh 'pnpm build'
                }
            }
        }

        stage('gateway') {
            when { environment name: 'RUN_GATEWAY', value: 'true' }
            steps {
                script {
                    for (pkg in ['recorder', 'relay', 'rtsp_probe', 'site_alert']) {
                        dir("gateway/${pkg}") {
                            sh 'uv sync --frozen'
                            sh 'uv run pytest'
                        }
                    }
                }
            }
        }

        stage('watch') {
            when { environment name: 'RUN_WATCH', value: 'true' }
            steps {
                script {
                    // Android SDK 는 이미지의 ANDROID_HOME. EC2 의 Jenkins 이미지를 다시 빌드하기 전이면 SDK 가 없다.
                    // 그때 실패로 끝내면 dev 배포까지 막히므로 불안정(GitLab 상태 failed)으로 알리고 넘어간다
                    if (!env.ANDROID_HOME) {
                        unstable 'watch 생략: Jenkins 이미지에 Android SDK 가 없다. docs/design/ci-pipeline.md "Jenkins 준비" 1 로 이미지를 다시 빌드한다'
                        return
                    }
                    dir('watch') {
                        // Kotlin 컴파일을 Gradle 프로세스 안에서 돌려(별도 데몬 없이) 컨테이너 메모리 상한(4 GB) 안에 둔다
                        sh 'sh gradlew assembleDebug testDebugUnitTest --no-daemon -Pkotlin.compiler.execution.strategy=in-process'
                    }
                }
            }
            post {
                always {
                    junit allowEmptyResults: true, testResults: 'watch/app/build/test-results/testDebugUnitTest/*.xml'
                }
            }
        }

        stage('ai') {
            // 평소 MR·dev 빌드는 의존성·import 규칙만 본다. ai pytest(약 5,350개, 30분대)는 새벽 전체 시험 작업
            // simai-nightly-ai(매개변수 FULL_AI_TESTS=true, 매일 02:00 KST, 배포 없음)에서만 돈다(S15P31A101-269).
            // 빌드가 한 줄로 하나씩 돌아 ai 전체 시험 하나가 뒤 MR 들을 30분 넘게 막았다(빌드 #43·#44).
            // 매개변수·주기는 Jenkins 작업 설정에 두고 여기 parameters·triggers 로 두지 않는다 — Webhook 토큰 문제
            when { environment name: 'RUN_AI', value: 'true' }
            steps {
                dir('ai') {
                    sh 'uv sync --frozen'
                    sh 'uv run lint-imports'
                    script {
                        if (params.FULL_AI_TESTS?.toString() == 'true') {
                            // 1) extras 없이 수집만: 선택 의존성(cv2·torch 등)을 함수 안에서 import 하는지 확인
                            // 2) 가벼운 extras(calibration·io: opencv-headless·scipy·psutil·pyarrow·av)를 넣고 전체 시험.
                            //    extras 없이 돌면 Tier 0 보정·배경·회전 복구 시험 약 35파일이 cv2 없음으로 통째로 건너뛰어
                            //    깨져도 CI 가 몰랐다(S15P31A101-574). torch 묶음은 무거워 넣지 않는다
                            echo 'ai 시험 전체(FULL_AI_TESTS): extras 없이 수집 확인 → calibration·io extras 로 전체'
                            sh 'uv run pytest -m "not gpu" --collect-only -q > /dev/null'
                            sh 'uv sync --frozen --extra calibration --extra io'
                            sh 'uv run pytest -m "not gpu"'
                        } else {
                            echo 'ai pytest 생략: 새벽 전체 시험 작업(simai-nightly-ai, 02:00 KST)에서 돈다. 급하면 로컬에서 scripts.select_tests 로 고른 시험을 돌린다'
                        }
                    }
                }
            }
        }

        stage('배포') {
            // dev 에 push(머지)된 빌드만. MR 빌드는 배포하지 않는다. 앞 단계가 하나라도 실패하면 여기까지 오지 않는다
            when {
                allOf {
                    environment name: 'gitlabActionType', value: 'PUSH'
                    environment name: 'gitlabBranch', value: 'dev'
                }
            }
            steps {
                // Jenkins 에는 Docker 소켓이 없다. 소스를 호스트로 보내고 빌드·기동은 호스트의 deploy.sh 가 한다.
                // 이 키는 authorized_keys 에서 그 스크립트만 실행하게 묶여 있다(command=)
                sh '''
                    git archive --format=tar HEAD | ssh -i "$HOME/.ssh/simai_deploy" -o BatchMode=yes \
                        ubuntu@host.docker.internal "deploy $(git rev-parse HEAD)"
                '''
            }
        }
    }

    post {
        success {
            sh 'mkdir -p "$VERIFIED" && : > "$VERIFIED/$(git rev-parse HEAD)"'
            updateGitlabCommitStatus name: 'jenkins', state: 'success'
        }
        unstable { updateGitlabCommitStatus name: 'jenkins', state: 'failed' }
        failure { updateGitlabCommitStatus name: 'jenkins', state: 'failed' }
        aborted { updateGitlabCommitStatus name: 'jenkins', state: 'canceled' }
    }
}
