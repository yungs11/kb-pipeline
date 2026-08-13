#!/usr/bin/env bash
# `.env` → `scripts/parse-svc.env` 동기화 (2026-08-10)
#
# ── 왜 필요한가 ──────────────────────────────────────────────────────────────
# parse-svc 는 두 방식으로 뜬다.
#   (a) 호스트 dev  — `run-parse-svc.sh` 가 `.env` + `parse-svc.env` 를 겹쳐 읽는다
#   (b) 컨테이너    — compose 가 `.env` 만 읽어 주입한다
# 그래서 값을 `.env` 에만 적으면 (b)는 되고 (a)의 **레거시 파일이 낡아간다**. 반대로
# `parse-svc.env` 에만 적으면 (a)는 되고 (b)가 못 본다. 실제로 그 어긋남 때문에
# OCR 게이트웨이 주소 검증이 옛 주소로 갔다(2026-08-10).
#
# 이 스크립트는 **`.env` 를 단일 출처로 두고** parse-svc 가 쓰는 키만 뽑아
# `scripts/parse-svc.env` 를 다시 쓴다. 그러면 두 방식이 항상 같은 값을 본다.
#
# ── 쓰는 법 ──────────────────────────────────────────────────────────────────
#   bash scripts/sync-parse-svc-env.sh            # .env → parse-svc.env 갱신
#   bash scripts/sync-parse-svc-env.sh --check    # 어긋난 키만 보고(파일 안 건드림)
#   bash scripts/sync-parse-svc-env.sh --verify   # 갱신 + parse-svc 재기동 + 주입 실측
#   bash scripts/sync-parse-svc-env.sh --promote  # parse-svc.env 전용 키를 `.env` 로 올림(최초 1회)
#
# ⚠️ `parse-svc.env` 에만 있고 `.env` 에 없는 키는 **지우지 않는다**(보존). 그런 키가 있으면
#    "`.env` 로 올려라" 고 안내한다 — 조용히 날리면 파싱 옵션이 사라진다.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/.env"
DST="$ROOT/scripts/parse-svc.env"
MODE="${1:-}"

[ -f "$SRC" ] || { echo "✗ $SRC 가 없다 — .env.example 을 복사해 채우세요" >&2; exit 1; }

