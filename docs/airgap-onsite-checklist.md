# 폐쇄망 반입 현장 체크리스트 (2026-08-10 번들)

> 이 번들이 **이전 반입분과 달라진 점**과, 현장에서 **꼭 확인해야 하는 것**만 적는다.
> 전체 절차는 `docs/airgap-deploy.md`, 두 스택 동시 배포는 `scripts/airgap/deploy-both.sh`.

## 0. 이번 번들에서 달라진 것 (읽지 않으면 사고 나는 것)

### ⚠️ 포트가 바뀌었다 — 방화벽을 다시 열어야 한다

| 서비스 | 이전 | **이번** |
|---|---|---|
| facade | 3000 | 3000 (동일) |
| edgequake API | 3001 | 3001 (동일) |
| edgequake webui | 3002 | 3002 (동일) |
| minio 콘솔 | 3003 | 3003 (동일) |
| **parse-svc (OCR/파싱 엔진)** | **18081** | **19001** ← 바뀜 |
| kb 웹앱 | `.env` 의 `FRONTEND_PORT`(18080) | 18080 (compose 기본값도 18080 으로 통일) |

```bash
# 방화벽 — 19001 을 추가하고 18081 은 그대로 둔다(아래 이유)
for p in 3000 3001 3002 3003 19001 18081 18080; do firewall-cmd --permanent --add-port=$p/tcp; done
firewall-cmd --reload
```

**왜 parse-svc 가 18081 을 떠났나** — `KBP_PADDLE_OCR_GATEWAY_URL` 이 가리키는 **사내 OCR
게이트웨이가 이미 호스트 18081** 을 쓴다. parse-svc 도 같은 포트를 발행해 **나중에 바인드하는
쪽이 실패**하고, 그 결과 OCR 요청이 parse-svc 로 가 파싱이 이상해졌다(현장에서 본
`18081 ALREADY PORT` 가 이것이다). 이제 18081 은 **OCR 게이트웨이 단독 소유**다.

- parse-svc 직접 호출 주소: `http://localhost:19001/healthz`, `.../parse`
- OCR 게이트웨이(18081)는 **kbp 가 소유하지 않는다** — 방화벽에서 지우지 말 것.

### ⚠️ `fitz`(PyMuPDF) 누락 재발 방지가 빌드에 박혔다

지난 반입에서 `ModuleNotFoundError: No module named 'fitz'` 가 났던 건 세 패키지
(`PyMuPDF`·`python-docx`·`openpyxl`)가 `pyproject.toml` 의 **dev extras** 에 있어
`pip install .` 이 건너뛰었기 때문이다. 폐쇄망엔 pip 이 없어 복구도 불가능했다.

- 지금은 세 패키지가 **`[project] dependencies`** 에 있다(extras 아님).
- **`build-bundle.sh` 가 `verify-bundle.sh --imports` 를 강제 실행**한다 —
  `import fitz, docx, openpyxl` 이 실패하면 **번들이 만들어지지 않는다.**
  (이전에는 가드가 있었는데 **아무도 돌리지 않아** 통과했다. 그게 더 위험한 형태다.)

현장에서 한 번 더 확인하려면:
```bash
bash scripts/airgap/verify-bundle.sh --imports      # kb 번들 쪽에서 실행
```

### ⚠️ 그래프 보기(webui) 주소 방식이 바뀌었다

이전 프론트는 webui 주소를 `http://localhost:13000` 으로 **하드코딩**하고 있었다. 그 값은
`next build` 시 번들에 굽히므로 compose env 로 못 바꿨고, 폐쇄망 webui 는 3002 라서
**현장에서 "그래프 보기" 가 원래 열리지 않았다.**

이번 프론트는 **보고 있는 호스트의 `:3002`** 를 쓴다(`window.location` 파생) →
`http://<서버IP>:18080` 으로 접속했다면 webui 는 `http://<서버IP>:3002` 로 연다.
**그래서 3002 가 방화벽에 열려 있어야 한다**(원격 PC 브라우저가 직접 연다).

