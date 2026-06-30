#!/usr/bin/env bash
# 엑셀 게이트 스택 일괄 재기동 — 파서-후단 엑셀 게이트가 실제로 동작하도록
# 의존 순서대로(게이트가 호출하는 excel-parser·doc_guard 가 먼저, 그 다음 kb-backend) 올린다.
#
#   doc_guard(:8000, /v1/check-excel) ─┐
#   excel-parser(:18055, gate_summary)─┴─▶ kb-backend(:8088, parse_preview 게이트 호출)
#
# 코드 변경(3레포 어디든) 후 게이트가 옛 코드로 남는 사고를 막는다. 각 서비스는 포트
# 기준 종료 + 새 코드 응답 검증까지 한다(run-*.sh).
#
# Usage:  bash scripts/restart-gate-stack.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

run() {  # name script
  echo "── $1 ──"
  if bash "$HERE/$2"; then echo "✅ $1"; else echo "❌ $1 (위 WARN 확인)"; FAIL=1; fi
  echo
}

FAIL=0
run "doc_guard (:8000)"     run-doc-guard.sh
run "excel-parser (:18055)" run-excel-parser.sh
run "kb-backend (:8088)"    run-kb-backend.sh

echo "── 요약 ──"
for p in 8000 18055 8088; do
  pid="$(lsof -nP -iTCP:$p -sTCP:LISTEN -t 2>/dev/null | head -1)"
  echo "  :$p $( [ -n "$pid" ] && echo "LISTEN (pid $pid)" || echo "DOWN" )"
done
echo
echo "프론트(:4000/:4001)는 next dev 핫리로드 — 브라우저 새로고침 권장."
echo "kb-backend BACKEND_ORIGIN 은 knowledge_base/frontend/.env.local 확인(현재 :8088)."
[ "$FAIL" = 0 ] && echo "게이트 스택 정상." || { echo "일부 실패 — 로그 확인."; exit 1; }
