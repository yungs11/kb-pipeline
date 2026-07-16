# MinerU PDF 레인 — 배포서버 전제조건

> parse-svc PDF 파서의 MinerU 레인(스캔/혼합 PDF)을 **배포서버**에서 구동하기 위한 전제조건.
> 설계: `docs/superpowers/specs/2026-07-13-mineru-pdf-integration-design.md`, 플랜: `docs/superpowers/plans/2026-07-13-mineru-pdf-integration.md`.

## 왜 별도 노트인가

로컬 dev(Intel Mac)는 GPU/CUDA/torch·MinerU 를 구동할 수 없다. 따라서 게이트/매핑/폴백은
로컬에서 fake(monkeypatch)로 단위검증하고, **실 MinerU 경로는 배포서버 스택검증**(플랜 Task 8)으로 분리한다.
로컬에서는 스캔 PDF 가 MinerU 레인으로 라우팅돼도 `mineru` import 실패 → `_invoke_mineru` 예외 →
`parse()` 가 삼켜 **기존 ODL/VL 레인으로 폴백**(가용성 회귀 없음).

## 전제조건 (배포서버) — Task 8 소스대조로 확정(mineru 3.4.4)

1. **MinerU 런타임 설치** — parse-svc Docker 이미지(`Dockerfile.parse-svc`)에 이미 반영:
   ```
   # parse-svc 는 GPU 없음 → CPU torch(CUDA/nvidia 미포함으로 이미지 축소) → mineru[pipeline]
   uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
   uv pip install "mineru[pipeline]"
   ```
   - hybrid-http-client 는 **`mineru[pipeline]`** 이면 충분(torch/torchvision/transformers/onnxruntime + PaddleOCR layout 의존).
     `mineru-vl-utils`(원격 VLM http 클라이언트)는 base 의존이라 자동 포함. `mineru[core]`(vlm 엔진+gradio)는 불필요.
   - ⚠️ **opencv(cv2) 시스템 라이브러리 필수** — mineru pipeline 이 cv2 를 import 하는데 `python:3.12-slim` 엔 그래픽 라이브러리가
     없어 `libxcb.so.1`/`libGL` import 실패(Task 8 검증서 발견). Dockerfile 에 apt 설치 추가:
     `libgl1 libglib2.0-0 libxcb1 libsm6 libxext6 libxrender1`.
   - **requirements.txt 엔 넣지 않는다** — 로컬 Intel Mac/py3.14 dev 는 torch 미설치 유지 → MinerU 레인 ODL 폴백.
   - 확인: `python -c "import mineru, torch; from mineru.cli.common import do_parse; print('ok')"`
2. **PaddleOCR/layout 모델** — 런타임 auto-download(`auto_download_and_get_model_root_path`). 오프라인/지연 억제 시
   빌드에서 사전다운로드: `mineru-models-download -s modelscope -m pipeline`(모델캐시 경로 env 고정). hybrid 는 layout+OCR-det 에 사용.
3. **do_parse 실계약(확정)** — 코드는 아래에 맞춤:
   - **필수 위치인자 `p_lang_list`**(기본값 없음) — 누락 시 TypeError. `_invoke_mineru` 가 `[MINERU_LANG or "korean"]` 전달.
     유효 lang: `"korean"`(Korean,English) / `"ch"`(중·영·일·번체·라틴).
   - `model` 파라미터 **없음** — http-client 는 vLLM 서빙 모델(MinerU2.5) 사용, MinerUClient 가 내부 처리.
   - 출력경로 = `{output_dir}/{stem}/hybrid_{parse_method}/{stem}_content_list.json` — 코드 재귀 glob `**/*content_list.json` 로 포착.
   - 동시성 = `max_concurrency`(기본 100, do_parse **kwargs) — `MINERU_MAX_CONCURRENCY` env 로 조절.