# parse-svc 가 실제로 읽는 키. 코드/Dockerfile 근거를 주석에 남긴다.
KEYS=(
  # 모달 요약 LLM (service/llm.py — 기본값 없어 없으면 KeyError)
  KBP_OPENAI_API_KEY KBP_OPENAI_BASE_URL KBP_LLM_MODEL
  # VL(in-process OCR) — vl 레인. MODEL_NAME 은 성능에 크게 영향(122b vs 235b)
  MODEL_API_URL MODEL_API_KEY MODEL_NAME VL_MAX_TOKENS
  # 스캔 레인 — paddle_gw 면 URL 이 반드시 있어야 한다
  KBP_GATE_OCR_LANE KBP_PADDLE_OCR_GATEWAY_URL KBP_PADDLE_GW_LANG
  # 게이트/트리아지 임계 (parse_service/parsers/pdf/gate.py, triage)
  # ⚠️ 아래 3개는 2026-08-12 페이지수준 라우팅 도입으로 **소비자가 사라졌다**(무효).
  #    파생만 유지 — 삭제는 Phase 4.
  KBP_GATE_DEFAULT_LANE KBP_GATE_VL_LANE KBP_GATE_VL_RATIO
  KBP_TRIAGE_CONTENT_MIN KBP_TRIAGE_NATIVE_TEXT_MIN_CHARS KBP_TRIAGE_LOG_TABLE
  KBP_TRIAGE_LANDSCAPE_TO_LLM KBP_TRIAGE_MIXED_IMAGE_COV
  KBP_TRIAGE_DIAGRAM_LINE_MIN KBP_TRIAGE_DIAGRAM_CURVE_MIN
  KBP_TRIAGE_DIAGRAM_COMBO_CURVE_MIN KBP_TRIAGE_DIAGRAM_IMG_COUNT
  # paddle_gw 페이지 게이트 + 퇴화 필터 (page_verdict.py, degen_filter.py)
  KBP_GW_GATE KBP_GW_MIN_CHARS KBP_GW_BLANK_INK_MAX
  KBP_GW_CJK_MIN KBP_GW_CJK_RATIO KBP_GW_CJK_DOC_RATIO KBP_GW_CJK_DOC_MIN_PAGES
  KBP_GW_DEGEN_SURVIVE_RATIO KBP_GW_DEGEN_MIN_CHARS
  KBP_DEGEN_COMPRESS_MAX KBP_DEGEN_SOFT_RULES
  # VL 프로바이더 차단(vl_api.py) — 미설정이면 DeepInfra 차단
  KBP_VL_BLOCK_PROVIDERS
  # 페이지수준 라우팅 · VL 전사(2026-08-12). **여기 없으면 파생 parse-svc.env 에서 조용히
  # 누락돼 폐쇄망에서만 코드 기본값으로 돈다** — 로컬 dev 는 루트 .env 를 직접 읽어 무증상.
  KBP_PADDLE_GW_DPI KBP_VL_PAGE_DPI KBP_VL_PAGE_MAX_TOKENS
  KBP_VL_VISUAL_MIN_AREA KBP_VL_MAX_CONCURRENT KBP_VL_DISABLE_REASONING
  # 프롬프트 오버라이드(비면 코드 기본값)
  KBP_PROMPT_HIERARCHY_RULE KBP_PAGE_HYBRID_DIAGRAM_RULE
  # 엑셀 — kordoc 백엔드. Dockerfile.parse-svc:10 이 컨테이너에 ENV 로 박는 값들
  EXCEL_PARSER_BACKEND KORDOC_BIN KORDOC_MD_OUT
  # 오브젝트 스토어(staging 업로드)
  MINIO_ENDPOINT MINIO_BUCKET MINIO_SECURE MINIO_ACCESS_KEY MINIO_SECRET_KEY
  # office→PDF 원격 변환
  KBP_FILECONVERT_URL KBP_FILECONVERT_TOKEN
)

val () { awk -F= -v K="$1" '$1==K {sub(/^[^=]*=/,""); print; exit}' "$2" 2>/dev/null; }

# ── --check : 어긋난 키만 보고 ────────────────────────────────────────────────
if [ "$MODE" = "--check" ]; then
  rc=0
  for k in "${KEYS[@]}"; do
    a="$(val "$k" "$SRC")"; b="$(val "$k" "$DST")"
    [ -z "$a" ] && continue                      # .env 에 없으면 비교 대상 아님
    if [ "$a" != "$b" ]; then
      echo "  ≠ $k  (.env 와 parse-svc.env 가 다르다)"; rc=1
    fi
  done
  # parse-svc.env 에만 있는 키 — 보존 대상이지만 알려준다
  while IFS= read -r k; do
    [ -z "$(val "$k" "$SRC")" ] && echo "  + $k  (parse-svc.env 에만 있다 — .env 로 올리는 것을 권함)"
  done < <(grep -oE '^[A-Z_][A-Z0-9_]*' "$DST" 2>/dev/null | sort -u)
  [ "$rc" = "0" ] && echo "  ✅ 동기화됨"
  exit $rc
fi

# ── --promote : parse-svc.env 에만 있는 키를 `.env` 로 올린다(단일 출처 완성) ──
# `.env` 가 진짜 단일 출처가 되려면 한 번은 올려야 한다. 이후로는 `.env` 만 고치면 된다.
if [ "$MODE" = "--promote" ]; then
  [ -f "$DST" ] || { echo "✗ $DST 가 없다"; exit 1; }
  cp "$SRC" "$SRC.bak.$(date +%H%M%S)"
  added=0
  {
    echo
    echo "# ── parse-svc 키 (sync-parse-svc-env.sh --promote 로 올림, $(date +%Y-%m-%d)) ──"
    echo "# 여기가 단일 출처다. 고친 뒤 \`bash scripts/sync-parse-svc-env.sh\` 로 반영한다."
  } >> "$SRC"
  while IFS= read -r line; do
    case "$line" in ''|'#'*) continue;; *=*) ;; *) continue;; esac
    k="${line%%=*}"
    # `.env` 에 그 키가 **값과 함께** 있으면 건너뛴다. 빈 값이면 덮어쓴다(빈 값은 없음과 같다).
    cur="$(val "$k" "$SRC")"
    [ -n "$cur" ] && continue
    # 기존의 빈 줄(`KEY=`)이 있으면 지우고 새 값을 아래에 쓴다
    if grep -qE "^${k}=$" "$SRC"; then
      sed -i '' "/^${k}=$/d" "$SRC"
    fi
    printf '%s\n' "$line" >> "$SRC"; added=$((added+1))
  done < "$DST"
  chmod 600 "$SRC"
  echo "✅ $SRC 에 $added 개 키를 올렸다(백업 .bak.* 남김, 권한 600)"
  echo "   이제 \`bash scripts/sync-parse-svc-env.sh\` 로 parse-svc.env 를 파생시킨다."
  exit 0
