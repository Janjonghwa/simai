// S15P31A101-571: 빌드를 하나씩만 돌린다.
// 4코어 EC2 에서 dev 빌드와 nightly 가 겹치면 메모리(-569)·CPU 부족으로 시험이 죽거나 시간 초과가 난다.
// 이미지의 ref/init.groovy.d 는 jenkins_home 으로 복사되어 시작할 때마다 실행된다
import jenkins.model.Jenkins

def j = Jenkins.get()
if (j.numExecutors != 1) {
    j.setNumExecutors(1)
    j.save()
}