## 1. 사전 점검 (podman / CNI)

이 환경은 **podman + CNI** 다. 컨테이너 이름 DNS 가 CNI 에서는 별도 플러그인에 의존한다.

```bash
podman info --format '{{.Host.NetworkBackend}}'    # cni 인지 확인
dnf install -y podman-plugins                      # cni 면 dnsname 플러그인 필요
# netavark 라면: dnf install -y aardvark-dns
```

`load-and-up.sh` 가 기동 전에 **실제 컨테이너 두 개를 띄워 이름 해석을 검사**하고, 실패하면
위 명령을 그대로 안내하며 멈춘다. 이 스택은 전부 컨테이너 이름으로 통신하므로(예
`facade:19000`, `parse-svc:19001`) 이름 해석이 안 되면 모든 서비스가 조용히 죽는다.

## 2. 순서

```bash
# ① 무결성 (빌드머신 절대경로가 아니라 basename 으로 기록돼 있다)
sha256sum -c kbp-airgap-bundle-*.tar.gz.sha256
tar xzf kbp-airgap-bundle-*.tar.gz -C kbp && cd kbp

# ② .env 작성 — .env.airgap.example 을 복사해 채운다
cp .env.airgap.example .env && vi .env

# ③ env 가드 (필수키 + 포트·TZ·OCR 레인 일관성)
bash scripts/airgap/verify-bundle.sh --env .env

# ④ 기동 (podman load → DNS 사전점검 → compose up → health 폴링 → MinIO 버킷)
bash scripts/airgap/load-and-up.sh

# ⑤ 두 스택을 함께 올릴 때는 이것 하나로
bash scripts/airgap/deploy-both.sh
```

### 2.1 파서 전용 번들(`kbp-parse-bundle-*.tar.gz`)은 ② 가 다르다

```bash
sha256sum -c kbp-parse-bundle-amd64.tar.gz.sha256
mkdir kbp && tar xzf kbp-parse-bundle-amd64.tar.gz -C kbp && cd kbp

# ★ 이 번들에는 **채워진 `.env` 가 이미 들어 있다**(권한 600, 실 비밀값).
#    `cp .env.parse-only.example .env` 로 덮어쓰면 값이 전부 날아간다.
test -f .env && vi .env || { cp .env.parse-only.example .env && vi .env; }

bash scripts/airgap/verify-bundle.sh --parse-only
bash scripts/airgap/parse-only-up.sh
```

현장에서 실제로 고칠 값은 보통 **`KBP_PADDLE_OCR_GATEWAY_URL` 하나**다. 고친 뒤에는
`restart` 가 아니라 `parse-only-up.sh` 를 다시 돌린다(컨테이너 env 는 생성 시점에 고정).
반영 확인: `bash scripts/ocr-test/verify-ocr-gw-url.sh --container`.

## 3. `.env` 에서 이번에 새로 봐야 하는 키

| 키 | 기본값 | 의미 |
|---|---|---|
| `KBP_PADDLE_OCR_GATEWAY_URL` | — | 사내 OCR 게이트웨이. **호스트 18081** 을 쓴다(parse-svc 아님) |
| `KBP_GATE_OCR_LANE` | `vl` | `paddle_gw` 로 켜면 위 주소가 **반드시** 있어야 한다. 없으면 스캔 PDF 가 **조용히 폴백**해 파싱이 성공처럼 보인다 → 가드가 차단한다 |
| `KBP_COMMUNITY_TZ` | `Asia/Seoul` | 야간 커뮤니티 배치의 창 판정 시간대. **컨테이너 `TZ` 가 아니다** — 컨테이너는 UTC 로 두어 로그 시각축을 통일한다. 비워도 정확하다 |
| `KBP_COMMUNITY_BUILD_ENABLED` | `true` | 커뮤니티 빌드를 밤 1회로 모은다. 파서 전용 배포에서는 `false` |
| `KBP_GLOBAL_SEARCH_CONCURRENCY` | `2` | 전체 요약(global) 검색 전역 동시 상한. `0` 이면 그 기능이 항상 503 |
| `KBP_REQUIRE_EDGEQUAKE` | `0` | `1` 이면 edgequake 미도달 시 facade 기동을 중단. **파서 전용 배포에서는 절대 1 로 두지 말 것** |
| `EDGEQUAKE_BASE_URL` | `http://edgequake:8081` | kb→edgequake 직결(검색). **평문 http**. `https` 로 적으면 검색이 전부 502 |
| `FRONTEND_PORT` (kb) | `18080` | 웹앱 접속 포트 |
| `CORS_ORIGINS` (kb) | `http://localhost:18080` | `FRONTEND_PORT` 와 같게 |