fi

# ── 갱신 ──────────────────────────────────────────────────────────────────────
TMP="$(mktemp)"
{
  echo "# scripts/parse-svc.env — **자동 생성**(scripts/sync-parse-svc-env.sh)"
  echo "# 단일 출처는 리포 루트 \`.env\` 다. 여기서 직접 고치지 말고 \`.env\` 를 고친 뒤 다시 돌려라."
  echo "#   bash scripts/sync-parse-svc-env.sh"
  echo "# GITIGNORED — 실 비밀값이 들어간다."
  echo
  n=0
  for k in "${KEYS[@]}"; do
    v="$(val "$k" "$SRC")"
    [ -z "$v" ] && continue
    printf '%s=%s\n' "$k" "$v"; n=$((n+1))
  done
  # .env 에 없고 parse-svc.env 에만 있던 키는 보존한다(파싱 옵션을 조용히 날리지 않는다).
  if [ -f "$DST" ]; then
    echo
    echo "# ── 아래는 .env 에 없어 보존한 키다. .env 로 올리면 이 절이 비워진다. ──"
    while IFS= read -r line; do
      case "$line" in ''|'#'*) continue;; *=*) ;; *) continue;; esac
      k="${line%%=*}"
      [ -n "$(val "$k" "$SRC")" ] && continue
      printf '%s\n' "$line"
    done < "$DST"
  fi
} > "$TMP"

[ -f "$DST" ] && cp "$DST" "$DST.bak.$(date +%H%M%S)"
install -m 600 "$TMP" "$DST"; rm -f "$TMP"
echo "✅ $DST 갱신 (권한 600, 백업 .bak.* 남김)"
echo "   동기화된 키: $(grep -cE '^[A-Z_]+=' "$DST")개"

# ── --verify : 재기동 + 주입 실측 ─────────────────────────────────────────────
if [ "$MODE" = "--verify" ]; then
  echo
  echo "== parse-svc 재기동 후 실효 env 확인 =="
  bash "$ROOT/scripts/run-parse-svc.sh" >/dev/null 2>&1 || { echo "✗ 기동 실패" >&2; exit 1; }
  P="$(pgrep -f 'parse_service.app:app' | head -1)"
  [ -n "$P" ] || { echo "✗ 프로세스를 못 찾았다" >&2; exit 1; }
  fail=0
  for k in KBP_GATE_OCR_LANE KBP_PADDLE_OCR_GATEWAY_URL MODEL_NAME EXCEL_PARSER_BACKEND KORDOC_BIN; do
    want="$(val "$k" "$SRC")"; [ -z "$want" ] && want="$(val "$k" "$DST")"
    got="$(ps eww "$P" | tr ' ' '\n' | awk -F= -v K="$k" '$1==K {sub(/^[^=]*=/,""); print; exit}')"
    if [ -n "$want" ] && [ "$want" != "$got" ]; then
      echo "  ✗ $k — 기대 '$want' / 실제 '$got'"; fail=1
    else
      # 값이 비밀일 수 있으니 길이만 보고한다
      echo "  ✓ $k (주입됨, 길이 ${#got})"
    fi
  done
  [ "$fail" = "0" ] || exit 1
  echo "  ✅ 주입 확인"
fi
