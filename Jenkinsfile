// MR·dev·master 자동 검사 (기획서 21.4, S15P31A101-28). 설명과 Jenkins 준비는 docs/design/ci-pipeline.md
// 바뀐 경로의 단계만 돈다. GPU 없이 돈다(GPU 시험은 pytest marker gpu 로 뺀다).
// 검사는 Docker 없이 Jenkins 컨테이너 안에서 직접 돈다. 도구(JDK 21·Node·pnpm·uv·Python 3.11)는 deploy/jenkins/Dockerfile 이 넣는다.
// 아직 없는 단계: Risk Engine 공통 벡터(부록 D-G3-04), 통합(Compose), 이미지 빌드·배포(자동 배포 티켓)

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
                        ['AI', ['ai/', 'contracts/']],
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
            // 가장 오래 걸려서(20분 이상) 마지막에 둔다
            when { environment name: 'RUN_AI', value: 'true' }
            steps {
                dir('ai') {
                    sh 'uv sync --frozen'
                    sh 'uv run lint-imports'
                    sh 'uv run pytest -m "not gpu"'
                }
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
