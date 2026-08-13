# parse-svc 를 별도 레포로 분리 — 보류 기록 (2026-08-11)

> 계기: doc_guard 이미지 드리프트를 파다가 "룰 원천이 왜 kb-pipeline 에 있나"로 번졌고,
> 사용자가 **parse-svc 도 doc_guard·adaptive_chunk 처럼 별도 레포로 유지보수하는 편이
> 낫지 않나**를 제기했다. 같은 세션 기록: `_workspace/02-changes.md`
> "엑셀 게이트 룰 사전 동기화 + 번들 소스 출처 가드".

## 판정 — 방향은 타당하다. 다만 **지금은 아니다**

보류 사유는 설계가 틀려서가 아니라 **파서 작업이 아직 많이 남아서**다(사용자 판단).
분리는 파서 소스가 흔들리는 동안 하면 이관·회귀 비용이 그대로 곱해진다.

## 근거 — 사용자 주장을 뒷받침하는 실측 (2026-08-11)

```
parse_service/  = 추적 파일 115 / 283        (41%)
최근 200 커밋 중 parse_service 를 건드린 것 = 190 (95%)
parse_service/  = 92 파일 / 15,075 줄 (테스트 제외)
```

**kb-pipeline 의 커밋 이력이 사실상 parse-svc 이력이다.** facade·청킹·검색 변경이 파서
커밋에 파묻힌다. gitea 가 kb / kb-pipeline / doc_guard / adaptive_chunk / excel-parser 로
나뉘어 있는데 가장 활발한 컴포넌트만 남의 레포에 얹혀 있다.

## 핵심 정정 — 드리프트는 "분리" 탓이 아니라 "핀 없음" 탓이다

오늘 겪은 사고(7/1 이미지·브랜치 드리프트·룰 어휘 갈라짐)를 근거로 분리에 반대하면 틀린다.
같은 "별도 레포"인데 결과가 갈린다:

| | 형태 | kb-pipeline 이 커밋을 아는가 |
|---|---|---|
| doc_guard / adaptive_chunk | `../99.projects/...` 상대경로 (sibling) | ❌ 모른다 → 아무 흔적 없이 샌다 |
| edgequake | **서브모듈** (`.gitmodules`) | ✅ SHA 로 기록 |

→ **분리한다면 sibling 방식이 아니라 edgequake 방식(서브모듈)으로.**
서브모듈이면 kb-pipeline 이 정확한 커밋을 기록하고, `BUILD-PROVENANCE.txt`(오늘 추가)에도
찍힌다. 한계도 있다 — 서브모듈 워킹트리도 다른 브랜치로 체크아웃될 수 있다(현재 edgequake 가
`feat/kb-pipeline-provider` + dirty). 다만 `git submodule status` 가 `+` 로 표시하고,
`build-bundle.sh` 의 `EXPECT_BRANCH` 가 잡는다.

## 착수 시 결정해야 할 것 — `kb_pipeline/{blockify,modal}` 소유권

현재 import 9곳 중 **7곳이 parse_service 안**이다(2026-08-11 실측).

| 모듈 | import 위치 | 제안 |
|---|---|---|
| `kb_pipeline.blockify` | `parse_service/router.py:36`, `parsers/ocr/__init__.py:105`, `parsers/pdf/paddle_gw.py:138`, `parsers/pdf/__init__.py:213,231,278`, `parsers/docx/__init__.py:14` | 사실상 파서 전용 → **파서 레포로 이관** |
| `kb_pipeline.modal` | `parse_service/app.py:33` (생산자) / 소비자는 `service/adaptive_chunk.py`·`service/edgequake.py`·`service/jobs/runner.py` | **생산자가 포맷을 소유** → 파서 레포로, kb-pipeline 은 서브모듈 경로에서 import |
| `service.llm.get_text_llm` | `parse_service/app.py:400` | 역방향 의존 — 분리 시 끊어야 한다 |

서브모듈이면 코드가 디스크에 있으므로 **패키징·버전핀 인프라가 따로 필요 없다**(sibling 대비
서브모듈의 실질 이득). 모달 마커 U+3008/U+3009 불변식도 정의처 한 곳이 유지된다.

## 최대 리스크

**모달 마커 계약**. 생산자(파서)와 소비자(청킹·적재)가 갈리면 버전 스큐가 생기고, 어긋나도
파싱이 실패하는 게 아니라 **청킹이 조용히 어긋난다** — 폐쇄망에서만 드러날 종류다.
이관 후 `enriched_content` 가 byte-identical 인지 확인하는 회귀 테스트가 **필수**다.

## 착수 시 단계 (초안)

1. gitea `parse-svc` 레포 생성 + `git subtree split` 으로 이력 보존 이관
2. `kb_pipeline/{blockify,modal}` 이관 + kb-pipeline import 경로 수정(facade 3곳)
3. `service.llm` 역방향 의존 제거
4. kb-pipeline 에 서브모듈 등록 + `Dockerfile.parse-svc` build context 조정
5. `scripts/airgap/build-bundle.sh` `EXPECT_BRANCH` 에 항목 추가
6. 모달 마커 byte-identical 회귀 테스트
7. (별건) `parse_service/parsers/excel/excel_parser_rag` 벤더링 정리 — `7.excel-parser`
   에서 복사한 것으로 **핀이 없다**. 2026-08-11 대조 시 diff 0 이지만 doc_guard 와 같은
   부류의 시한폭탄이다.

범위가 크므로 착수 시 plan 파일 + ultracode 경쟁 검증을 거친다(글로벌 룰).

---

## 함께 확인된 것 — adaptive_chunk 의 별도 레포는 **맞다**

kb-pipeline 은 adaptive_chunk 코드를 **import 하지 않는다**. 경계가 HTTP 하나뿐이다
(`service/adaptive_chunk.py` = 얇은 HTTP 클라이언트, `POST /chunk/jobs` 폴링).
공유 라이브러리도, 역방향 의존도 없다 → 별도 레포가 맞는 형태다.
doc_guard 도 같다(facade `/gate/*` 뒤 HTTP).

**다만 둘 다 핀이 없다**(sibling 상대경로). 오늘 `build-bundle.sh` 에 브랜치 가드 +
`BUILD-PROVENANCE.txt` 를 넣어 최소한 **기록은 남게** 했지만, 서브모듈처럼 커밋이
kb-pipeline 이력에 박히지는 않는다. 이 둘도 서브모듈로 옮길지는 별건으로 남긴다.
