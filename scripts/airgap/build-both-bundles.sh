#!/usr/bin/env bash
# Docker 준비 대기 → kbp 번들 → kb 번들. 순차(둘 다 QEMU amd64 크로스빌드).
#
# 재시도를 넣은 이유: 직전 실행이 **빌드 컨테이너 안의 DNS 실패**로 죽었다
# (host 는 pypi.org 200 인데 builder 가 `no such host`). QEMU 부하로 Docker Desktop
# 네트워킹이 엉키는 현상이라 코드 문제가 아니다 — 같은 명령이 재시도로 통과한다.
set -uo pipefail
KBP=/Users/xxx/workspace/8.kb-pipeline
KB=/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base

echo "[0] Docker 데몬 대기…"
for i in $(seq 1 120); do docker info >/dev/null 2>&1 && break; sleep 5; done
docker info >/dev/null 2>&1 || { echo "[0] ✗ Docker 미기동 — 중단"; exit 1; }
echo "[0] ✅ Docker $(docker info --format '{{.ServerVersion}}')"

echo "[0] 컨테이너 DNS 확인"
for i in 1 2 3; do
  if docker run --rm alpine:latest sh -c 'nslookup pypi.org' >/dev/null 2>&1; then
    echo "[0] ✅ 컨테이너 DNS 정상"; break
  fi
  echo "[0] ⚠️ DNS 실패 ($i/3) — 10s 후 재시도"; sleep 10
done

build () {  # $1=이름 $2=디렉터리 $3=로그
  local name="$1" dir="$2" log="$3"
  for attempt in 1 2 3; do
    echo "[$name] 빌드 시도 $attempt/3 → $log"
    ( cd "$dir" && bash scripts/airgap/build-bundle.sh ) > "$log" 2>&1 && {
      echo "[$name] ✅ 완료"; grep -E "번들:|sha256:|import 성공|✓ " "$log" | tail -8; return 0; }
    if grep -qE "dns error|no such host|failed to do request" "$log"; then
      echo "[$name] ⚠️ DNS 문제로 실패 — Docker 네트워킹 이슈다. 30s 후 재시도"; sleep 30; continue
    fi
    echo "[$name] ✗ 실패(DNS 아님) — 마지막 25줄:"; tail -25 "$log"; return 1
  done
  echo "[$name] ✗ 3회 재시도 모두 실패"; return 1
}

build kbp "$KBP" /tmp/bundle-build.log
KBP_RC=$?
build kb  "$KB"  /tmp/kb-bundle-build.log
KB_RC=$?

echo
echo "════ 결과 ════"
echo "  kbp: $([ $KBP_RC -eq 0 ] && echo ✅ || echo ✗)"
echo "  kb : $([ $KB_RC -eq 0 ] && echo ✅ || echo ✗)"
ls -la "$KBP"/dist/*.tar.gz* 2>/dev/null | awk '{printf "  %s  %s\n", $5, $9}'
ls -la "$KB"/dist/*.tar.gz* 2>/dev/null | awk '{printf "  %s  %s\n", $5, $9}'
