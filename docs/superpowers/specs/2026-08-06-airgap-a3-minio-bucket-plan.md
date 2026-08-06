<!-- plan-version: v4 -->
<!-- ultracode-validation: PENDING -->

# 폐쇄망 A3 — MinIO 버킷 생성 검증 + 익명 읽기 정책

> `docs/airgap-known-issues.md` A3 절의 "리포 패치(미반영)" 4건 중 **kb 쪽 1건은 이제
> 대상이 아니다** — A3 문서 작성(2026-08-04) 이후 kb의 MinIO 접근 방식이 통째로
> 바뀌었다(§0). 이 plan은 살아있는 kbp 쪽 2건 + 문서 갱신을 다룬다.
>
> **A2 와 관계**: 코드 의존 없음. A2 는 kb 리포, A3 는 kbp 리포 + 양쪽 문서.
>
> **v1 → v2**: 검증 blocking 4건 — 그중 하나는 **실제 보안 결함**이었다. 제안한
> `mc anonymous set download`(버킷 전체 익명 읽기)가 페이지 이미지뿐 아니라 **잡 큐
> 원본 업로드 파일 전체**(`kbp-jobs/{job_id}/...`)와 **kb 의 미리보기·배치 원본**
> (`parse-staging/...`)까지 인증 없이 노출시킨다 — 신탁 문서 원본이 job_id 를 아는
> 누구에게나 열린다. **JSON 정책으로 그 두 prefix 를 명시 Deny** 해 페이지 이미지만
> 남기도록 다시 짰다.
>
> **v2 → v3**: 검증 blocking 1건(3개 렌즈 독립 재현) — **v2 의 보안 수정 자체가
> 불완전했다.** Deny 대상 prefix(`kbp-jobs`·`parse-staging`)를 **리터럴로 하드코딩**
> 했는데, 두 값 다 `KBP_JOB_MINIO_PREFIX`·`KBP_STAGING_PREFIX` 로 배포마다 바꿀 수
> 있고 **운영 문서(부록 A, D16)가 공유 MinIO 다중배포 시 실제로 바꾸라고 권장**한다.
> 운영자가 그 권고를 따르면 Deny 가 실제 prefix 를 못 잡아 v1 의 노출이 **그대로
> 재현**된다. `.env` 에서 두 prefix 를 실제로 읽어 ARN 에 치환하도록 고쳤다. 겸사겸사
> 존재하지 않던 헬퍼 함수 호출(`_page_image_anon_policy_json`)을 실제 인라인 코드로
> 바꾸고, `mc mb -p` 가 이미 `--ignore-existing` 라 애초에 `set -e` 로 안 죽는다는
> 점을 정정했다(`\|\| true` 는 유지하되 근거를 "존재하는 버그를 고친다"가 아니라
> "방어적 안전망"으로 바꿈).
>
> **v3 → v4**: 검증 blocking 1건(2개 렌즈, 실제 bash 실행으로 재현) — v3 가 새로
> 추가한 `JOB_PREFIX=$(grep ... )`·`STAGING_PREFIX=$(grep ...)` 가 `set -euo pipefail`
> 아래서 **grep 매치 0건(정상적인 경우 — `.env.airgap.example` 에 이 두 키가 원래
> 없다)이면 파이프가 실패해 스크립트가 그 자리에서 죽는다.** `${JOB_PREFIX:-kbp-jobs}`
> 폴백 줄까지 도달하지도 못한다 — v1 부터 지키려던 "6번 요약까지는 도달시킨다"를 정확히
> 어겼다. 기존 `BUCKET=$(grep ...)` 줄도 **같은 결함을 안고 있었는데**, `.env.airgap.example`
> 에 `MINIO_BUCKET` 키가 우연히 존재해서 지금까지 안 터졌을 뿐이다 — 이 plan 이 그 줄을
> 이미 다시 쓰는 김에 셋 다 `\|\| true` 로 고쳤다. 실제 bash 실행으로 수정 전/후를
> 재현해 확인했다.

---

## 0. 배경 — kb 쪽 원안이 이제 왜 무의미한가

known-issues.md A3 는 원래 kb 쪽에도 패치를 요구했다: *"`MinioStore.from_settings` 또는
첫 put 경로에서 `bucket_exists` → `make_bucket` 멱등 보장"*.

