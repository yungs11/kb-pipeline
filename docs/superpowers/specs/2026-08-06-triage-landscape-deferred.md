# 가로형 triage 규칙 + 페이지 전사 프롬프트 통합 — 범위 밖 발견 기록

> 짝 작업: 가로형 페이지(width>height) → `Bucket.LLM_NEEDED` triage 규칙 추가
> (`parse_service/parsers/pdf/triage.py`) + 페이지 전사 경로(`_vl_lane`/`_odl_lane` 스캔
> 페이지)의 VL 프롬프트를 PAGE_HYBRID 로 통일(`parse_service/parsers/pdf/__init__.py`).
> plan: `/Users/xxx/.claude/plans/mighty-whistling-quiche.md`(ultracode 경쟁 검증
> v1~v5, 각 라운드에서 발견된 것 중 이번 작업 목표에 필수가 아닌 것을 여기 모은다).

## 판정 기준

이번 작업(가로형 triage + 페이지 전사 프롬프트 통일)에 넣는 것은 둘 중 하나다.

1. 없으면 **가로형 규칙이 목표대로 동작하지 않는다**(VL로 안 가거나, diagram_pages 집계가
   깨지거나, 기존 테스트가 회귀한다)
2. 없으면 **페이지 전사 프롬프트 통일이 반쪽짜리가 된다**(사용자가 "전체적으로"라고 명시한
   범위를 못 채운다)

나머지는 여기로 온다.

---

## D1. `_odl_lane`의 스캔페이지 일반 OCR도 밋밋한 전사 프롬프트 — ✅ **해결됨(같은 작업에서 처리)**

처음 계획(v3)은 "가로형→vl 레인" 경로(`_vl_lane`, line 187)만 고칠 예정이라 `_odl_lane`의
스캔페이지 일반 OCR(line 223, 네이티브 텍스트 없는 페이지를 처음부터 VL로 전사하는 경로)은
같은 근본 원인(순수 `build_system_prompt`/`build_user_prompt` fallback, 순서도 흐름서술·
차트 요약 조항 없음)을 안고 있으면서도 범위 밖으로 남겨뒀다.

사용자가 "PAGE 전사는 가로형뿐 아니라 전체적으로 통일해야 한다"고 확장 지시해, 같은 작업에서
line 187(`_vl_lane`)과 line 223(`_odl_lane` 스캔페이지) 둘 다 PAGE_HYBRID 로 통합됐다.
별도 후속 작업 불필요.

## D2. `_supplement_diagram_pages`(line 260, 다이어그램 보충)의 `DIAGRAM_*` 프롬프트는 통합 대상이 아니다 — **의도적으로 범위 밖 유지**

v4 검증(ultracode, logic-correctness + adversarial-break 2개 독립 렌즈 수렴)에서 발견:
"`DIAGRAM_USER_PROMPT`는 순서도만 다루고 표/본문 전사 규칙이 없다"는 전제로 line 260 도
PAGE_HYBRID 로 통합하려 했으나, 그 전제가 실측(prompts.py:587-596)과 달랐다 —
`DIAGRAM_USER_PROMPT`는 이미 "순서도 밖의 표·주석·체크리스트·서류 목록 텍스트는 원문 그대로
보존"을 명시한다. 그리고 `_supplement_diagram_pages`가 이 페이지에 얹는 방식은 **ODL 레인에서
`replace=False`(additive, `entry["blocks"].extend(extra)`)** — 그 페이지는 이미 ODL 네이티브
마크다운(`hybrid_to_blocks(md)`)으로 표/본문 블록을 정확히 갖고 있다. PAGE_HYBRID(표/본문/
그림을 각각 별도 element 로 분해하는 범용 결정트리)로 바꾸면 그 위에 표/본문이 **중복**되는
회귀가 생긴다. 그래서 line 260 은 계속 `DIAGRAM_*`를 쓴다 — "전체적으로 통일" 지시는 **페이지를
처음부터 전사하는 경로(대체할 기존 콘텐츠가 없는 경우)**에 한정되고, "이미 있는 콘텐츠에
추가/교체하는 보충 경로"는 성격이 달라 통일 대상이 아니다.

**판정기준**: 있으면 더 좋다가 아니라 **있으면 회귀**(표/본문 중복) — 명확히 하지 않는 것이
Phase 목표. 재검토 트리거: 향후 `_supplement_diagram_pages`가 additive 모드를 버리거나
figure-only 필터링을 갖추면, 그때는 PAGE_HYBRID 로 안전하게 통합할 수 있다(현재는 없음).
