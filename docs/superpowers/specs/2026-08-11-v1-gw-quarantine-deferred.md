# v1 GW quarantine — deferred

짝 스펙: `/Users/xxx/.claude/plans/v1-gw-hardfail-quarantine.md` (v7, ultracode READY)
근거 실측: `/Users/xxx/Downloads/kbp-parser-compare/V1_DECISION.md`

이번 작업의 정의: **"확실히 망가진 것만 격리하고 나머지는 검색 가능성을 최대한 보존하는 파서."**
애매하면 보존이 원칙 — false quarantine 보다 silent deletion 이 더 나쁘다.

---

## D1. 표 R1 임계 0.16 의 FP 마진이 0.010 뿐이다

TP 최대 comp **.105**(`LOW_TTR` 픽스처) / FP 최소 comp **.170**(죽림 p18 등기부).
v1 은 임계를 바꾸지 않고 `KBP_DEGEN_COMPRESS_MAX` 로 env 화 + SOFT 관측 로그로 분포를 쌓는다.

**판정 기준**: 운영 로그에서 comp 0.10~0.17 구간 사례가 유의미하게 쌓이면 재조정.
`degen_filter` 의 `log.info("degen SOFT 관측(보존) …")` 이 **유일한 수집 경로**다 — 지우지 말 것.

## D2. `paddle_gw.py` 가 응답의 `layout`/`metrics`/`model` 을 버린다

`_post_page_once` 가 `body.get("text")` 만 반환한다. layout 은 추가 GPU 추론 없이 얻을 수 있다.

v1 은 layout 을 쓰지 않기로 확정했다 — 76p 실측에서 검증한 A/B/C feature(collapse anomaly /
coverage mismatch / structural complexity)가 **기존 text hard gate 대비 incremental value 0**
이었다(coverage 는 분리도 0, collapse 는 recall +0, complexity 는 recall +8~15%p 에 FPR
19~28%). **다만 "layout 이 무가치" 가 아니라 "그 세 feature 가 이 라벨 기준에서 도움이 안 됐다"** 다.

**판정 기준**: v1.1 의 `VL_ROUTE` 라벨(아래 D4) 기준으로는 **재평가하지 않았다.**
스키마·호출법은 `GW_API_GUIDE.md` 에 보존돼 있어 다시 꺼내는 비용이 낮다.

## D3. `prompts.py` `build_page_hybrid_prompts` 중복 정의 → `NameError`

`prompts.py:762-764` 의 def 가 754 행 별칭을 덮어쓰고 미정의 `_PAGE_HYBRID_EXTRA` 를 참조한다.
호출하면 `NameError`. 실측 확인했다. 이번 경로(paddle_gw 레인)와 무관해 손대지 않았다.

## D4. SOFT RISK trigger 미확보 — v1.1 의 핵심 연구 질문

이번에 **연구 질문 자체가 바뀌었다.**

```
기존(틀림): "GW unusable 을 어떻게 잡지?"
신규:       "GW 가 결과를 충분히 생성했지만 문자·핵심 용어가 조용히 틀린 페이지를 어떻게 찾지?"
```

target label 도 `GW_UNUSABLE` 이 아니라 **`VL_ROUTE = VL rescued GW`** 로 직접 잡아야 한다.

확보된 것은 **자격 조건(앞쪽 절반)뿐**이다 — VL 이 rescue 한 4건은 전부 "GW 가 건강해 보이는"
프로파일이었다(자수 942~1169, 한글 71~88%, 한자 0~1%, 루프 0, **표 없는 순수 서술 페이지**).
뒤쪽(조용한 오독 신호)은 없다 — "건강해 보인다" 는 usable 49p 도 대부분 만족한다.

```
SOFT_RISK = (GW 건강 프로파일 = escalation 자격)  AND  (조용한 오독 신호 = 미검증)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ 확보
```

**판정 기준**: 뒤쪽 신호가 실데이터로 검증되면 `page_verdict.py` 에 조건 한 줄을 붙여
`Verdict.ESCALATE_VL` 을 활성화한다. contract 는 이미 있다.