**지금 코드는 이걸 구현할 수조차 없다.** `backend/app/clients/minio_client.py` 의 독스트링이
명시하듯, kb 는 이제 **MinIO 를 직접 부르지 않는다** — 제어평면(원본 승격, staging,
삭제)은 facade `/objects/*` 를 거치고, kb 는 MinIO 자격증명을 **아예 안 갖는다**("kb 에서
MinIO 자격증명이 사라지는 게 요점이다"). `MinioStore` 클래스는 HTTP 로 facade 를 부르는
래퍼일 뿐 minio SDK 를 안 쓴다. **`bucket_exists`/`make_bucket` 을 kb 에 넣을 방법이
없다** — 자격증명이 없다.

즉 A3 의 kb 쪽 항목은 "패치 필요" 가 아니라 **"이미 다른 방식으로 해소됨"** 이다 — 버킷
소유·생성 책임이 애초부터 kbp 하나로 좁혀졌다.

브라우저의 데이터평면(챗 인용·썸네일)은 여전히 `/obj/*`(Next.js same-origin rewrite) 로
MinIO 를 **직접** 때린다(`frontend/next.config.mjs:23`) — 이건 kb 백엔드 코드가 아니라
**MinIO 버킷의 익명 읽기 정책**에 의존한다. 그 정책은 지금 **아무 곳에도 설정돼 있지
않다**(§1 사실 4).

---

## 1. 실측 사실 (2026-08-06, 라인번호 재검증)

| # | 사실 | 근거 |
|---|---|---|
| 1 | `load-and-up.sh` 의 버킷 생성이 **실패를 삼킨다** — `mc mb ... 2>/dev/null; mc ls local/` 는 `mc ls local/`(전체 버킷 목록, 특정 버킷 아님)의 exit code 로만 성공을 판정한다 | `scripts/airgap/load-and-up.sh:161-174` |
| 2 | 버킷명 파싱은 `.env` 의 `MINIO_BUCKET=` 을 grep — 기본값 `document-parser` | `load-and-up.sh:163-164` |
| 3 | 컨테이너 탐색(`ctr minio`)은 **compose 라벨 기반**으로 이미 고쳐져 있다(D16) — 남의 스택 버킷을 잘못 잡는 문제는 **이미 해소됨** | `load-and-up.sh:80-98` |
| 4 | **익명 읽기 정책 설정이 스크립트·compose 어디에도 없다** — `mc anonymous`/`policy` 문자열이 리포에 0건 | grep 확인 |
| 5 | `frontend/next.config.mjs:23` 의 `/obj/:path*` rewrite 가 **인증 없이** MinIO 를 직접 GET 한다(혼합콘텐츠 회피용 same-origin 프록시). **경로 제한이 없다** — 버킷 아래 어떤 키든 그대로 통과시킨다 | 해당 파일 |
| 6 | kb `backend/app/clients/minio_client.py` 는 **HTTP 로 facade `/objects/*` 를 부르는 래퍼**일 뿐, minio SDK·자격증명이 없다 — "kb 에서 MinIO 자격증명이 사라지는 게 요점" | 해당 파일 독스트링 |
| 7 | facade `service/jobs/blobs.py` 는 **`make_bucket` 을 의도적으로 호출 안 함** — 제한된 업로드 전용 자격증명이 `AccessDenied` 를 낼 수 있어서다. 기동 시 `check_bucket()` 으로 **존재만 확인** | `service/jobs/blobs.py:8`, `:90-105` |
| 8 | `check_bucket()` 실패는 **WARN 만** 남기고 기동을 막지 않는다 — **의도된 설계**: 버킷 생성(§1 사실 1, 스크립트 5번 단계)이 facade 기동(3번)·헬스체크(4번)보다 **나중**이라, 첫 배포에서 버킷이 아직 없는 게 정상 상태다 | `blobs.py:96-99`; `load-and-up.sh` 단계 순서(`3→4→4b→5`) |
| 9 | 위 순서 제약 때문에, 버킷 생성을 facade 기동보다 앞으로 옮기는 것은 이 plan 의 범위 밖이다 | 설계 판단 |
| 10 | **`document-parser` 버킷은 최소 4가지 키 형태를 공유한다**: (a) 페이지 이미지 `{doc_id}/{page_uuid}.jpeg`, (b) 원본 승격분 `{doc_id}/original/{name}`, (c) kbp 잡 큐 staging/오프로드 `kbp-jobs/{job_id}/...`(**원본 업로드 바이트 + `enriched_content` 포함 파싱 결과 전체**), (d) kb 미리보기·배치 원본 `parse-staging/{name}` | `service/objects.py:35-41`(a·b); `service/jobs/blobs.py`(c, `DEFAULT_PREFIX="kbp-jobs"`, 사실 7 독스트링); `docs/airgap-deploy.md:174-180`(d) |
| 11 | (c)·(d) 는 **공개 의도가 전혀 없다** — job_id(UUID)는 잡 상태 API 응답·로그에 노출되므로, 그 값을 아는 누구나 유추 가능한 키다 | 위 근거들의 결합 |
| 12 | `mc anonymous set <none|download|upload|public>` 은 **버킷 전체에 적용되는 캔드(canned) 정책**이다 — prefix 단위 스코프가 없다. prefix 를 가르려면 **JSON 정책**(`mc anonymous set-json`)이 필요하다 | mc 표준 동작 |
| 13 | **`docs/airgap-deploy.md`(kbp) 트러블슈팅에 `NoSuchBucket` 행이 이미 있다** — *"MinIO 버킷 미생성(`NoSuchBucket`) │ 최초 1회 생성 필요 │ load-and-up.sh 가 자동 생성. 실패 시 §부록 수동 생성"*. **§부록 B "MinIO 버킷 수동 생성"도 이미 있다** — 단, 그 부록이 `grep -i minio` 로 컨테이너를 찾는 **D16 이전의 낡은 방식**을 그대로 쓴다(남의 스택을 집을 수 있음, 사실 3 이 본문에서 이미 고친 문제) | `docs/airgap-deploy.md:143`, `:183-186` |
| 14 | `docs/airgap-deploy.md`(kb)에는 "버킷은 kbp 소유" 명시가 없다 | grep 확인 |
| 15 | 스크립트 끝 6번 단계(`:176-189`)는 `FAIL` 플래그를 매 단계에서만 세우고 **`compose ps` 요약을 항상 출력한 뒤** 마지막에 `FAIL` 을 보고 `die` 한다 — 중간 단계에서 즉시 `die` 하면 이 요약이 안 나온다 | `load-and-up.sh:107-158`(FAIL 세팅 패턴), `:176-189`(요약 후 최종 판정) |
| 16 | 스크립트 전체가 `set -euo pipefail`(`:15`) — `&&`/`\|\|` 체인의 일부가 아닌 단독(bare) 명령이 실패하면 즉시 스크립트가 죽는다. **다만 `mc mb` 는 이미 `-p`(`--ignore-existing`) 플래그를 쓰고 있어(`:170`), 버킷이 이미 있는 멱등 케이스에서는 애초에 실패하지 않고 exit 0 을 낸다** — `\|\| true` 는 "존재하는 크래시 버그를 고친다"가 아니라 **그 외의(권한·용량 등) 진짜 오류에 대한 방어적 안전망**일 뿐이다(v2 는 이 인과관계를 잘못 서술했다) | `load-and-up.sh:15`, `:170`; mc 문서(`--ignore-existing`: "버킷이 이미 있으면 오류 없이 아무 것도 안 함") |
| 17 | **Deny 대상 prefix 는 하드코딩 대상이 아니다** — `KBP_JOB_MINIO_PREFIX`(기본 `kbp-jobs`)·`KBP_STAGING_PREFIX`(기본 `parse-staging`)로 배포마다 다르게 설정 가능하고, **부록 A(D16)가 공유 MinIO 다중배포 시 실제로 다르게 잡으라고 권장**한다 — 그 권고를 따른 배포에서 리터럴 Deny 는 실제 prefix 를 못 잡는다 | `docker-compose.airgap.yml:69-70`; `service/jobs/staging_gc.py`(`staging_prefix()` 가 env 를 읽음); `service/jobs/blobs.py`(`JobBlobStore.from_env` 가 `KBP_JOB_MINIO_PREFIX` 를 읽음); `docs/airgap-deploy.md` 부록 A |

---

## 2. 설계

### 2.1 `load-and-up.sh` — 버킷 생성을 실제로 검증한다 (set -e 안전)

```bash
# ── 5) MinIO 버킷 생성 (멱등, 존재 검증 + prefix 제한 익명 읽기 정책) ────────
log "MinIO 버킷 생성"
# ★ grep 매치 0건(exit 1)이면 set -e 아래서 파이프 전체가 실패해 스크립트가 죽는다.
#   `.env.airgap.example` 에 KBP_JOB_MINIO_PREFIX/KBP_STAGING_PREFIX 키가 원래 없으므로
#   (표준 배포에서 매치 0건이 정상 경로다) 세 줄 모두 `|| true` 로 감싼다. 기존
#   BUCKET 줄도 같은 결함을 안고 있었다 — MINIO_BUCKET 키가 우연히 템플릿에 있어서
#   지금까지 안 터졌을 뿐이라 여기서 함께 고친다(v3→v4, 실제 bash 재현으로 확인).
BUCKET="$(grep -E '^MINIO_BUCKET=' "$BUNDLE_ROOT/.env" | cut -d= -f2 | tr -d '[:space:]')" || true
BUCKET="${BUCKET:-document-parser}"
# ★ 두 prefix 도 BUCKET 과 같은 방식으로 .env 에서 읽는다 — 리터럴 금지(사실 17).
#   기본값은 코드 기본값(staging_gc.py DEFAULT_PREFIX, blobs.py DEFAULT_PREFIX)과 일치.
JOB_PREFIX="$(grep -E '^KBP_JOB_MINIO_PREFIX=' "$BUNDLE_ROOT/.env" | cut -d= -f2 | tr -d '[:space:]')" || true
JOB_PREFIX="${JOB_PREFIX:-kbp-jobs}"
STAGING_PREFIX="$(grep -E '^KBP_STAGING_PREFIX=' "$BUNDLE_ROOT/.env" | cut -d= -f2 | tr -d '[:space:]')" || true
STAGING_PREFIX="${STAGING_PREFIX:-parse-staging}"
MC_CTR="$(ctr minio)"
if [ -z "$MC_CTR" ]; then
  warn "minio 컨테이너를 찾지 못함 — 버킷 생성 건너뜀"
  FAIL=1                                    # ★ die 아님 — 6번 요약까지 도달시킨다(§2.1 근거)
else
  podman exec "$MC_CTR" sh -c \
    'mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1' \
    || { warn "mc alias 설정 실패 — MinIO 컨테이너 상태 확인"; FAIL=1; }

  # mc mb -p 는 이미 --ignore-existing 이라 존재하는 버킷에서 실패하지 않는다(사실 16).
  # || true 는 그 외의(권한·용량 등) 진짜 오류에 대한 방어적 안전망일 뿐이다.
  podman exec "$MC_CTR" mc mb -p "local/$BUCKET" >/dev/null 2>&1 || true

  # mc mb 의 성패가 아니라 mc stat 의 실제 존재 여부로 판정한다(사실 1 을 직접 고침)
  if podman exec "$MC_CTR" mc stat "local/$BUCKET" >/dev/null 2>&1; then
    echo "  ✓ 버킷 '$BUCKET' 존재 확인"

    # ★ 익명 읽기 — 페이지 이미지만. JOB_PREFIX·STAGING_PREFIX·*/original/* 는 명시
    #   Deny(§2.1.1). 정책 문자열은 여기서 완성해 heredoc 으로 컨테이너에 넣는다 —
    #   별도 헬퍼 함수가 아니라 이 블록 안에 인라인한다(v2 는 미정의 함수를 불렀다).
    ANON_POLICY=$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Principal": {"AWS": ["*"]}, "Action": ["s3:GetObject"],
     "Resource": ["arn:aws:s3:::${BUCKET}/*"]},
    {"Effect": "Deny", "Principal": {"AWS": ["*"]}, "Action": ["s3:GetObject"],
     "Resource": [
       "arn:aws:s3:::${BUCKET}/${JOB_PREFIX}/*",
       "arn:aws:s3:::${BUCKET}/${STAGING_PREFIX}/*",
       "arn:aws:s3:::${BUCKET}/*/original/*"
     ]}
  ]
}
POLICY
)
    if podman exec -i "$MC_CTR" sh -c \
        "cat > /tmp/anon-policy.json && mc anonymous set-json /tmp/anon-policy.json local/$BUCKET" \
        <<< "$ANON_POLICY" >/dev/null 2>&1; then
      echo "  ✓ 익명 읽기 정책 설정됨(페이지 이미지 전용, deny: $JOB_PREFIX/*, $STAGING_PREFIX/*)"
    else
      warn "익명 읽기 정책 설정 실패 — 챗 인용/썸네일 이미지가 403 날 수 있음"
    fi
  else
    warn "버킷 '$BUCKET' 생성 실패 — mc stat 로 존재 확인 안 됨. MinIO 자격증명·용량 확인"
    FAIL=1                                  # ★ die 아님 — 아래 근거
  fi
fi
```

- **prefix 를 리터럴로 안 쓴다** — `BUCKET` 과 같은 방식으로 `.env` 에서
  `KBP_JOB_MINIO_PREFIX`·`KBP_STAGING_PREFIX` 를 읽는다(사실 17). 운영자가 부록 A(D16)
  권고대로 공유 MinIO 다중배포에서 prefix 를 바꿔도 Deny 가 그 실제 값을 따라간다.
- **`die` 를 안 쓴다 — `FAIL=1` 로 표시하고 6번 요약까지 흘려보낸다.** v1 은 여기서 즉시
  `die` 했는데, 스크립트 끝(사실 15)이 이미 "단계마다 `FAIL` 세팅 → 마지막에 요약 후
  판정"이라는 일관된 패턴을 쓰고 있다 — 이 패턴을 그대로 따라야 `compose ps` 진단
  출력이 실패해도 항상 나온다. 최종적으로 스크립트는 여전히 non-zero exit 로 끝난다
  (기존 6번 블록의 `die` 가 처리) — 사용자 입장에선 "실패했다"는 신호가 똑같이 오지만,
  **왜** 실패했는지 볼 수 있는 정보가 v1 보다 많아진다.
- **`mc mb` 뒤의 `|| true` 는 존재하는 크래시를 고치는 게 아니라 방어적 안전망이다**
  (사실 16 — `-p` 가 이미 idempotent 를 보장한다). v2 가 "이게 없으면 set -e 로
  죽는다"고 쓴 건 틀렸다 — 정정한다.
- **`mc alias`·최종 판정 실패도 `FAIL=1` 로만 표시**하고 계속 진행한다 — 같은 이유.
- **`podman exec -i` + heredoc 으로 JSON 을 직접 흘려보낸다** — v2 는 컨테이너 안에서
  `cat > file <<'JSON' ... JSON` 를 문자열 보간으로 구성했는데, 그 안에 `$(...)` 미정의
  호출을 넣는 실수가 있었다. 여기서는 셸이 정책 문자열을 완성한 뒤 `-i` 로 stdin 을
  통해 그대로 넘긴다 — 변수 치환·따옴표 이슈가 한 곳(호스트 셸)에서만 일어난다.

#### 2.1.1 익명 읽기 정책 — 페이지 이미지 prefix 만, JSON 정책으로 명시 Deny

**v1 의 결함(실제 보안 문제, 검증에서 확인)**: `mc anonymous set download local/$BUCKET`
는 **버킷 전체**에 적용되는 캔드 정책이다(사실 12). 이 버킷에는 페이지 이미지 말고도
**잡 큐 원본 업로드 파일**과 **kb 미리보기·배치 원본**이 같이 있다(사실 10 c·d). 버킷
전체를 익명 읽기로 열면, job_id 를 아는(잡 상태 API·로그에 노출됨, 사실 11) 누구나 신탁
문서 원본을 인증 없이 그대로 내려받을 수 있다 — **이건 기존에 감수하던 노출(페이지
이미지)이 아니라 새로 만드는 노출**이다.

**v2 의 결함(검증에서 3개 렌즈 독립 재현)**: JSON 정책의 Deny 대상을 `kbp-jobs`·
`parse-staging` **리터럴**로 하드코딩했다. 두 값은 실제로 `KBP_JOB_MINIO_PREFIX`·
`KBP_STAGING_PREFIX` 로 배포마다 다르게 설정된다(사실 17) — `docs/airgap-deploy.md`
부록 A 가 "같은 MinIO 버킷을 여러 배포가 공유하면 배포별로 다르게 잡아야 한다"고
**명시적으로 권장**하는 바로 그 값들이다. 운영자가 그 권고를 따른 배포에서는 리터럴
Deny 가 실제 prefix 를 못 잡아 **v1 의 보안 노출이 그대로 재현**된다 — v2 가 "보안결함
해소"를 핵심으로 내세운 채 정작 다중배포(정확히 이 정책이 필요해지는 배포 형태)에서
무너지는 셈이었다.

**고침 — §2.1 스크립트가 `.env` 에서 두 prefix 를 읽어 그 값으로 ARN 을 구성한다**
(위 §2.1 코드 참고). 정책은 이제 다음과 같다(예: 기본값 그대로일 때):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {"AWS": ["*"]},
      "Action": ["s3:GetObject"],
      "Resource": ["arn:aws:s3:::document-parser/*"]
    },
    {
      "Effect": "Deny",
      "Principal": {"AWS": ["*"]},
      "Action": ["s3:GetObject"],
      "Resource": [
        "arn:aws:s3:::document-parser/kbp-jobs/*",
        "arn:aws:s3:::document-parser/parse-staging/*",
        "arn:aws:s3:::document-parser/*/original/*"
      ]
    }
  ]
}
```

- 첫 Deny 항목이 **잡 큐 staging/오프로드**(사실 10 c, `$JOB_PREFIX`)를, 둘째가 **kb
  미리보기·배치 원본**(사실 10 d, `$STAGING_PREFIX`)을 막는다 — 둘 다 `.env` 값을
  따라간다(리터럴 아님, 위 수정).
- 셋째(`*/original/*`)가 **원본 승격분**(사실 10 b, `{doc_id}/original/{name}`)을 막는다.
  이 경로는 env 로 바뀌지 않는 고정 레이아웃(`service/objects.py:36`)이라 리터럴이어도
  안전하다. `*` 는 AWS/MinIO 정책 매칭에서 `/` 를 못 건너뛰는 개념이 **아니다**(임의
  문자열에 매칭) — 그래서 `*/original/*` 는 "`original` 이라는 세그먼트를 어딘가에
  포함하는 모든 경로"를 넉넉하게 잡는다. **과잉 차단은 안전한 방향**이라 문제 삼지
  않는다(원본은 애초에 공개할 이유가 없다).
- **남는 것 = 페이지 이미지**(`{doc_id}/{page_uuid}.jpeg`, 사실 10 a) — 위 세 Deny 패턴
  중 어느 것에도 안 걸리는 유일한 키 형태다.

### 2.2 kb 쪽 — 변경 없음, 문서만 정정

§0 이 확인했듯 kb 는 MinIO 자격증명이 없어 `bucket_exists`/`make_bucket` 을 구현할 수
없다. **원안의 kb 패치는 적용하지 않는다** — `docs/airgap-known-issues.md` A3 항목에
"kb 쪽: 아키텍처 변경(facade 경유)으로 이제 해당 없음" 각주를 남긴다(§2.4).

### 2.3 문서 — 기존 항목을 갱신한다(신규 추가 아님)

**v1 의 결함**: "`docs/airgap-deploy.md`(kbp)에 `NoSuchBucket` 항목이 없다"고 실측했는데
**틀렸다**(사실 13) — 이미 트러블슈팅 표 1행 + 부록 B 가 있다. "행 추가"가 아니라
**기존 내용을 새 동작(FAIL 표시 + JSON 정책)에 맞게 갱신**해야 한다.

**`8.kb-pipeline/docs/airgap-deploy.md`**:
- 트러블슈팅 `:143` 행을 갱신: *"…실패 시 `FAIL` 로 표시되고 6번 요약에 원인이 남는다.
  §부록 B 로 수동 생성(재실행은 스크립트를 다시 돌리는 게 우선)."*
- **부록 B(`:183-186`)를 D16 방식으로 갱신** — `grep -i minio` 대신 스크립트 본문과 같은
  compose 라벨 탐색을 쓰도록 예시를 바꾸고, **JSON 익명 정책 예시**(§2.1.1)를 추가한다.
  같은 문서 안에서 본문과 부록이 서로 다른(하나는 안전, 하나는 낡은) 컨테이너 탐색법을
  쓰는 상태를 정리한다.
- 챗 원문 이미지(`/obj/*`)가 인증 없이 MinIO 를 직접 GET 하므로 **페이지 이미지 prefix
  에만** 익명 `s3:GetObject` 정책이 필요하다는 것을 명시하고, 그 prefix 안에서는 여전히
  "버킷 이름·오브젝트 키를 아는 누구나 읽을 수 있다"는 리스크(폐쇄망 내부망 전제 위에서
  감수)를 함께 적는다.

**`knowledge_base/docs/airgap-deploy.md`**:
- "버킷은 kbp 스택 소유, kb 는 자격증명이 없다"를 명시(§0 의 근거를 옮긴다).
- `NoSuchBucket` 이 보이면 **kbp 쪽** `load-and-up.sh` 5단계·트러블슈팅 항목을 보라고
  안내(사실 14 — 지금은 kbp 문서에만 있다).

### 2.4 `docs/airgap-known-issues.md` 갱신

A3 절의 "리포 패치(미반영)" **4건 모두**의 상태를 명시적으로 갱신한다(v2 는 3건만
언급해 나머지 1건의 처리 상태가 빠질 위험이 있었다):

1. kbp `load-and-up.sh` 버킷 생성(존재 검증) — **완료**
2. kb `MinioStore` bucket_exists/make_bucket — **해당 없음(아키텍처 변경, §0)**
3. kbp `airgap-deploy.md` 트러블슈팅 행 + "버킷은 kbp 소유" 명시 — **완료**(§2.3, 신규
   추가가 아니라 기존 행·부록 **갱신**임을 함께 적는다)
4. 익명 읽기 정책 — **완료**(prefix 제한 JSON 정책, §2.1.1)

---

## 3. 변경 목록

**kbp**
- `scripts/airgap/load-and-up.sh` — §2.1 (버킷 존재 검증, `FAIL=1` 패턴 준수, `.env` 에서
  `MINIO_BUCKET`·`KBP_JOB_MINIO_PREFIX`·`KBP_STAGING_PREFIX` 를 함께 읽어 JSON 정책의
  `$BUCKET`·`$JOB_PREFIX`·`$STAGING_PREFIX` 로 치환, `podman exec -i` + heredoc)
- `docs/airgap-deploy.md` — §2.3 (트러블슈팅 행 **갱신**, 부록 B **갱신**: 컨테이너
  탐색을 D16 방식으로, JSON 정책 예시 추가 — **prefix 가 env 로 바뀔 수 있음을 예시에
  명시**)

**kb**
- `docs/airgap-deploy.md` — §2.3 (코드 변경 없음)

**공통**
- `docs/airgap-known-issues.md`(kb 리포 소유, kbp 관련 서술도 포함) — §2.4

---

## 4. 테스트

**`load-and-up.sh` (수동 — 실 MinIO 컨테이너 필요, CI 범위 밖)**
- 정상 케이스: 버킷이 없는 신규 배포에서 스크립트를 돌리면 생성 + `mc stat` 통과 +
  "✓ 존재 확인" + "✓ 익명 읽기 정책 설정됨" 출력
- **재실행(멱등)**: 이미 버킷이 있는 상태에서 다시 돌려도 **스크립트가 죽지 않고**
  `mc mb` 실패(`|| true`)를 넘겨 `mc stat` 통과로 정상 진행 ← §2.1 회귀 핵심(v1 이전
  스타일의 bare `mc mb` 였다면 `set -e` 로 여기서 죽었을 시나리오)
- **`mc alias` 실패(자격증명 틀림)**: 스크립트가 **죽지 않고** `FAIL=1` 로 표시된 채
  6번 요약(`compose ps`)까지 진행한 뒤 최종적으로 non-zero exit ← §2.1 회귀 핵심(v1 은
  이 케이스에서 즉시 `die` 해 요약이 안 나왔다)
- **minio 컨테이너 자체를 못 찾는 경우**: 마찬가지로 `FAIL=1` + 6번 요약까지 도달
- **익명 정책 범위 검증(회귀 핵심)**: 정책 설정 후, 인증 없는 `curl` 로
  (a) 페이지 이미지 키(`{doc_id}/{page_uuid}.jpeg`) GET → 200,
  (b) `kbp-jobs/{job_id}/...` 키 GET → 403(또는 AccessDenied),
  (c) `parse-staging/{name}` 키 GET → 403,
  (d) `{doc_id}/original/{name}` 키 GET → 403.
  (b)~(d) 가 이 plan 이 막으려는 정확한 시나리오다 — v1 의 캔드 정책이면 전부 200 이었을
  것이다.
- **커스텀 prefix 검증(v2→v3 회귀 핵심)**: `.env` 에 `KBP_JOB_MINIO_PREFIX=kbp-jobs-prod`
  ·`KBP_STAGING_PREFIX=parse-staging-prod` 를 설정한 뒤 스크립트를 돌리면, 정책의 Deny
  Resource 가 **그 커스텀 값**을 담는다(`mc anonymous get-json local/$BUCKET` 로 확인).
  기본값 리터럴(`kbp-jobs`·`parse-staging`)이 아니라 실제 설정값이 들어가는지가 핵심 —
  v2 는 이 케이스에서 여전히 기본 리터럴만 Deny 했을 것이다(즉 커스텀 prefix 아래 원본이
  익명으로 열렸을 것이다)
- **표준 배포(v3→v4 회귀 핵심, 가장 흔한 경로)**: `.env` 를 `.env.airgap.example` 그대로
  복사해 쓴 상태(즉 `KBP_JOB_MINIO_PREFIX`·`KBP_STAGING_PREFIX` 키가 **없음** — 실제
  템플릿 상태)로 스크립트를 돌리면 **5번 단계에서 죽지 않고** `JOB_PREFIX=kbp-jobs`·
  `STAGING_PREFIX=parse-staging` 기본값으로 정상 진행한다. v3 는 실제 bash 로 재현하면
  이 케이스에서 **아무 메시지 없이 조용히 죽었다** — 이 테스트가 그 회귀를 직접 잡는다

**문서 (리뷰만, 자동 검증 없음)**
- 세 문서(kbp `airgap-deploy.md`, kb `airgap-deploy.md`, `airgap-known-issues.md`)가
  버킷 소유·정책 요구사항에 대해 서로 어긋나지 않는지 교차 확인
- kbp `airgap-deploy.md` 부록 B 의 컨테이너 탐색 예시가 본문(compose 라벨 기반)과
  일치하는지 확인 ← §2.3 회귀 핵심

---

## 5. 리스크

| 리스크 | 완화 |
|---|---|
| **버킷 전체 익명 읽기가 잡 원본·kb staging 원본을 노출한다** | JSON 정책으로 `$JOB_PREFIX/*`·`$STAGING_PREFIX/*`·`*/original/*` 를 명시 Deny(§2.1.1) — 남는 것은 페이지 이미지뿐 |
| **Deny prefix 를 리터럴로 하드코딩하면 커스텀 prefix 배포에서 무력화된다** | `.env` 에서 `KBP_JOB_MINIO_PREFIX`·`KBP_STAGING_PREFIX` 를 실제로 읽어 ARN 에 치환(§2.1) — v2 의 blocking 결함, v3 에서 고침 |
| **표준 배포(키 없음)에서 `set -e` + grep exit 1 로 스크립트가 조용히 죽는다** | `BUCKET`·`JOB_PREFIX`·`STAGING_PREFIX` 세 grep 모두 `\|\| true` 로 감쌈(§2.1) — v3 의 blocking 결함, 실제 bash 재현으로 확인 후 v4 에서 고침. 기존 `BUCKET` 줄도 같은 결함을 안고 있었다(우연히 안 터졌을 뿐) |
| `mc mb` 가 `set -e` 아래서 멱등 재실행을 죽인다 | 실제로는 `-p` 가 이미 방지한다(사실 16) — `\|\| true` 는 방어적 안전망일 뿐, "고치는" 게 아니다(v2 의 서술 정정) |
| 실패를 `die` 대신 `FAIL=1` 로 바꾸면 실패가 덜 눈에 띄는가 | 아니다 — 스크립트는 여전히 non-zero exit 로 끝난다(6번 블록의 기존 `die`). 오히려 **6번 요약(`compose ps`)이 항상 나와** 진단 정보가 늘어난다(§2.1) |
| 페이지 이미지 익명 정책 자체는 여전히 "버킷 이름·키를 아는 누구나 읽음"이다 | 폐쇄망 내부망 전제 위의 기존 데이터평면 설계(같은 위험은 이미 있었다) — 문서에 명시(§2.3) |
| `mc anonymous set-json` 이 일부 minio/mc 버전에서 문법이 다를 수 있다 | 착수 시 이미지의 `mc --version` 확인. JSON 정책 자체는 S3 표준이라 minio server 버전 영향은 적다 |
| `Resource` 패턴의 `*` 가 `/` 를 안 건너뛴다는 전제가 틀리면 과소 차단될 수 있다 | §4 테스트에서 (b)~(d) 를 **실제로 GET 해서 403 확인** — 정책 문법을 신뢰하지 않고 행동으로 검증 |
| kbp `airgap-deploy.md` 트러블슈팅·부록이 이미 있는 걸 "추가"로 오인해 중복 행이 생긴다 | §2.3 에서 "갱신"으로 명시 — 착수 시 `:143`·`:183-186` 을 직접 열어 기존 내용을 확인하고 그 위에 편집한다 |
| kb 쪽 문서만 고치고 실제 kb 코드에 남아있는 옛 MinIO 관련 잔재가 있는가 | §0 확인 결과 `minio_client.py` 는 이미 HTTP-only 래퍼 — 잔재 없음. 착수 시 `grep -rn "minio.Minio\|access_key" backend/` 로 재확인 |
