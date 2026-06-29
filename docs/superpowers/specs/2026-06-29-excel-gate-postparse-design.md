# 엑셀 게이트웨이 검증 재설계 — 파서 후단 + 추출오류/나란히2표 차단 (설계 스펙)

작성 2026-06-29 · 상태: 설계(브레인스토밍 완료) → 구현계획 단계

## 0. 한 줄 요약
doc_guard 게이트를 **파서 뒤**로 옮기고, 기존 13개 규칙을 전부 끈 뒤, **추출이 실제로 깨지는 경우(참조오류·헤더누수·빈헤더)와 나란히 놓인 무관한 두 표**만 차단한다. 차단 시 **사유 + 해당 행:열**을 알려 작성자가 엑셀을 고치게 한다. 검증 계산은 excel-parser가 요약으로 내보내고, doc_guard는 판정·메시지만 담당한다.

## 1. 목표 / 비범위

### 목표
- **최우선: 모든 형식의 엑셀을 파싱 가능하게.** 게이트는 추출이 깨지는 경우만 잡는다.
- 핵심 가정(실측): 기본 backend=kordoc 이 헤더 후보를 의외로 잘 찾는다(제목-위-헤더·쌓인표·개념제목 모두 정확 추출). 따라서 **값이 잘못 뽑히면 = 헤더가 잘못 추출된 것**으로 간주해 사유·위치를 알린다.
- 게이트를 파서 후단으로 이동(파서 추출 결과 기반 검증). UI 단계도 `파싱 → 게이트검증 → 청킹 → 적재`.

### 비범위 (향후 고도화)
- **위임전결기준표 류 ○/X 교차형 매트릭스**: openpyxl 특화 경로가 트리플(matrix_fact)로 추출. header:값 1:1 은 안 되지만 이번엔 **통과**시킨다. 매트릭스 전용 처리는 후속.
- 비-엑셀 문서(docx/pdf) 게이트: 기존 13규칙 전부 비활성 → **무검증 통과**.

## 2. 실측 근거 (왜 이 규칙인가)
| 파일/시트 | 구조 | kordoc 추출 | 게이트 판정 |
|---|---|---|---|
| aws_cost_estimate | 제목2행+평면(헤더3행) | 24청크 정확 | 통과 |
| 외부데이터 시트1 | 제목1행+평면(헤더2행) | 9청크 정확 | 통과 |
| 자산목록 "접근제어 적용 대상" | 쌓인 3표+개념제목 | 24청크 정확 | 통과 |
| 외부데이터 시트2 (법령리스트) | **나란히 2표**(순번\|법령 + 순번\|행정규칙) | 두 표를 한 행으로 오병합 | **차단** |
| WBS | 제목5행+계층(헤더6행) | 계층OK, **#REF! 적재 + 헤더누수** | **차단** |
| 위임전결기준표 | ○매트릭스+계단계층 | openpyxl 트리플 | 통과(향후) |

→ "헤더가 1행이 아님"·"쌓인 표"는 kordoc 이 잘 처리하므로 **차단하지 않는다**(과차단 반례 확인). 실제로 깨지는 건 **참조오류·헤더누수·빈헤더**와 **나란히 2표**뿐.

## 3. 아키텍처 (3-repo)

```
[knowledge_base :8001]                    [excel-parser :18055]              [doc_guard :8000]
 ingest_document()
   Stage: 파싱+게이트검증 (xlsx 일 때)
     ① POST /parse ───────────────────▶  파싱(auto→kordoc) + gate_summary 계산
          ◀── chunks + stats.gate_summary ──
     ② POST /v1/check-excel(payload=gate_summary) ─────────────────────────▶ 정책 판정 + 한국어 메시지
          ◀──────────── CheckReport{result, findings[], customer_message} ──
     fail → status=rejected + gate_popup(기존 UI 재사용)
     pass → provider tail (parse→chunk→insert)
 비-xlsx → 게이트 무검증 통과(기존 13규칙 호출 안 함)
```

- **검증 계산 위치 = excel-parser** (셀 그리드 보유 → 정확). doc_guard 는 요약을 받아 **판정·메시지만**.
- doc_guard 응답은 **기존 CheckReport 스키마 재사용** → 프론트 `GatePopup`/`gate_popup` 불변.

## 4. 컴포넌트 A — excel-parser: `gate_summary` 생성

### 4.1 위치
`excel_parser_rag/` 에 신규 모듈 `gate/excel_gate.py` (가칭). `pipeline.parse_excel_for_rag` 가 파싱 직후 호출해 `stats["gate_summary"]` 에 담는다. (stats 에는 이미 `validation_errors`,`sheets` 키 존재 — 동일 위치에 추가.)