3. **별도 VLM GPU 서버** 가동 — MinerU 가 `server_url` 로 호출하는 원격 vLLM(OpenAI 호환).
   **실 엔드포인트(2026-07-13 확정)**: base=`https://api-mineru.ys-helperai.com`, model=`MinerU2.5`(자동조회), **인증 없음**.
   `/v1/models` 라이브 확인(root: opendatalab/MinerU2.5-Pro-2605-1.2B, max_model_len 8192).
   ```
   # ⚠️ /v1 없이 base 만 — mineru_vl_utils.http_client 가 f"{server_url}/v1/chat/completions" 를 만든다(Task 8 소스확인).
   #    /v1 붙이면 /v1/v1/... 이중경로가 됨. 모델명은 _get_model_name 이 /v1/models 로 자동조회(model kwargs 불요).
   MINERU_VLM_SERVER_URL=https://api-mineru.ys-helperai.com
   # MINERU_LANG=korean          # PaddleOCR OCR-det 언어(기본 korean). "ch"=중·영·일·번체·라틴.
   # MINERU_MAX_CONCURRENCY=100  # mineru_vl_utils 동시요청(기본 100) — vLLM --max-num-seqs 와 매칭.
   ```
   ⚠️ 반드시 `scripts/parse-svc.env` 에 둔다(`.gitignore` `scripts/*.env` 로 무시 + 런처
   `scripts/run-parse-svc.sh:44` 가 `set -a; . scripts/parse-svc.env` 로 로드). `parse_service/parse-svc.env`
   는 gitignore 도 로드도 안 되니 쓰지 말 것(비밀 유출·미로드).

   **server_url 형식 주의**: 위 OpenAI SDK base_url 은 `/v1` 포함이다. MinerU `MinerUClient`(`mineru_vl_utils`)가
   `server_url` 에 base(`/v1` 없이)를 원하는지 `/v1` 포함을 원하는지는 Task 4 Step 1(`inspect`)에서 확정. 우선 `/v1` 포함으로 두고 실패 시 base 로 조정.

   raw 호출 형태(참고 — MinerU 가 내부에서 이 형태로 :8103 을 부른다. mineru_vl_utils 가 프롬프트/파싱 담당):
   ```python
   from openai import OpenAI
   client = OpenAI(base_url="https://api-mineru.ys-helperai.com/v1", api_key="dummy")
   resp = client.chat.completions.create(
       model="MinerU2.5",
       messages=[{"role": "user", "content": [
           {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
           {"type": "text", "text": "<프롬프트>"}]}],
       max_tokens=4096)
   ```

## 처리량 튜닝 (배치 속도)

배치는 **두 층**에서 일어난다(설계 §2 검증): (a) parse-svc 로컬 — MinerU 가 페이지를 `window_size`(기본 64) 로 묶어
PaddleOCR layout 을 로컬 배치 예측하고 window 의 VLM 요청을 **동시 발사**. (b) GPU — vLLM 이 그 동시요청을
**continuous batching** 으로 토큰단위 동적 묶음. GPU 실효 배치 크기는 window_size(64) 가 아니라 아래 두 값이 결정한다.

- **vLLM `--max-num-seqs`** (GPU 서버): GPU 가 동시에 물 수 있는 시퀀스 수 = 실효 배치 상한. 넉넉히(예: 64~256, GPU VRAM/KV캐시 여유에 맞춰). 낮으면 MinerU 가 아무리 동시 요청해도 GPU 에서 큐잉돼 처리량이 안 오른다.
- **MinerU/mineru_vl_utils 동시성** (parse-svc): window 의 VLM 요청을 실제로 얼마나 병렬로 쏘는지. 이게 vLLM 에 충분한 in-flight 요청을 공급해야 continuous batching 이 효과를 낸다. **Task 4 Step 1 에서 mineru_vl_utils 가 동시(async) 발사인지 순차인지 확인** — 순차면 처리량 병목이므로 동시성 옵션/환경변수를 켠다. MinerU 는 보통 `MINERU_VLM_SERVER_URL` 계열과 함께 요청 동시성/배치 관련 옵션을 노출하므로 실제 옵션명을 do_parse/MinerUClient 시그니처로 확정.
- **`window_size`(64)**: parse-svc 로컬 PaddleOCR 배치·메모리 파이프라인 단위. GPU 배치와 직접 무관하나 너무 작으면 in-flight 공급이 줄어 GPU 유휴가 생길 수 있다. 기본 64 유지, 지연/메모리 보며 조정.

> 튜닝 순서: ① vLLM `--max-num-seqs` 를 목표 동시성 이상으로 → ② mineru_vl_utils 동시 발사 확인/활성화 → ③ 실측(스캔 N페이지 문서 parse 시간)으로 window_size·동시성 미세조정. GPU 가 포화되면(util ~100%) max-num-seqs 가 상한.

## 배포 후 스택검증 (플랜 Task 8)

1. `import mineru` + PP-OCRv5 모델 존재 + `MINERU_VLM_SERVER_URL` 헬스체크.
2. ✅ **do_parse 계약 대조 완료(Task 8 Step 1)** — mineru 3.4.4 소스 직독으로 확정, 코드 반영됨(위 3항).
   content_list v1 스키마 확정: text(+text_level)/equation(text,text_format:latex)/image(img_path)/
   table(table_body:HTML)/**chart(별도 타입, content+img_path)**/list(list_items:문자열배열)/code(code_body).
   `_content_list_to_elements` 가 chart→text(content)/list→불릿/heading→text_level 로 매핑(유실 없음).
   → **런타임 잔여**: MinerUClient 가 vLLM 에 실제 요청 시 model 이름을 `/v1/models` 자동조회로 채우는지
     (server_url `/v1` 포함 여부 포함) 첫 실호출로 확인 — 실패 시 `server_headers`/model kwargs 로 조정.
