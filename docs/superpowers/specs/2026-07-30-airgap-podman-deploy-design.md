# kb-pipeline 폐쇄망(air-gap) Podman 배포 — 설계 스펙

- 작성일: 2026-07-30
- 대상 저장소: `/Users/xxx/workspace/8.kb-pipeline`
- 상태: 승인됨 (brainstorming → 구현 진행)

## 1. 목표

인터넷이 안 되는 폐쇄망 RHEL 서버에서 kb-pipeline 엔진 스택 전체(8개 서비스)를
**빌드/pull 없이 바로 구동**할 수 있게 한다. 온라인 개발기(Apple Silicon Mac)에서
amd64 이미지를 크로스빌드→저장하여 단일 번들로 만들고, 폐쇄망에서 로드→기동한다.

## 2. 확정 전제 (사용자 결정)

| 항목 | 결정 |
|------|------|
| 모델 서빙(LLM·임베딩·리랭커·VL-OCR) | **사내 온프렘 엔드포인트 이미 존재** → 번들 범위 밖. `.env`로 주소만 재설정 |
| 대상 아키텍처 | **linux/amd64** (개발기 arm64 → 크로스빌드 필수) |
| 서비스 범위 | **전체 8개** (postgres·minio·gotenberg·edgequake·doc_guard·adaptive_chunk·parse-svc·facade + edgequake_webui) |
| 대상 OS/런타임 | **RHEL + Podman (rootful/root)** |
| Phase B 구동 경로 | **podman-compose 전제** (compose 파일 직접 사용) |
| 엔드포인트 설정 | 모두 `.env`로 외부화 + 바꾸는 위치·방법 주석 명시 (사용자 명시 요청) |

## 3. 핵심 관찰 (왜 단순 도커화로 부족한가)

1. **빌드타임 인터넷 의존**: edgequake(Rust crates + pdfium curl 다운로드),
   parse-svc(`npm install -g kordoc`, apt, pip), edgequake_webui(npm), adaptive_chunk/doc_guard(pip).
   → 폐쇄망에서 빌드 불가. 반드시 온라인에서 빌드 완료된 이미지를 옮겨야 함.
2. **아키텍처 불일치**: 개발기 arm64 vs 대상 amd64. 네이티브 빌드 이미지는 대상에서 실행 불가.
   → `docker buildx --platform linux/amd64` 크로스빌드.
3. **런타임 모델 의존**: 스택이 실행 중 OpenRouter/litellm.ax-demo.com 를 호출.
   → 온프렘 엔드포인트로 `.env` 재설정 (모델 서버 자체는 사내에 이미 존재).
4. **Podman 호환성**: podman-compose 는 `depends_on: condition: service_healthy` 를
   기다리지 않을 수 있고, `--wait` 플래그가 없으며, `!override` 커스텀 YAML 태그를 지원하지 않음.
   → podman 전용 compose 파일 + 스크립트 health 폴링.

## 4. 산출물

### 4.1 `docker-compose.airgap.yml` (신규)
기존 `docker-compose.yml`에서 podman-compose 비호환 요소를 제거한 전용본:
- 모든 서비스에서 `build:` 제거 → `image: kbp-<svc>:airgap` 태그만 (오프라인 빌드 시도 차단).
- `docker-compose.override.yml`의 `!override` 커스텀 태그 제거 → 호스트 포트를 본문에 직접 인라인.
  - minio: `9000:9000`, `9001:9001` (override 승격값 채택)
  - edgequake_webui: `13000:3000`
  - 나머지 base 포트 유지 (postgres 5433, edgequake 8081, parse-svc 19001, facade 19000, doc_guard 8001, adaptive_chunk 18060)
- 각 서비스에 `restart: unless-stopped` 추가 → health 미대기로 인한 기동 레이스 자가치유.
- 환경변수·네트워크·볼륨·healthcheck 는 base 와 동일 유지.
- pull 이미지도 명시적 태그로 고정(로드된 로컬 이미지 재사용 보장).

### 4.2 `.env.airgap.example` (신규)
- 파일 **최상단에 "온프렘 재설정 필수" 블록**으로 외부 엔드포인트 키를 모음.
- 각 키에 3줄 주석: `① 무엇을 서빙 ② 여기로 바꾸세요(예시) ③ 안 바꾸면 증상`.
- 대상 키:
  - LLM: `OPENROUTER_API_KEY`, `KBP_OPENAI_API_KEY`, `KBP_OPENAI_BASE_URL`, `KBP_LLM_MODEL`, `ADAPTIVE_CHUNK_OPENROUTER_API_KEY/BASE_URL`, `ADAPTIVE_CHUNK_REGEX_LLM_MODEL/COREF_LLM_MODEL`
  - VL-OCR: `MODEL_API_URL`, `MODEL_API_KEY`
  - 임베딩: `LITELLM_EMBEDDING_BASE_URL/API_KEY`, `ADAPTIVE_CHUNK_SCORING_EMBEDDING_BASE_URL/API_KEY/MODEL`
  - 리랭커: `EDGEQUAKE_RERANK_BASE_URL/MODEL/API_KEY`, `ADAPTIVE_CHUNK_RERANK_BASE_URL/API_KEY/MODEL`
