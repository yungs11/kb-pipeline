#!/usr/bin/env bash
# `.env` 의 KBP_PADDLE_OCR_GATEWAY_URL 을 바꿨을 때 **정말 그 주소로 가는지** 증명한다.
#
# 왜 이 검증이 필요한가 — 응답 200 은 증거가 아니다. 옛 주소가 아직 살아 있으면 그쪽으로
# 가고도 성공한다. 요청이 **새 호스트에 도착하는 것**만이 증거다. 그래서 목업 게이트웨이를
# 세우고 그 로그에 요청이 찍히는지 본다.
#
# 로컬(호스트 dev)에서:
#   bash scripts/ocr-test/verify-ocr-gw-url.sh
#
# 폐쇄망(컨테이너)에서 — 실 게이트웨이 주소가 맞는지 확인할 때:
#   bash scripts/ocr-test/verify-ocr-gw-url.sh --container
#     → parse-svc 컨테이너의 **실효 env** 를 출력하고, 그 주소로 계약 3종을 직접 찔러본다.
#       (목업을 못 세우는 환경이므로 "설정이 프로세스까지 도달했는가"까지 확인한다.)
#
# ⚠️ 컨테이너 env 는 **생성 시점에 고정**된다. `.env` 를 바꾼 뒤에는
#    `podman-compose up -d`(재생성)가 필요하다 — `restart` 만으로는 새 값이 안 들어간다.
#    코드는 요청마다 os.environ 을 읽으므로(paddle_gw.py:90) 프로세스 캐시는 없다.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODE="${1:-}"
PORT="${MOCK_PORT:-18099}"
MOCK_LOG="${MOCK_LOG:-/tmp/mock_ocr.log}"
BASE="http://localhost:$PORT/ocr/paddleocr_vl"

engine () { command -v podman >/dev/null 2>&1 && echo podman || echo docker; }

# ── 컨테이너 모드: 실효 env 확인 + 계약 도달성 ────────────────────────────────
if [ "$MODE" = "--container" ]; then
  E="$(engine)"
  CTR="$($E ps --format '{{.Names}}' 2>/dev/null | grep -m1 -E 'parse-svc' || true)"
  [ -n "$CTR" ] || { echo "✗ parse-svc 컨테이너를 못 찾았다"; exit 1; }
  echo "== parse-svc 컨테이너($CTR)의 실효 env =="
  $E exec "$CTR" sh -c 'echo "  KBP_GATE_OCR_LANE=$KBP_GATE_OCR_LANE"; echo "  KBP_PADDLE_OCR_GATEWAY_URL=$KBP_PADDLE_OCR_GATEWAY_URL"'
  URL="$($E exec "$CTR" sh -c 'printf %s "$KBP_PADDLE_OCR_GATEWAY_URL"' 2>/dev/null)"
  LANE="$($E exec "$CTR" sh -c 'printf %s "$KBP_GATE_OCR_LANE"' 2>/dev/null)"
  if [ "$LANE" = "paddle_gw" ] && [ -z "$URL" ]; then
    echo "✗ 레인은 paddle_gw 인데 URL 이 비었다 — 스캔 PDF 가 RuntimeError 로 실패한다."; exit 1
  fi
  [ -n "$URL" ] || { echo "! URL 미설정(레인이 paddle_gw 가 아니면 정상)"; exit 0; }
  echo
  echo "== 그 주소로 계약 3종 도달 확인(컨테이너 안에서) =="
  # 실 게이트웨이에 빈 submit 을 던지면 4xx 가 정상이다 — **연결이 되는지**가 관심사다.
  $E exec "$CTR" sh -c "curl -s -o /dev/null -w '  POST %{http_code} (connect %{time_connect}s) -> ${URL}/tasks\n' -m 15 -X POST '${URL}/tasks' || echo '  ✗ 연결 실패 — 주소/방화벽/DNS 확인'"
  echo
  echo "판정: HTTP 코드가 돌아오면 **주소는 도달한다**(4xx 도 도달이다)."
  echo "      연결 실패면 주소·방화벽·컨테이너 DNS 를 본다. CNI 면 podman-plugins 필요."
  exit 0
