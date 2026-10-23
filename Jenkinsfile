// MR·dev·master 자동 검사 (기획서 21.4, S15P31A101-28). 설명과 Jenkins 준비는 docs/design/ci-pipeline.md
// 바뀐 경로의 단계만 돈다. GPU 없이 돈다(GPU 시험은 pytest marker gpu 로 뺀다).
// 검사는 Docker 없이 Jenkins 컨테이너 안에서 직접 돈다. 도구(JDK 21·Node·pnpm·uv·Python 3.11)는 deploy/jenkins/Dockerfile 이 넣는다.
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
                SPRING_DATASOURCE_URL = 'jdbc:postgresql://ci-db:5432/simai_ci'
                SPRING_DATASOURCE_USERNAME = 'postgres'
                SPRING_DATASOURCE_PASSWORD = 'simai-ci'
                // 시험 컨텍스트마다 풀을 따로 잡는다(Spring 시험 컨텍스트 캐시). 기본 10 이면 ci-db 연결 상한을 넘을 수 있다
                SPRING_DATASOURCE_HIKARI_MAXIMUM_POOL_SIZE = '5'
            }
            steps {
                // 빌드마다 빈 DB 에서 Flyway 가 V1 부터 적용하게 지우고 새로 만든다
                sh 'psql -v ON_ERROR_STOP=1 -d postgres -c "DROP DATABASE IF EXISTS simai_ci WITH (FORCE)" -c "CREATE DATABASE simai_ci"'
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

        stage('ai') {
            // 가장 오래 걸려서 마지막에 둔다. MR 빌드는 바뀐 파일과 import 관계로 고른 시험만 돌고(scripts/select_tests.py,
            // S15P31A101-255·-260), dev push 빌드는 전체를 돈다(머지 뒤 안전망: 고르기가 놓친 것을 잡는다)
            when { environment name: 'RUN_AI', value: 'true' }
            steps {
                dir('ai') {
                    sh 'uv sync --frozen'
                    sh 'uv run lint-imports'
                    script {
                        List<String> selected = ['ALL']
                        if (env.gitlabActionType == 'MERGE' && env.gitlabTargetBranch) {
                            selected = sh(script: "uv run python -m scripts.select_tests --base origin/${env.gitlabTargetBranch}",
                                          returnStdout: true).trim().readLines().findAll { it.trim() }
                        }
                        if (selected.isEmpty()) {
                            echo 'ai 시험 생략: 바뀐 파일이 ai 시험에 닿지 않는다(lint-imports 만)'
                        } else if (selected.contains('ALL')) {
                            sh 'uv run pytest -m "not gpu"'
                        } else {
                            echo "고른 ai 시험 경로 ${selected.size()}개"
                            sh "uv run pytest -m 'not gpu' ${selected.join(' ')}"
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
        success { updateGitlabCommitStatus name: 'jenkins', state: 'success' }
        unstable { updateGitlabCommitStatus name: 'jenkins', state: 'failed' }
        failure { updateGitlabCommitStatus name: 'jenkins', state: 'failed' }
        aborted { updateGitlabCommitStatus name: 'jenkins', state: 'canceled' }
    }
}