3. ✅ **스택 스모크 통과(2026-07-13)** — 실 이미지(kbp-parse-svc, 6.14GB) 컨테이너(gunicorn -w4) 기동 →
   스캔 PDF `POST /parse` → **enriched_content 에 `<table>` HTML + 셀값 추출**(n_blocks=3, 표 비어있지 않음).
   전 구간(게이트→MinerU 레인→라이브 VLM→blocks→enriched) 실서비스 동작 확인.
4. 혼합 PDF(네이티브 텍스트 + 스캔) 1건 → 게이트가 `'ocr'` 강제 라우팅, 스캔 페이지 텍스트 유실 없음. (잔여)
5. **처리량**: vLLM `--max-num-seqs` 와 `MINERU_MAX_CONCURRENCY`(mineru_vl_utils 동시요청, 기본 100) 를 맞춰 GPU 포화.

## 런타임 주의(스택 스모크서 발견·해결)

- ⚠️ **async 이벤트루프 충돌(치명, 해결됨)** — mineru_vl_utils `http_client.batch_predict` 는 `get_running_loop()`
  성공 시 `loop.run_until_complete` 를 쓴다. FastAPI async 핸들러(`/parse`)의 실행 중 루프 위에서 동기 `do_parse`
  를 부르면 `RuntimeError: This event loop is already running` → MinerU 레인 폴백 → **빈 결과**.
  → `mineru_lane._invoke_mineru` 가 `do_parse` 를 **ThreadPoolExecutor 워커스레드**(실행 중 루프 없음)에서 실행
  → mineru_vl_utils 가 `asyncio.run()` 깨끗한 경로. (회귀테스트 `test_invoke_runs_do_parse_off_running_event_loop`)
- **MinerU multiprocessing** — MinerU 는 PDF 렌더에 spawn multiprocessing(persistent executor, max_workers=3)을 쓴다.
  gunicorn `-w4` uvicorn 워커 밑에서 정상 동작 확인(크래시 없음). `parse_service.app` 이 do_parse 를 import 가 아닌
  요청 핸들러에서 부르므로 spawn 재-import 문제 없음.
- **첫 요청 모델 다운로드** — PaddleOCR/layout 모델(~215MB+)이 첫 `/parse` 때 런타임 다운로드(~1분+ 지연). 볼륨/사전다운로드로 억제.
- ⚠️ **대용량 스캔 PDF 렌더 타임아웃(발견됨)** — MinerU 는 window(기본 64페이지)를 이미지로 렌더할 때 내부 타임아웃
  `MINERU_PDF_RENDER_TIMEOUT`(**기본 300s**)을 건다. 고해상도 스캔 다수페이지(예: 86p)를 느린 CPU 에서 렌더하면
  300s 초과 → `TimeoutError: PDF image rendering timeout` → MinerU 레인 폴백(ODL/VL)로 빠져 스캔 품질 유실.
  → 대용량 대비 env 상향: `MINERU_PDF_RENDER_TIMEOUT=1800`, `MINERU_PDF_RENDER_THREADS=<코어수>`(기본 3).
  근본 완화는 코어 많은 배포서버 + (선택) window_size 축소.
- **완전 오프라인 모델**: `~/.cache/mineru.json`(`models-dir.pipeline`=캐시 경로) + `MODELSCOPE_OFFLINE=1`+`HF_HUB_OFFLINE=1` → 런타임 모델 네트워크 0.
- ⚠️ **pipeline 은 hybrid 보다 모델이 더 많음** — hybrid 는 OCR det/rec + layout(3개)만, **pipeline 은 표인식·수식(MFR/MFD) 등 +7개** 추가 필요. 빌드시 사전다운로드는 backend 별로:
  `mineru-models-download -s modelscope -m pipeline`(pipeline 전체) — hybrid 만 받으면 pipeline 첫 요청 때 표/수식 모델을 런타임 다운로드(오프라인이어도 캐시에 없으면 받음). 두 레인 다 쓰면 **pipeline 모델셋을 미리 받아두면 둘 다 커버**.

## PaddleOCR-VL 게이트웨이 스캔 레인 (2026-07-15)

