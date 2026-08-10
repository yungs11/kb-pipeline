#!/usr/bin/env python3
"""OCR 게이트웨이 목업 — `KBP_PADDLE_OCR_GATEWAY_URL` 이 **실제로 그 주소로 가는지** 증명한다.

왜 필요한가
-----------
`.env` 의 `KBP_PADDLE_OCR_GATEWAY_URL` 을 바꿨을 때 그게 정말 반영되는지는
**요청이 그 호스트에 도착하는 것**으로만 확인된다. 응답 200 은 증거가 아니다 —
옛 주소가 살아 있으면 그쪽으로 가고도 성공한다. 그래서 요청을 받아 기록하는 쪽을 세운다.

계약(`parse_service/parsers/pdf/paddle_gw.py` 실측)
  POST {base}/tasks             → {"task_id": "..."}
  GET  {base}/tasks/{id}        → {"status": "completed"}    # 즉시 응답(폴링)
                                  ★ 파서는 정확히 "completed"/"failed" 를 본다
                                    (paddle_gw.py:111,113). "success" 를 돌려주면
                                    폴링이 영원히 끝나지 않는다 — 실측으로 밟았다.
  GET  {base}/tasks/{id}/result → {"status": "ok", "text": "..."}
`{base}` 는 env 값 그대로다(예 `http://localhost:18099/ocr/paddleocr_vl`) — 경로 접두어까지
포함해 검증되므로, 사내 게이트웨이의 `/ocr/<engine>` 부분을 잘못 적었으면 여기서 드러난다.

사용
----
    python3 scripts/ocr-test/mock_ocr_gateway.py --port 18099 --log /tmp/mock_ocr.log
    # 다른 셸에서:
    KBP_GATE_OCR_LANE=paddle_gw \\
    KBP_PADDLE_OCR_GATEWAY_URL=http://localhost:18099/ocr/paddleocr_vl \\
      bash scripts/run-parse-svc.sh
    # 스캔 PDF 를 /parse 로 보내고 /tmp/mock_ocr.log 에 요청이 찍히는지 본다.

`--fail-rate` 로 게이트웨이 장애를 흉내낼 수도 있다(파서가 조용히 폴백하지 않는지 확인).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE: dict[str, dict] = {}
ARGS = argparse.Namespace()


def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if getattr(ARGS, "log", None):
        with open(ARGS.log, "a") as fh:
            fh.write(line + "\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *a):  # 기본 stderr 로그 억제(우리 _log 를 쓴다)
        pass

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        # ★ 이 줄이 증거다 — 파서가 어느 경로로 왔는지 그대로 남긴다.
        _log(f"POST {self.path}  (body {len(body)}B, host={self.headers.get('Host')})")
        if not self.path.endswith("/tasks"):
            self._send(404, {"error": f"unexpected path {self.path}"}); return
        if ARGS.fail_rate and (len(STATE) % max(1, int(1 / ARGS.fail_rate))) == 0:
            _log("  → 의도적 500(게이트웨이 장애 흉내)")
            self._send(500, {"error": "mock gateway failure"}); return
        tid = uuid.uuid4().hex[:12]
        STATE[tid] = {"created": time.time(), "base": self.path[: -len("/tasks")]}
        self._send(200, {"task_id": tid})

    def do_GET(self) -> None:
        _log(f"GET  {self.path}")
        parts = self.path.rstrip("/").split("/")
        if self.path.endswith("/result"):
            tid = parts[-2]
            if tid not in STATE:
                self._send(404, {"status": "error", "error": f"unknown task {tid}"}); return
            self._send(200, {"status": "ok", "text": ARGS.text})
            return
        tid = parts[-1]
        if tid not in STATE:
            self._send(404, {"status": "error", "error": f"unknown task {tid}"}); return
        # ARGS.delay 초 동안은 pending 을 돌려 폴링 경로도 태운다.
        pending = (time.time() - STATE[tid]["created"]) < ARGS.delay
        self._send(200, {"status": "pending" if pending else "completed"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18099)
    ap.add_argument("--log", default="/tmp/mock_ocr.log")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="폴링 경로를 태우기 위해 pending 을 유지할 초")
    ap.add_argument("--fail-rate", type=float, default=0.0,
                    help="0.5 면 두 번에 한 번 500 — 파서가 조용히 폴백하지 않는지 확인")
    ap.add_argument("--text", default="목업 게이트웨이가 돌려준 OCR 텍스트입니다. 표1 항목 A 값 1.")
    global ARGS
    ARGS = ap.parse_args()
    if ARGS.log:
        open(ARGS.log, "w").close()
    srv = ThreadingHTTPServer(("127.0.0.1", ARGS.port), Handler)
    _log(f"목업 OCR 게이트웨이 시작 — http://localhost:{ARGS.port}  (log={ARGS.log})")
    _log("   기대 경로: POST <base>/tasks · GET <base>/tasks/{id} · GET <base>/tasks/{id}/result")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        _log("종료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
