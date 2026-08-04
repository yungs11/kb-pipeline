# OCR Gateway API 테스트 스크립트

대상: **OCR Gateway (`:18081`)** — PaddleOCR-VL(vLLM `:8104`)을 프론트하는 REST 게이트웨이.
계약 출처: "OCR Gateway API — PaddleOCR-VL" API 정의서.

의존: `bash` + `curl` + `python3`(표준 라이브러리만). **pip 불필요 → 폐쇄망 안전.**

---

## 1. `test_ocr_api.sh` — 단건 기능 테스트

모든 엔드포인트를 순서대로 왕복한다: `/health` → `/engines` → `/ocr/{engine}/health`
→ **동기** `POST /ocr/{engine}` → **비동기** `POST /tasks` → 폴링 → `/result`(+ 없는 id 404 계약 확인).

```bash
./test_ocr_api.sh sample.pdf                      # 기본 127.0.0.1:18081
HOST=10.0.0.5:18081 ./test_ocr_api.sh sample.pdf  # 원격
CHART=1 ./test_ocr_api.sh report.pdf              # opts use_chart_recognition
SKIP_ASYNC=1 ./test_ocr_api.sh doc.pdf            # 동기만
```
환경변수: `HOST` `ENGINE`(기본 paddleocr_vl) `LANG_`(korean) `SYNC_TIMEOUT`(600) `POLL_TIMEOUT`(900).
성공 시 exit 0, 하나라도 실패 시 exit 1.

---

## 2. `batch_concurrency_test.py` — 동시처리/배치 부하

파일 N개를 동시에 던져 **처리량·지연·kv_cache 사용률**을 측정한다. 게이트웨이 `MAX_CONCURRENT`(기본 8)와
vLLM continuous batching 의 실동작을 검증하는 용도.

```bash
# doc.pdf 를 24건, 동시 8로 (동기)
./batch_concurrency_test.py doc.pdf --repeat 24 --concurrency 8

# 여러 파일 비동기 제출/폴링
./batch_concurrency_test.py a.pdf b.png c.pdf --mode async -c 12

# 원격 + opts
./batch_concurrency_test.py doc.pdf --host 10.0.0.5:18081 --opts '{"use_chart_recognition": true}'
```

주요 옵션: `--concurrency/-c`(동시 수) · `--repeat N`(총 건수) · `--mode sync|async` ·
`--host` · `--opts JSON` · `--timeout`.

출력: 총건수/성공/실패, 전체 wall, **throughput(docs/s)**, 요청지연 p50/p95/max, 서버 elapsed_s,
**kv_cache 최대 사용률**. kv_cache 가 95%↑면 동시성을 낮추거나 `PADDLE_UTIL` 상향(문서 튜닝 주의).

> 튜닝 권장: 동시성 **8~12**. 너무 높이면 게이트웨이 로컬 layout 검출(CPU) 경쟁으로 페이지당 시간이 늘 수 있음.

---

## ⚠️ 포트 주의 — parse-svc 와 18081 충돌

kbp compose 에서 **parse-svc 를 호스트 `18081`** 로 매핑해 두었는데, 이 **OCR Gateway 도 `18081`** 을 쓴다.
**같은 호스트라면 충돌**한다 — 둘 중 하나의 호스트 포트를 바꿔야 한다(예: parse-svc 를 `19001` 등으로).
컨테이너 내부 포트는 그대로 두고 compose 의 좌측(호스트) 숫자만 변경하면 된다.