### 4.2 입력
- openpyxl 워크북(원시 셀 그리드) — 참조오류·헤더위치·중복라벨 판정용
- 파싱 결과 chunks — 헤더누수·빈컬럼 판정용(실제 추출값 기준)

### 4.3 검출 규칙 (시트 단위)
모든 검출은 **사람이 읽는 셀 좌표**(예 `A2`,`C2`)를 함께 보고한다.

1. **ref_error** (참조오류): 셀 값(문자열)에 정규식 `#(REF|VALUE|DIV/0|N/A|NAME\?|NULL|NUM)!?` 매치 → 해당 셀 좌표 목록. (WBS H열에서 확인)
2. **header_leak** (헤더누수): 검출된 헤더행 라벨 집합과 **동일한 값 집합**을 갖는 데이터 청크/행이 존재 → 그 행 좌표. (WBS `단계=단계…`)
3. **empty_header** (빈헤더/빈컬럼명): 사용 열 범위 안에서 헤더 라벨이 빈 칸이거나, 헤더는 있으나 값이 전부 비는 열 → 해당 열.
4. **side_by_side** (나란히 2표): **헤더행에 동일 컬럼 라벨이 중복** 등장 → 중복 라벨 + 좌표. (법령리스트 `순번` @ A2,C2). 보조 신호로 헤더행 사이의 완전 빈 분리열도 가점.

> 위임전결 ○매트릭스는 위 4개 어디에도 안 걸리므로 통과(의도대로).

### 4.4 출력 스키마 (`stats.gate_summary`)
```json
{
  "ok": false,
  "sheets": [
    {
      "sheet": "법령리스트",
      "ok": false,
      "findings": [
        {"code": "side_by_side", "cells": ["A2", "C2"],
         "detail": "헤더 '순번'이 A2, C2에 중복되어 나란히 놓인 두 표로 판단됨"}
      ]
    },
    {"sheet": "외부데이터소스 현황", "ok": true, "findings": []}
  ]
}
```
- `ok` = 모든 시트 findings 없음. code ∈ {ref_error, header_leak, empty_header, side_by_side}.

### 4.5 엔드포인트
기존 `POST /parse` 응답 `stats.gate_summary` 로 노출(신규 엔드포인트 불필요). knowledge_base 가 게이트 단계에서 `/parse` 를 호출해 summary 를 얻는다.

## 5. 컴포넌트 B — doc_guard: 신규 엔드포인트 + 기존 규칙 비활성

### 5.1 신규 엔드포인트 `POST /v1/check-excel`
- 입력(JSON): `{filename, gate_summary}` (excel-parser 가 만든 4.4 요약)
- 처리: 정책상 **4개 code 전부 차단(error)**. (경고 없음 — 사용자 결정)
- 출력: 기존 `CheckReport` 스키마 재사용
  - `result`: 하나라도 finding 있으면 `"fail"`, 없으면 `"pass"`
  - `findings[]`: `{rule_id(=code), rule_name, severity:"error", location(=cells), message}`
  - `customer_message`: 한국어, **사유 + 해당 행:열 + "엑셀을 수정해주세요"** 안내문 조립
- 메시지 예:
  - side_by_side → "한 시트에 표가 좌우로 나란히 있습니다(헤더 '순번' 중복: A2, C2). 시트를 분리해 한 시트에 표 하나만 두세요."
  - ref_error → "참조 오류가 값에 포함되어 있습니다(H3: #REF!). 수식 오류를 정리한 뒤 다시 업로드하세요."
  - header_leak/empty_header → "헤더가 올바르게 인식되지 않았습니다(6행). 1행(또는 데이터 위 첫 행)을 헤더로, 그 아래를 값으로 정리해주세요. 문제 위치: …"

### 5.2 기존 규칙 비활성
- 기존 `POST /v1/check`(13규칙)은 **호출 경로에서 제거**(코드는 보존하되 knowledge_base 가 더는 호출 안 함). `GET /v1/rules` 카탈로그도 UI 에서 미사용(§6).

## 6. 컴포넌트 C — knowledge_base 통합

### 6.1 백엔드 (`core/pipeline.py`)
- 현재 Stage0 게이트(`pipeline.py:452-497`, 원시 바이트 → `deps.docguard.check`)를 **파서 후단 게이트**로 교체:
  - 확장자가 xlsx/xlsm 이면: excel-parser `/parse` 호출 → `stats.gate_summary` → doc_guard `/v1/check-excel` 호출.
  - fail → 기존과 동일하게 `status="rejected"` + `_build_gate_popup(report)` + `set_doc_guard_result`.
  - pass / 비-xlsx → 게이트 통과(13규칙 호출 안 함).
