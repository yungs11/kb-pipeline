# EXP-C1-20260811 — same-image GW/VL pair labeling (DEV)

> **2026-08-13 post-run correction:** 아래 `known GW strata`는 legacy `gw_labels.tsv.idx`를
> 현재 sample 행 번호로 잘못 결합한 값이었다. doc/page identity가 일치하지 않으므로 strata
> 설명은 무효이며, 본 표본은 convenience DEV로만 취급한다. 현재 원본·parser output·human
> label은 이 결합을 사용하지 않았다. 상세: `run-01/LEGACY_LABEL_JOIN_CORRECTION.md`.

## 1. 질문

동일한 원본 페이지 이미지에서 GW보다 VL이 안전하고 실질적으로 더 나은 페이지
(`VL_BENEFICIAL`)는 어떤 실패 양상에 나타나는가?

## 2. Hypothesis

- H0: 기존 GW 오류 유형과 VL의 상대 효용 사이에 재현 가능한 관계가 없다.
- H1: GW hard-fail보다 겉보기 정상인 silent corruption에서 VL rescue가 더 자주 나타난다.

## 3. Target

`VL_BENEFICIAL = GW에 material error가 있고, VL은 RETRIEVAL_PASS이면서
CRITICAL_ERROR 및 HALLUCINATION_CONFIRMED가 아님`

## 4. Sample

- source: 기존 `qual_sample_60p_normal.tsv`
- N: 16 pages / 16 documents
- split: DEV
- selection: 오류 양상 탐색을 위한 의도적 enrichment
- selected indices: 3, 10, 14, 17, 18, 19, 20, 24, 26, 35, 37, 41, 42, 46, 49, 56
- known GW strata: silent/error-like 5, hard-fail-like 5, existing usable controls 6
- prevalence나 production rate 추정에는 사용하지 않는다.

## 5. Engine Conditions

### Shared input

- exact same JPEG bytes for GW and VL
- render DPI: 150
- crop/orientation: current parser `render_pdf_pages` behavior, no extra preprocessing
- each sent JPEG is saved before either engine call and SHA-256 recorded

### GW

- current workspace `parse_service.parsers.pdf.paddle_gw._post_page`
- gateway URL/version: manifest에 실행 시 기록; server revision unavailable이면 명시
- lang: current env/default (`korean`)

### VL

- current workspace `ocr_elements_sync` + `page_hybrid_prompts()`
- model: explicit `MODEL_NAME`; implicit default 금지
- temperature: current parser fixed value 0.1
- max_tokens / guided JSON: execution manifest에 기록
- one call per page (run variance 측정 실험이 아님)

## 6. Candidate Features

- current v1 verdict/reason
- visible chars, Hangul/Han counts and CJK ratio
- degen HARD/SOFT rules and survival ratio
- repeated n-gram dominance / longest repeated run
- headings, tables, critical-pattern token counts
- GW/VL normalized length ratio

## 7. Threshold

이번 실험은 classifier threshold를 승격하지 않는 feature-discovery 실험이다.
사후 threshold가 제안되면 별도 revision/VALIDATION에서 고정해 검증한다.

## 8. Automated Metrics

- GW/VL engine success, empty, parser exception
- current v1 gate verdict
- output coverage proxies and feature values above
- latency
- SHA-256(input/output/config/prompt)

자동 metric만으로 품질 label을 확정하지 않는다.

## 9. Human Review Plan

- 16/16 전수 원본 대조
- 한 화면에서 original / GW / VL / feature / v1 verdict 확인
- 원본 전체와 페이지 후반부까지 검토
- critical fields: 당사자, 사건번호, 법원, 금액, 날짜, 조항번호, 주소
- labels: Retrieval Usability, Fidelity, Generative Safety, Coverage, Structure, source anomaly
- winner: GW / VL / TIE / BOTH_FAIL
- 애매하면 `ADJUDICATION_REQUIRED`; 자동으로 유리한 쪽을 선택하지 않는다.

## 10. Decision options

`PROMOTE`, `KEEP_OBSERVATIONAL`, `DEFER`, `REJECT` 중 하나만 선택한다.