스캔 레인이 MinerU pipeline(CPU) → **PaddleOCR-VL 게이트웨이**로 교체됨(`parsers/pdf/paddle_gw.py`).
- **엔드포인트**: `KBP_PADDLE_OCR_GATEWAY_URL=https://api-doc.ys-helperai.com/ocr/paddleocr_vl` (multipart file+lang, 무인증 ⚠️ 접근제한 권고)
- **방식**: 페이지 렌더(로컬 PyMuPDF) → 페이지별 병렬 POST(동시성 `KBP_VL_MAX_CONCURRENT`) → markdown+HTML표 → `hybrid_to_blocks`. 문서 통짜 대신 페이지별 = page_number 계약 보존 + 병렬 가속.
- **실측**(신탁 3p 스캔, GPU 채점 경쟁 중): 게이트웨이 48s vs MinerU pipeline(CPU) 181s vs hybrid 166s. 표 `<table>` 구조보존·한국어 정확.
- **폴백**(사용자 결정): 게이트웨이 실패/빈결과 → **ODL/in-process VL** (MinerU 는 스캔 폴백 체인 제외). 실전 검증됨 — 게이트웨이 vlm worker 장애 시 자동 폴백으로 정상 결과(308s).
- MinerU hybrid 레인은 디지털 차트문서(비율≥0.5)용으로 유지. pipeline 레인 코드는 잔존(미사용) — 안정화 후 torch/mineru 를 이미지에서 제거하면 6GB→경량 원복 가능.

### paddle_gw 동시성 실측 (2026-07-15)
- 게이트웨이 병렬화(서버 멀티워커) 후: 13p 문서 **동시성 3 = 238s, 실패 0, 콘텐츠 온전(31KB)**.
- ⚠️ **동시성 5 = 69s 로 빨라 보이나 조용한 품질 유실** — 밀집 페이지(p9~13 양식/표)가 status=ok 인 채
  빈껍데기(수백 자)로 반환됨(GPU 5-way 경쟁 시 게이트웨이가 오류 신호 없이 내용 축소). **탐지 불가형 실패**라
  동시성은 3 유지(KBP_VL_MAX_CONCURRENT 기본값). 올리려면 게이트웨이 쪽 품질 보증(짧은 응답 재시도 등) 선행 필요.
- Cloudflare 터널 100s 제한(error 524): 페이지당 처리가 100s 를 넘으면 CF 가 끊음 — 동시성 과다로 페이지당
  시간이 늘면 524 재발. 이것도 동시성 상한의 근거.

### paddle_gw 동시성 확정 (2026-07-15, 서버 병렬화 후)
- 서버측 멀티워커 반영 후 실측(소유권 13p, dpi150): **동시성 4 = 282s, 페이지별 품질 기준과 완전 일치(유실 0, 실패 0)**.
  동시성 3=238s 와 오차 범위 — 벽시계는 밀집 페이지(p10 출력 10KB)가 지배.
- 서버 분석: 동시성 8은 페이지당 66-78s 로 CF 100s 에 근접(524 위험) + CPU layout 경쟁 → **4 확정**(`KBP_VL_MAX_CONCURRENT=4`).
- 추가 속도는 서버측(CPU layout 가속/무거운 페이지) 영역 — 클라이언트 노브(dpi·동시성·프로브) 소진.

## 스캔 게이트웨이 엔진 확정: paddleocr_vl (2026-07-16)

같은 api-doc 게이트웨이에 dots_ocr / paddleocr_vl 둘 다 노출됨. 46p 소장(밀집 텍스트/표) 실측 비교로 **기본 = paddleocr_vl** 확정:

| | dots_ocr | **paddleocr_vl(기본)** |
|---|---|---|
| 46p 시간 | 1236s(20.6분) | **684s(11.4분)** |
| 빈/실패 페이지 | 4개 | **0개** |
| 내용량 | 37KB | **93KB** |

- **문서 성격별 강약**: 밀집 텍스트/표(소장·계약서)=paddle 우위(빠름·완주·다량). 양식/순서도(프로세스정의서)=dots 환각 적음. 기본은 다수 케이스인 paddle.
- **엔진 전환**: `KBP_PADDLE_OCR_GATEWAY_URL` 만 `/ocr/{engine}` 로 바꾸면 됨(dots/paddle/mineru_vlm 동일 계약: file+lang→markdown+HTML표, 비동기 tasks 공용).
- **비동기(tasks) 호출**: submit→poll→result 로 CF 100s 우회(paddle_gw._post_page). 대용량(46p) 은 gunicorn `--timeout 3600` 로 완주 보장(20분+ 걸려도 부분성공 저장). 서버 `DGX_MAX_CONCURRENT=4` 가 동시처리 상한 — 클라 동시성 4 초과는 큐 대기만 늘림.
- **부분 실패 저장**: 페이지 단위 빈 결과는 비치명 — 나머지 페이지는 enriched_content 로 정상 적재(빈 페이지만 누락).
