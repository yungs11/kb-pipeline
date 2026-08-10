#!/usr/bin/env bash
# 반입 번들 3종을 순차로 만든다 — kbp 전체 · kbp 파서전용 · kb.
#
#   bash scripts/airgap/build-all-bundles.sh              # 이미지까지 다시 빌드
#   bash scripts/airgap/build-all-bundles.sh --no-build   # 이미지 재사용(스크립트·문서만 갱신)
#
# 왜 순차인가 — 이미지 빌드는 QEMU amd64 크로스빌드다. 16GB 머신에서 동시에 돌리면 CPU 포화 +
# 스왑으로 양쪽이 느려지고, 실패했을 때 원인이 리소스인지 코드인지 구분이 안 된다.
#
# 각 빌드는 끝에서 `verify-bundle.sh --images --imports` 를 **강제 실행**한다
# (kbp=kordoc+엑셀 왕복, kb=fitz/docx/openpyxl import). 실패하면 그 번들을 만들지 않는다.
#
# 분할(SPLIT_SIZE)은 기본 끔 — 2GB 를 넘어도 단일 파일이다.
set -uo pipefail
KBP="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KB="${KB_DIR:-/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base}"
ARG="${1:-}"

# ★ 옛 산출물이 새 것과 섞이지 않게 먼저 치운다. 분할본이 남아 있으면 서버에서 재결합 시
#   옛 번들이 배포되고 .parts.sha256 검증까지 통과한다(실측 사고).
for d in "$KBP/dist" "$KB/dist"; do
  [ -d "$d" ] || continue
  if ls "$d"/*.part-* >/dev/null 2>&1 || ls "$d"/*.parts.sha256 >/dev/null 2>&1; then
    mkdir -p "$d/stale-DO-NOT-SHIP"
    mv "$d"/*.part-* "$d"/*.parts.sha256 "$d/stale-DO-NOT-SHIP/" 2>/dev/null || true
    echo "[prep] $d 의 옛 분할본을 stale-DO-NOT-SHIP/ 로 격리했다"
  fi
done

one () {  # $1=이름 $2=디렉터리 $3=로그 $4..=추가인자
  local name="$1" dir="$2" log="$3"; shift 3
  for attempt in 1 2 3; do
    echo "[$name] 시도 $attempt/3 → $log"
    ( cd "$dir" && bash scripts/airgap/build-bundle.sh "$@" ) > "$log" 2>&1 && {
      echo "[$name] ✅"; grep -E "번들:|sha256:|import 성공|왕복 성공|kordoc 설치" "$log" | tail -6; return 0; }
    # 빌더 안 DNS 실패는 코드 문제가 아니다(QEMU 부하로 Docker 네트워킹이 엉킨다) — 재시도한다.
    if grep -qE "dns error|no such host|failed to do request" "$log"; then
      echo "[$name] ⚠️ 빌더 DNS 문제 — 30s 후 재시도"; sleep 30; continue
    fi
    echo "[$name] ✗ 실패 — 마지막 25줄:"; tail -25 "$log"; return 1
  done
  echo "[$name] ✗ 3회 실패"; return 1
}

one "kbp-full"   "$KBP" /tmp/bundle-build.log        ${ARG:+$ARG};            R1=$?
one "kbp-parse"  "$KBP" /tmp/bundle-parse-only.log   --parse-only ${ARG:+$ARG}; R2=$?
one "kb"         "$KB"  /tmp/kb-bundle-build.log     ${ARG:+$ARG};            R3=$?

echo
echo "════ 결과 ════"
printf "  %-10s %s\n" kbp-full  "$([ $R1 -eq 0 ] && echo ✅ || echo ✗)"
printf "  %-10s %s\n" kbp-parse "$([ $R2 -eq 0 ] && echo ✅ || echo ✗)"
printf "  %-10s %s\n" kb        "$([ $R3 -eq 0 ] && echo ✅ || echo ✗)"
echo
echo "반입 세트:"
ls -la "$KBP"/dist/*.tar.gz "$KBP"/dist/*.sha256 2>/dev/null | awk '{printf "  %12s  %s\n", $5, $9}'
ls -la "$KB"/dist/*.tar.gz "$KB"/dist/*.sha256 2>/dev/null | awk '{printf "  %12s  %s\n", $5, $9}'
[ $R1 -eq 0 ] && [ $R2 -eq 0 ] && [ $R3 -eq 0 ]