## D5. degen 안전화가 이미지/pptx 도메인 OCR 파서에도 적용된다

`filter_degenerate_pages` 호출부는 둘이다 — `pdf/__init__.py:81`(PDF 공통)과
`ocr/__init__.py:135`(**이미지/pptx 도메인 파서**, PDF vl 레인이 아니다).
R3/R4 → SOFT 강등은 두 경로 모두에 적용되므로 그쪽 삭제량도 줄어든다. **의도된 개선이지만
그 레인 실측은 하지 않았다.**

**판정 기준**: 회귀 감시 대상. 되돌림은 `KBP_DEGEN_SOFT_RULES=none`.

## D6. 라벨 문서 정합성

`GW_LABELS_EVIDENCE.md` 헤더는 `USABLE 49 / UNUSABLE 11` 인데, 세션 중 경계사례 2건
(괘법동 p20, 원주 반곡동 p3)을 UNUSABLE 로 재판정했다는 기록이 문서에 반영되지 않았다.
v1 판정에는 영향 없다(TP 3건이 모두 UNUSABLE 11 안).

또한 v1 의 `degen_filter` FP 근거 2건 중 **죽림 p18 은 경계 사례 8건 중 하나**다
(`### [39]`, "USABLE 로 판정했으나 이견 가능"). 장현 p52 는 USABLE 41 안이다.
→ 반례 강도는 **"USABLE 1건 + 경계 1건"**. R4→SOFT 결론 자체는 `R3/R4 단독 TP 0` 이라는
독립 근거가 있어 유지된다.

## D7. 게이트웨이 부분 장애의 자동 재시도·큐잉

v1 은 `status: "ok"|"error"` 로 **엔진 사고와 판정을 구분 가능하게** 만드는 데까지다.
`ENGINE_ERROR` 는 quarantine 과 별도 카운터로 집계된다. 재시도 메커니즘 자체는 범위 밖.

**판정 기준**: 운영에서 `ENGINE_ERROR` 비율이 유의미하게 쌓이면 착수.

## D8. 전 페이지 quarantine 을 상위에 실패로 노출

`app.py:307-314` 는 blocks 를 무조건 concat 하고 빈 결과 검사가 없다. 전 페이지 quarantine 이면
`enriched_content=""`, `n_blocks=0` 으로 **HTTP 200** 이 나가고 kb-backend 가 "파싱 성공한
빈 문서" 를 적재한다. v1 은 이 사실을 문서화하고 **전용 `log.warning`** 을 남기는 데까지 한다.

`parse_status`/`ParserError` 승격은 facade·kb-backend **데이터 계약 변경**이라 별건.

**판정 기준**: 실제로 빈 문서가 적재되는 사례가 관측되면 착수.

## D9. `MODEL_NAME` 프로세스 레벨 기동 실패

v1 은 `vl_api._require_model_name()` 의 `RuntimeError` 로만 강제한다(사용자 선택).
acceptance("미설정 상태에서 어떤 모델도 암묵 실행되지 않는다")는 이것으로 충족된다 —
리포 전수 grep 결과 `MODEL_NAME` 을 읽는 파이썬 경로가 그 함수뿐이다.

import-time 기동 실패를 채택하지 않은 이유: `/healthz` 까지 죽어 원인 파악이 어려워지고,
VL 을 전혀 호출하지 않는 paddle_gw 전용/parse-only 배포가 못 뜨며, pytest **collection
단계**가 깨진다.

**판정 기준**: 프로세스 레벨 실패가 필요해지면 startup 훅 한 줄.

## D10. 텍스트 T2/T3 임계 재조정

T2(5-gram 지배 0.35, USABLE 최대 0.25 → 마진 0.10) · T3(ttr 0.45, 마진 0.05).
"텍스트 degen 규칙 무변경" 이 v1 의 명시적 비목표였다. 60p 에서 TP 5 / observed FP 0 이라
당장 근거도 없다. `KBP_GW_DEGEN_MIN_CHARS=500` 으로 **증폭만 차단**했다.

## D11. `DEGEN_COLLAPSE` 의 ratio 0.5 값 자체

