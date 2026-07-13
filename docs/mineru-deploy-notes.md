# MinerU PDF 레인 — 배포서버 전제조건

> parse-svc PDF 파서의 MinerU 레인(스캔/혼합 PDF)을 **배포서버**에서 구동하기 위한 전제조건.
> 설계: `docs/superpowers/specs/2026-07-13-mineru-pdf-integration-design.md`, 플랜: `docs/superpowers/plans/2026-07-13-mineru-pdf-integration.md`.

## 왜 별도 노트인가

로컬 dev(Intel Mac)는 GPU/CUDA/torch·MinerU 를 구동할 수 없다. 따라서 게이트/매핑/폴백은
로컬에서 fake(monkeypatch)로 단위검증하고, **실 MinerU 경로는 배포서버 스택검증**(플랜 Task 8)으로 분리한다.
로컬에서는 스캔 PDF 가 MinerU 레인으로 라우팅돼도 `mineru` import 실패 → `_invoke_mineru` 예외 →
`parse()` 가 삼켜 **기존 ODL/VL 레인으로 폴백**(가용성 회귀 없음).

## 전제조건 (배포서버)

1. **MinerU 런타임 설치** — parse-svc `.venv-kb` 에 `mineru` + torch + PaddleOCR(PP-OCRv5 모델).
   확인: `python -c "import mineru; from mineru.cli.common import do_parse; print('ok')"`
2. **PP-OCR 모델(PP-OCRv5)** 이 서버에 존재 — `hybrid-http-client` 백엔드가 로컬 layout/OCR-det 용으로 사용.
   MinerU 가 모델 경로를 env/기본경로로 요구하면 명시(플랜 Task 4 Step 1 에서 실제 요구 env 확정).
3. **별도 VLM GPU 서버** 가동 — MinerU 가 `server_url` 로 호출하는 원격 VLM.
   `scripts/parse-svc.env`(런처가 로드, gitignored) 에:
   ```
   MINERU_VLM_SERVER_URL=http://<mineru-vlm-gpu-host>:<port>
   # MINERU_VLM_API_KEY=... (필요 시)
   ```
   ⚠️ 반드시 `scripts/parse-svc.env` 에 둔다(`.gitignore` `scripts/*.env` 로 무시 + 런처
   `scripts/run-parse-svc.sh:44` 가 `set -a; . scripts/parse-svc.env` 로 로드). `parse_service/parse-svc.env`
   는 gitignore 도 로드도 안 되니 쓰지 말 것(비밀 유출·미로드).

## 배포 후 스택검증 (플랜 Task 8)

1. `import mineru` + PP-OCRv5 모델 존재 + `MINERU_VLM_SERVER_URL` 헬스체크.
2. **do_parse 실제 계약 대조**(플랜 Task 4 Step 1): `inspect.signature(do_parse)` 로 인자명
   (`output_dir`/`pdf_bytes_list`/`pdf_file_names`/`backend`/`server_url`/`parse_method`)·content_list.json
   출력경로 패턴·content_list item 필드(`type`/`text`/`text_level`/`table_body`/`img_path`/`page_idx`)를 확인.
   - 어긋나면 `mineru_lane._invoke_mineru` kwargs/glob 과 `_content_list_to_elements`/`_TYPE_TO_CATEGORY`
     (특히 heading 이 `type=='text'`+`text_level` 로 오는 경우) 정정.
3. 실 스캔 PDF 1건 `POST /parse` → 표가 `<table>` 로 비어있지 않게 추출(2026-07-07 빈 표 버그 재발 없음).
4. 혼합 PDF(네이티브 텍스트 + 스캔) 1건 → 게이트가 `'ocr'` 강제 라우팅, 스캔 페이지 텍스트 유실 없음.
