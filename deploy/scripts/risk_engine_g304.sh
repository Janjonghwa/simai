#!/usr/bin/env bash
# 부록 D-G3-04: Tier 0 이미지와 EC2 Risk Engine 컨테이너 이미지의 Risk Engine 동일성 (S15P31A101-99). GPU 불필요, 저장소 최상위에서:
#   bash deploy/scripts/risk_engine_g304.sh simai/risk-engine:<TAG> simai/tier0:<TAG>
# 같은 커밋으로 빌드한 두 이미지에서
#   1) 공통 테스트 벡터(ai/tests/risk/vectors)를 각 이미지 안의 라이브러리로(python -m simai_ai.risk.vectors) 돌려 출력이 같고 모두 통과,
#   2) 계약 예시 PLANNED 요청을 Risk Engine 이미지는 API(HTTP, 망 없는 컨테이너 안), Tier 0 이미지는 라이브러리 직접 호출
#      (services.risk_engine.handler)로 보내 응답(휘발 필드 제외)이 같고 계약 예시와 같은지 본다(deploy/scripts/risk_plan_roundtrip.py).
# 벡터·예시는 이미지에 없을 수 있어(Tier 0 이미지는 tests/·contracts/examples 를 싣지 않는다) tar 로 넣는다. 모두 같으면 종료 코드 0.
set -uo pipefail
export MSYS_NO_PATHCONV=1

RISK=${1:?사용법: risk_engine_g304.sh <risk-engine 이미지> <tier0 이미지>}
TIER0=${2:?사용법: risk_engine_g304.sh <risk-engine 이미지> <tier0 이미지>}
CTR=simai-g304-$$
OUT=$(mktemp -d)
# shellcheck disable=SC2317  # trap 으로 부른다
cleanup(){ docker rm -f "$CTR" >/dev/null 2>&1; rm -rf "$OUT"; }
trap cleanup EXIT
FAILS=0
pass(){ echo "PASS  $*"; }
fail(){ echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
inputs(){ tar -cf - ai/tests/risk/vectors contracts/examples deploy/scripts/risk_plan_roundtrip.py; }
# 이미지 안 /tmp/w 에 입력을 풀고 명령을 실행한다(망 없음, 이미지 작업 디렉터리 /opt/simai/ai)
in_image(){ local img=$1 cmd=$2; inputs | docker run -i --rm --network none --entrypoint sh "$img" -c \
  "set -e; mkdir -p /tmp/w; tar -xf - -C /tmp/w; cd /opt/simai/ai; $cmd"; }

VEC='python -m simai_ai.risk.vectors /tmp/w/ai/tests/risk/vectors'
in_image "$RISK" "$VEC" >"$OUT/vec-risk.txt" 2>&1; RC_R=$?
in_image "$TIER0" "$VEC" >"$OUT/vec-tier0.txt" 2>&1; RC_T=$?
if [ "$RC_R" -eq 0 ] && [ "$RC_T" -eq 0 ] && cmp -s "$OUT/vec-risk.txt" "$OUT/vec-tier0.txt"; then
  pass "공통 테스트 벡터: 두 이미지 출력이 같다 ($(tail -1 "$OUT/vec-risk.txt"))"
else
  fail "공통 테스트 벡터: 종료 코드 risk=$RC_R tier0=$RC_T"; diff "$OUT/vec-risk.txt" "$OUT/vec-tier0.txt" | head -20 | sed 's/^/      /'
fi

# Risk Engine 이미지: API 로(HTTP)
docker run -d --name "$CTR" --network none "$RISK" >/dev/null
STATE=starting
for _ in $(seq 1 60); do
  STATE=$(docker inspect --format '{{.State.Health.Status}}' "$CTR" 2>/dev/null || echo gone)
  if [ "$STATE" = healthy ] || [ "$STATE" = unhealthy ] || [ "$STATE" = gone ]; then break; fi
  sleep 1
done
inputs | docker exec -i "$CTR" sh -c 'mkdir -p /tmp/w && tar -xf - -C /tmp/w'
docker exec "$CTR" python /tmp/w/deploy/scripts/risk_plan_roundtrip.py --url http://127.0.0.1:8080 \
  --examples /tmp/w/contracts/examples --dump /tmp/w/plan.json >"$OUT/plan-risk.log" 2>&1; RC_R=$?
docker exec "$CTR" cat /tmp/w/plan.json >"$OUT/plan-risk.json" 2>/dev/null
# Tier 0 이미지: 라이브러리 직접(HTTP 없음)
in_image "$TIER0" 'python /tmp/w/deploy/scripts/risk_plan_roundtrip.py --direct --examples /tmp/w/contracts/examples --dump /tmp/w/plan.json >&2 && cat /tmp/w/plan.json' \
  >"$OUT/plan-tier0.json" 2>"$OUT/plan-tier0.log"; RC_T=$?
if [ "$STATE" = healthy ] && [ "$RC_R" -eq 0 ] && [ "$RC_T" -eq 0 ] && cmp -s "$OUT/plan-risk.json" "$OUT/plan-tier0.json"; then
  pass "PLANNED 계약 예시: Risk Engine API = Tier 0 라이브러리 = 계약 예시 ($(tail -1 "$OUT/plan-risk.log"))"
else
  fail "PLANNED 계약 예시: 컨테이너 $STATE, 종료 코드 risk=$RC_R tier0=$RC_T"
  tail -n 3 "$OUT/plan-risk.log" "$OUT/plan-tier0.log" 2>/dev/null | sed 's/^/      /'
fi

echo
if [ "$FAILS" -eq 0 ]; then echo "risk_engine_g304: 같음 ($RISK = $TIER0)"; else echo "risk_engine_g304: 다름 $FAILS 건"; fi
exit "$FAILS"