## 3.0 OCR 주소는 **레인마다 하나** — 먼저 레인을 정한다

"OCR 주소를 바꾼다" 는 env 가 하나가 아니다. **레인을 먼저 정하고 그 레인의 env 하나**를 바꾼다
(`parse_service/parsers/pdf/gate.py:24`).

| `KBP_GATE_OCR_LANE` | 바꿀 env | 무엇 |
|---|---|---|
| `paddle_gw` (기본) | **`KBP_PADDLE_OCR_GATEWAY_URL`** | 사내 OCR 게이트웨이(PaddleOCR-VL 프론트, 호스트 18081) |
| `vl` | **`MODEL_API_URL`** | in-process VL(qwen) — 게이트웨이 없이 페이지를 직접 본다 |
| `odl` | 없음 | OpenDataLoader 로컬 CLI(외부 주소 불필요) |

`KBP_OCR_URL`(:18050)·`KBP_EXCEL_URL`(:18055)은 **죽은 env** 다 — Phase 2c/2e 에서 OCR·엑셀이
parse-svc in-process 로 들어가 코드가 무시한다. 어디에 남아 있으면 무시하면 된다(2026-08-10
런처에서 제거했다).

## 3.1 OCR 게이트웨이 주소가 **실제로 반영되는지** 확인하는 법

`.env` 의 `KBP_PADDLE_OCR_GATEWAY_URL` 을 바꿨을 때 **응답 200 은 증거가 아니다** — 옛 주소가
아직 살아 있으면 그쪽으로 가고도 성공한다. 요청이 **그 호스트에 도착하는 것**만이 증거다.

```bash
bash scripts/ocr-test/verify-ocr-gw-url.sh --container
```
→ parse-svc 컨테이너의 **실효 env** 를 출력하고, 그 주소로 계약(`POST <base>/tasks`)을
직접 찔러 도달성을 본다. **HTTP 코드가 돌아오면 도달한다**(4xx 도 도달이다). 연결 실패면
주소·방화벽·컨테이너 DNS 를 본다(CNI 면 `podman-plugins`).

### ⚠️ 컨테이너 env 는 생성 시점에 고정된다

`.env` 를 고친 뒤 **`podman-compose ... up -d`(재생성)** 를 해야 새 값이 프로세스에 들어간다.
`restart` 만으로는 **옛 값이 그대로 남는다.** 코드는 요청마다 `os.environ` 을 읽으므로
(`parse_service/parsers/pdf/paddle_gw.py:90`) 프로세스 캐시 문제는 없다 — 컨테이너만 새로
만들면 즉시 반영된다.

### 게이트웨이 응답 계약 (틀리면 무한 폴링)

파서는 3단 계약을 쓴다. **status 문자열이 정확히 `completed`/`failed` 여야 한다** —
다른 값(예 `success`)을 돌려주면 폴링이 끝나지 않고 파싱이 시한까지 매달린다(실측으로 밟았다).

