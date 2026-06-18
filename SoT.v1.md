지식베이스 파이프라인
1. parser
**XLS ,XLSX/HWP,HWPX,HWPML:: kordoc**
**PDF: markitdown**
**pptx: markitdown**
**DOCX: markitdown**
/Users/xxx/workspace/excel-parser-markitdown
참고하여 프로젝트 경로에 직접 설치

2. chunker
adaptive chunk
/Users/xxx/workspace/99.projects/adaptive_chunk
(백엔드 서버 사용)

3. insert
- edgequake 스키마 차용
Postgres + pgvector + AGE (per-KB schema/RLS)
https://github.com/raphaelmansuy/edgequake.git

4. search
vector + GraphRAG-global 커뮤니티 리포트, 단 KB별로 빌드 

# 궁금한점
- lightRAG 의 앞단에 파서와 청킹을 고도화한게 raganything이라고 알고있다.
그럼 raganything 베이스로 파서와 청킹을 바꾸고 edgequake 운영환경에 insert하는게 나을까
묻는 이유는, raganything의 content_list 만드는 방식이 edgequake로 적재하는 방식보다 더 벡터/관계에 효율적일듯 하여 그렇다.
소스참고 경로: /Users/xxx/workspace/99.projects/raganything_svc