이 코퍼스에서는 (0,1) 어디에 둬도 결과가 같아 **검증 불가**. 운영 로그 축적 후 재조정.

## D12. 국한문혼용 코퍼스 확보 — CJK 임계·문서 가드 실증

60p 표본에 국한문혼용(한자 병기) 문서가 **한 장도 없다**. 구 등기부·제적등본·1980~90년대
계약서/정관은 페이지당 한자 30자·비율 0.30 을 전 페이지에서 넘긴다.

이 코퍼스의 실질 분리자는 `cjk_min=30` 이다(한자 30자 이상인 페이지가 정확히 3장:
정왕 p114 .757 / 경산 p419 .292 / 화성 p86 .274, USABLE 최대는 .047).
`ratio=0.50` 은 그 3장 중 정왕만 남기면서 국한문혼용 대비 안전마진을 두는 값이다.
**문서 단위 가드(`KBP_GW_CJK_DOC_RATIO`)도 이 코퍼스로는 검증 불가** — 60p 가 59개 문서라
(서로 다른 PDF 에서 1페이지씩) 가드가 자기 자신을 무력화한다.

**판정 기준**: 국한문혼용 문서를 확보하면 두 값을 실증한다. 그 전까지 **미검증**이다.

## D13. ink 계산 중복 렌더 제거

`page_ink` 는 EMPTY 후보 페이지에만 지연 호출되지만, `_supplement_diagram_pages` 의
`render_pdf_pages` 결과와 공유하면 렌더가 한 번 줄어든다. v1 성능 위험이 아니라 튜닝 항목.

## D14. `gw_raw` 코퍼스의 보관·비식별 정책

60건 중 7건에 주민번호 패턴이 있다. 로컬 분석 자료이고 **리포에 커밋하지 않는다**
(`scripts/dev/replay_gw_gate.py` 가 코퍼스 부재 시 skip 종료한다).
테스트 픽스처로 커밋한 것은 **PII 가 없음을 152셀 전수 확인한** 장현 p52 `table0` 하나뿐이다.

## D15. `EMPTY` 의 구조적 노출면

"저텍스트 + 스탬프/사진만 있는 페이지" 는 잉크가 높아 `EMPTY` → quarantine 으로 갈 수 있다.
표본에서 observed FP 0 이지만 이 유형이 없었을 뿐이다. 실운영 감시 대상.

## D16. 죽림 p18 을 회귀 픽스처로 쓰려면

R1 마진이 **.0096** 뿐이라 어떤 비식별화 방식을 써도 임계에 달라붙는다(실측: 상수 접기
comp .1610, 인물별 고유값 .1620 — 둘 다 0.16 을 0.001~0.002 차로 통과). zlib 버전·공백
처리만 바뀌어도 뒤집히는 flaky 앵커가 된다. 게다가 표 본문에 임원 성명·주소 PII 가 있다.

**판정 기준**: D1(임계 재조정) 또는 마진에 무관한 판정 축이 생기면 재검토.
현재 회귀 앵커는 마진 +.0519 인 장현 `table0` 이 담당한다.

## D17. 리플레이와 프로덕션의 렌더 DPI 차이

`scripts/dev/replay_gw_gate.py` 가 태우는 `gw_raw/` 코퍼스는 **300dpi** 로 만들었는데,
프로덕션 `paddle_gw` 의 기본은 **`KBP_PADDLE_GW_DPI=150`** 이다. 즉 V4 수치(quarantine 3 /
recall 3/11 / observed FP 0/49)는 **300dpi 입력에 대한 값**이다.

실제로 V5(실 게이트웨이, 150dpi)에서 춘천 p115 가 quarantine 되지 않았다 — 같은 페이지가
dpi 에 따라 다른 출력을 낸다. **게이트 로직의 문제가 아니라 입력이 다른 것**이고, v1 결론
(붕괴 페이지만 격리)은 어느 dpi 에서도 유지되지만 **§5 수치를 프로덕션 예측치로 인용하면 안 된다.**

**판정 기준**: 프로덕션 dpi 에서의 quarantine 비율을 운영 로그로 별도 관측한다.