- 내부 전용(바꾸지 말 것) 키는 별도 하단 블록에 분리: `MINIO_*`, `POSTGRES_PASSWORD`, 인트라스택 DNS URL.
- **실제 시크릿은 전부 blank** (템플릿이므로 커밋 안전).

### 4.3 스크립트 3종 (`scripts/airgap/`)

**`build-bundle.sh`** (Phase A, 온라인, 이 Mac / docker buildx)
1. buildx builder 확인/생성.
2. 6개 이미지 `--platform linux/amd64` 빌드 + `--load`, 태그 `kbp-<svc>:airgap`.
   - edgequake: `docker/edgequake.Dockerfile` (context `.`)
   - parse-svc: `Dockerfile.parse-svc` (context `.`)
   - facade: `Dockerfile.facade` (context `.`)
   - adaptive_chunk: context `../99.projects/adaptive_chunk`
   - doc_guard: context `../99.projects/shinhan_trust/doc_guard`
   - edgequake_webui: context `./edgequake`, dockerfile `edgequake_webui/Dockerfile`
3. 3개 base 이미지 amd64 pull: `edgequake-postgres`, `minio/minio`, `gotenberg/gotenberg:8` → `kbp-*:airgap` 로 재태그(선택) 또는 원본 태그 유지.
4. `docker save` 9종 → `dist/images/kbp-images-amd64.tar`, gzip.
5. 배포물 스테이징(`dist/bundle/`): `docker-compose.airgap.yml`, `.env.airgap.example`, `scripts/airgap/load-and-up.sh`, `scripts/airgap/verify-bundle.sh`, `docs/airgap-deploy.md`, 이미지 tar.
6. `tar czf dist/kbp-airgap-bundle.tar.gz -C dist/bundle .`
7. sha256 체크섬 출력.

**`load-and-up.sh`** (Phase B, 오프라인, RHEL / rootful podman)
1. `podman load -i images/kbp-images-amd64.tar.gz` (전량).
2. `.env` 존재/필수키 확인 (`verify-bundle.sh` 재사용). 없으면 `.env.airgap.example` 복사 안내 후 중단.
3. `podman-compose -f docker-compose.airgap.yml up -d`.
4. **health 폴링**: 각 서비스 healthz/health 를 타임아웃까지 폴링(edgequake 8081, facade 19000, parse-svc 19001, adaptive_chunk 18060, doc_guard 8001, minio, webui 13000).
5. MinIO 버킷 생성(멱등): 컨테이너명 자동탐색(`podman ps` name 필터) 후 내부 `mc` 로 버킷 생성.
6. 최종 스모크 요약 출력.

**`verify-bundle.sh`** (무결성)
- 이미지 tar 존재·9종 이미지 매니페스트·arch=amd64 확인.
- `.env` 필수 온프렘 키가 비어있지 않은지 확인.
- 헬퍼로 Phase A(save 후)·Phase B(load 전) 양쪽에서 호출 가능.

### 4.4 `docs/airgap-deploy.md` (신규 런북)
- Phase A 절차(온라인 빌드→번들), 예상 소요(edgequake Rust ~10분+).
- 전송(단일 tar.gz).
- Phase B 절차(load→env→up→검증→버킷→스모크).
- 트러블슈팅: podman rootful, SELinux(named volume 은 relabel 불요), 포트 충돌, health 미대기 레이스, buildx 크로스빌드 실패, 온프렘 엔드포인트 오설정 증상 매핑.

## 5. 비목표 (YAGNI)

- 모델 서버(vLLM/litellm) 번들링 — 온프렘에 이미 존재.
- kb-backend/frontend(신한 웹앱) 번들 — 본 요청은 kb-pipeline 엔진 한정.
- 멀티아키(arm64) 이미지 — 대상 amd64 확정. (매뉴얼에 변경법만 한 줄 안내.)
- `podman play kube` / systemd unit 생성 — podman-compose 전제.

## 6. 검증 기준

- Phase A: `build-bundle.sh` 가 Mac에서 완주하여 `kbp-airgap-bundle.tar.gz` 생성,
  `verify-bundle.sh` 가 이미지 9종·arch=amd64 통과.
- Phase B(폐쇄망 실서버)는 사용자 환경에서 검증(개발기에서 podman 미보유 가능) —
  스크립트는 방어적으로 작성하고 매뉴얼로 절차 보증.

## 7. 리스크 / 완화

| 리스크 | 완화 |
|--------|------|
| edgequake amd64 크로스빌드가 QEMU 에뮬 위 Rust 빌드로 매우 느림 | 매뉴얼에 소요시간 명시, buildx 캐시 사용 |
| podman-compose 가 `depends_on` health 미대기 → 기동 레이스 | `restart: unless-stopped` + 스크립트 health 폴링 |
| podman-compose 버전별 컨테이너 네이밍 차이(`kbp_minio_1` vs `kbp-minio-1`) | 버킷 스크립트가 name 부분일치로 탐색 |
| `.env` 온프렘 미설정 시 조용한 실패 | `verify-bundle.sh` 필수키 검사 + 매뉴얼 증상 매핑 |
| 실제 시크릿이 `.env.airgap.example` 에 유출 | 템플릿 전부 blank, self-review 로 확인 |