fi

# ── 로컬 모드: 목업 세우고 실제 파싱을 흘려본다 ───────────────────────────────
cd "$ROOT"
command -v lsof >/dev/null 2>&1 && kill $(lsof -nP -iTCP:$PORT -sTCP:LISTEN -t 2>/dev/null) 2>/dev/null || true

echo "== 1) 목업 OCR 게이트웨이 기동 (:$PORT) =="
python3 scripts/ocr-test/mock_ocr_gateway.py --port "$PORT" --log "$MOCK_LOG" >/tmp/mock_ocr.out 2>&1 &
MOCK_PID=$!
trap 'kill $MOCK_PID 2>/dev/null || true' EXIT
for _ in $(seq 1 20); do curl -s -m 1 "http://localhost:$PORT/ocr/paddleocr_vl/tasks/x" >/dev/null 2>&1 && break; sleep 0.3; done
echo "   목업 pid=$MOCK_PID  base=$BASE"

echo "== 2) parse-svc 를 그 주소로 재기동 =="
KBP_GATE_OCR_LANE=paddle_gw KBP_PADDLE_OCR_GATEWAY_URL="$BASE" \
  bash scripts/run-parse-svc.sh 2>&1 | sed 's/^/   /'

echo "== 3) parse-svc 프로세스의 실효 env 확인 =="
PSPID="$(pgrep -f 'parse_service.app:app' | head -1)"
if [ -n "$PSPID" ]; then
  ps eww "$PSPID" | tr ' ' '\n' | grep -E '^KBP_(GATE_OCR_LANE|PADDLE_OCR_GATEWAY_URL)=' | sed 's/^/   /'
else
  echo "   ✗ parse-svc 프로세스를 못 찾았다"; exit 1
fi

echo "== 4) 스캔 PDF 를 흘려 목업에 요청이 도착하는지 =="
SAMPLE="${OCR_SAMPLE_PDF:-}"
if [ -z "$SAMPLE" ]; then
  SAMPLE="$(find "$ROOT/test_doc" -iname '*.pdf' 2>/dev/null | head -1)"
fi
if [ -z "$SAMPLE" ] || [ ! -f "$SAMPLE" ]; then
  echo "   ! 스캔 PDF 샘플이 없다 — OCR_SAMPLE_PDF=<경로> 로 지정하면 4단계까지 검증한다."
  echo "   (1~3단계로 '설정이 프로세스에 도달했다' 까지는 확인됐다.)"
  exit 0
fi
echo "   샘플: $SAMPLE"
curl -s -m 600 -F "file=@$SAMPLE" -F "filename=$(basename "$SAMPLE")" \
  http://localhost:19001/parse > /tmp/ocr_parse_out.json 2>&1 || true

echo "== 5) 판정 =="
if grep -qE "POST /ocr/paddleocr_vl/tasks" "$MOCK_LOG" 2>/dev/null; then
  echo "   ✅ 목업에 요청이 도착했다 — URL 변경이 실제로 반영된다."
  grep -E "POST|GET" "$MOCK_LOG" | head -5 | sed 's/^/      /'
else
  echo "   ✗ 목업에 요청이 없다. 가능한 원인:"
  echo "      - 그 PDF 가 스캔 페이지가 아니라 OCR 레인을 타지 않았다(텍스트 PDF)"
  echo "      - 게이트가 다른 레인으로 라우팅했다(KBP_GATE_OCR_LANE 확인)"
  echo "      - env 가 프로세스에 도달하지 않았다(3단계 출력 확인)"
  echo "   --- parse 응답 앞부분 ---"
  head -c 400 /tmp/ocr_parse_out.json | sed 's/^/      /'
  exit 1
fi