- `docguard_client.py` 에 `check_excel(gate_summary, filename)` 메서드 추가(기존 `check` 는 미사용).
- 단계 emit: 게이트가 파서 결과를 쓰므로 UI 표기 순서가 `파싱 → 게이트검증` 이 되도록 stage 이벤트 순서 조정(§6.2 와 짝).

### 6.2 프론트 (`frontend/components`)
- `JobList.tsx` `KB_PIPELINE_STAGE_ORDER` 를 `[게이트검증, 파싱, 청킹, 적재]` → **`[파싱, 게이트검증, 청킹, 적재]`** 로 재정렬. (기존: `gate, parse, chunk, insert` → 신규: `parse, gate, chunk, insert`.)
- `UploadPanel.tsx` 의 "문서 가드 규칙" 체크박스 영역(167–193행 일대) **숨김**(렌더 제거 + `listDocguardRules` 호출 제거 또는 무력화). 업로드 시 `disabled_rules` 전송 로직도 제거.

### 6.3 게이트 시점/이중 파싱 메모
- kb_pipeline provider tail(facade `/ingest`)도 내부에서 파싱한다 → 엑셀은 게이트 파싱 + tail 파싱이 **이중**이 될 수 있음. 엑셀 파싱은 결정적·저비용이라 v1 은 허용. UI 의 "파싱"은 게이트 단계의 excel-parser 호출을 표기하고, tail 내부 파싱은 중복 표기하지 않는다(stage emit 1회 보장).
- (후속 최적화: 게이트 파싱 결과를 tail 로 전달해 재파싱 제거 — 이번 비범위.)

## 7. 데이터 계약 요약
- excel-parser `/parse` → `{chunks[], stats{…, gate_summary{ok, sheets[]}}}`
- doc_guard `/v1/check-excel` 입력 `{filename, gate_summary}` → `CheckReport{result, summary, findings[], customer_message}`
- knowledge_base → 기존 `gate_popup`/`set_doc_guard_result`/`status=rejected` 재사용.

## 8. 에러 처리
- excel-parser `/parse` 실패(파싱 자체 실패) → 게이트는 보수적으로 **차단**(추출 불가 = 적재 불가) + 메시지("파일을 읽을 수 없습니다"). 단 파싱 타임아웃/서비스다운은 운영 오류로 구분해 로깅.
- doc_guard 다운/오류 → 기존 정책 따름(advisory 통과 vs 차단). 기본은 **통과(advisory)** 로 두되 로깅(게이트 장애가 적재 전체를 막지 않도록). 최종 정책은 구현계획에서 확정.
- 비-xlsx → 항상 통과.

## 9. 테스트 (수용 기준)
**차단되어야 정상:**
- 외부데이터 시트2(나란히 2표) → side_by_side 차단, 메시지에 `A2,C2`
- WBS(#REF! 포함) → ref_error 차단, 메시지에 해당 셀

**통과되어야 정상:**
- 외부데이터 시트1 / aws_cost_estimate / 자산목록 "접근제어 적용 대상" → 통과
- 위임전결기준표 → 통과(향후 고도화)

**격리:** docx/pdf 및 다른 provider(dify/edgequake/raganything) 적재에 영향 0(엑셀만 신규 게이트).

## 10. 변경 파일 (예상)
| repo | 파일 | 종류 |
|---|---|---|
| 7.excel-parser | `excel_parser_rag/gate/excel_gate.py` | 신규 |
| 7.excel-parser | `excel_parser_rag/pipeline.py` (gate_summary 호출) | 수정 |
| doc_guard | `app/main.py` (+`/v1/check-excel`), `app/core.py`/신규 excel 정책 모듈 | 수정/신규 |
| knowledge_base | `backend/app/core/pipeline.py` (게이트 후단화), `clients/docguard_client.py` (`check_excel`) | 수정 |
| knowledge_base | `frontend/components/JobList.tsx`, `frontend/components/UploadPanel.tsx` | 수정 |

## 11. 미해결(구현계획에서 확정)
- header_leak/empty_header 의 정확한 판정 알고리즘(검출 헤더행 식별 방식: openpyxl 헤더탐지 재사용 vs 단순 1행/2행 규칙).
- doc_guard 장애 시 통과 vs 차단 기본값.
- tail 이중 파싱 stage emit 1회 보장 방법(facade/knowledge_base 어느 쪽에서 억제).