| 단계 | 요청 | 기대 응답 |
|---|---|---|
| submit | `POST {base}/tasks` (multipart 이미지) | `{"task_id": "..."}` |
| poll | `GET {base}/tasks/{id}` | `{"status": "completed"}` (또는 `pending`/`failed`) |
| result | `GET {base}/tasks/{id}/result` | `{"status": "ok", "text": "..."}` |

`{base}` 는 env 값 **그대로**다 — `/ocr/paddleocr_vl` 같은 경로 접두어까지 포함해서 쓰인다.
엔진 이름을 잘못 적으면 submit 이 404 로 실패한다.

## 4. 기동 후 한 바퀴 (여기까지 봐야 "됐다")

```bash
curl -fsS http://localhost:3000/healthz            # facade  → {"status":"ok"}
curl -fsS http://localhost:3000/jobs/workers       # → "online":true  (없으면 모든 적재가 503)
curl -fsS http://localhost:3001/health             # edgequake
curl -fsS http://localhost:19001/healthz           # parse-svc (18081 아님)
curl -fsS http://localhost:8080/readyz             # kb api (스키마 마이그레이션 완료 판정)
```

그다음 **웹앱에서 문서 1건을 실제로 적재**한다(`http://<서버IP>:18080`):
1. 업로드 → 잡이 `succeeded` 로 끝나는지
2. 챗에서 검색 → 답변 + 원문 인용이 나오는지
3. 문서상세 → **"그래프 보기"** 가 `:3002` 로 열리는지 (이번 번들의 수정 지점)

## 5. 이번 번들에서 **검증되지 않은 것** (그대로 두면 위험한 가정)

정직하게 남긴다. 된 것처럼 쓰지 않는다.

- **CNI 네트워크 백엔드** — 개발기 검증 환경은 `netavark` 였다. CNI 는 **한 번도 검증되지
  않았다.** `load-and-up.sh` 의 DNS 사전점검이 그걸 잡도록 만들어 뒀지만, 현장이 첫 실행이다.
- **실제 온프렘 LLM·임베딩·리랭커·VL·파일변환 서버 연동** — 개발기에서는 목업이었다.
  주소·인증·응답 스키마가 사내 서버와 맞는지는 현장에서 확인해야 한다.
- **이번 포트 변경의 폐쇄망 실측** — 포트 배치는 개발기(docker)에서만 왕복 검증했다
  (facade→edgequake 실호출로 204건 결과 확인). podman/CNI 에서의 첫 확인은 현장이다.
- 대용량·다건 동시 적재의 성능/안정성.

## 6. 막혔을 때 먼저 보는 것

| 증상 | 원인 후보 |
|---|---|
| 모든 적재가 503 | `facade-worker` 미기동 (`/jobs/workers` 가 `online:false`) |
| 컨테이너가 서로 못 찾음 | CNI 인데 `podman-plugins`(dnsname) 미설치 → §1 |
| 검색이 전부 502 | `EDGEQUAKE_BASE_URL` 이 `https://` 이거나 미설정(코드 기본값이 컨테이너 자신을 부른다) |
| 스캔 PDF 파싱이 "성공" 인데 내용이 부실 | `KBP_GATE_OCR_LANE=paddle_gw` 인데 게이트웨이 주소가 비어 조용히 폴백 |
| OCR 주소를 바꿨는데 안 먹음 | 컨테이너를 **재생성**하지 않았다(`restart` 로는 옛 env 가 남는다) → §3.1 |
| 스캔 PDF 파싱이 시한까지 매달림 | 게이트웨이 poll 응답 `status` 가 `completed` 가 아니다(예 `success`) → §3.1 계약표 |
| 페이지 수가 이상 / 신호 수집이 부실 | `fitz` 누락 → `verify-bundle.sh --imports` |
| "그래프 보기" 가 안 열림 | 3002 방화벽 미개방 (프론트가 `<서버IP>:3002` 를 직접 연다) |
| 커뮤니티 리포트가 안 생김 | 야간 배치는 **밤 1회**(기본 03:00 KST)다. 즉시 필요하면 `POST /communities/build` |
