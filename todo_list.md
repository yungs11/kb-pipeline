# todo_list

> 변경/구현을 지시하는 작업 큐. 사용자가 여기에 항목을 적으면, 세션 시작 시 Claude 가 분석해 구현계획 → codex 검증 → 구현 → 문서 반영 → 항목 삭제 순으로 처리한다. (워크플로 상세: `CLAUDE.md` 참조.)
>
> 작성 예시:
> - [ ] (facade) /search 에 unified_search local/global 라우터 배선
> - [ ] (parse-svc) 그림 모달 vision LLM 백엔드 연결

<!-- 항목이 없으면 비워 둔다. Claude 는 비어 있으면 평소대로 진행한다. -->

<!--
완료(2026-06-29~30): (doc_guard) 엑셀 게이트웨이 검증 파서 후단 이동 + 추출오류/나란히2표 차단.
  설계: docs/superpowers/specs/2026-06-29-excel-gate-postparse-design.md
  계획: docs/superpowers/plans/2026-06-29-excel-gate-postparse.md (v2 READY)
  구현 브랜치(미머지): 7.excel-parser feat/excel-gate, doc_guard feat/excel-gate, knowledge_base feat/kb-pipeline-provider.
  남은 비범위: 위임전결 ○매트릭스 고도화, I2 perf(canvas 재사용), 라이브 스모크.
-->
