<!-- plan-version: v1 -->
<!-- codex-validation: READY v1 at 2026-06-22T11:45:06Z -->

# 엑셀 lane — facade 전략 라우터 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** kb_pipeline provider 의 엑셀(xlsx/xlsm/xls)을 facade `/parse` 에서 **excel-rag-parser(:18055, parse+chunk)** 로 라우팅해 native 청크로 insert(LLM 무관). 비엑셀·타 provider 불변.

**Architecture:** 전략 라우터(A). facade `/parse` 가 파일 ext 로 분기 — 엑셀이면 `ExcelRagParserClient` 위임(이식 없음), 아니면 기존 parse-svc. 엑셀은 facade `/parse` 응답에 `chunks`+`chunk_strategy` 를 실어 보내고, kb_pipeline tail 이 그 청크를 그대로 써 adaptive `/chunk` 를 건너뛴다.

**Tech Stack:** Python httpx, pytest. excel-rag-parser 비동기 잡(`POST /parse/jobs/file` + 폴링).

## Global Constraints

- **비엑셀 경로·타 provider(dify/edgequake/raganything/ragflow) 완전 불변.** 엑셀 분기만 추가(additive).
- **LLM 무관** — excel-rag-parser·edgequake insert 어디에도 modal/adaptive LLM 없음.
- facade 청크 contract = `{chunk_index:int, text:str, titles_context, pages:list}` (adaptive 경로와 동일).
- 엑셀 ext = `{xlsx, xlsm, xls}` (소문자).
- excel-rag-parser 청킹 로직 **이식·복붙 금지**(HTTP 위임만).
- 8.kb-pipeline 테스트: `.venv-kb/bin/python -m pytest`. knowledge_base 테스트: 해당 repo venv(`.venv/bin/python -m pytest`).

---

### Task 1: ExcelRagParserClient + RagChunk 정규화 (facade)

**Files:**
- Create: `8.kb-pipeline/service/excel_parser_client.py`
- Test: `8.kb-pipeline/service/tests/test_excel_parser_client.py`

**Interfaces:**
- Produces: `normalize_rag_chunk(rc:dict, index:int) -> dict|None`; `normalize_chunks(rag:list[dict]) -> list[dict]`; `ExcelRagParserClient(base_url, timeout=600, poll_timeout=1800, poll_interval=2.0).parse_chunks(*, file_bytes:bytes, filename:str) -> list[dict]`.

- [ ] **Step 1: 실패 테스트** — `service/tests/test_excel_parser_client.py`

```python
from service.excel_parser_client import normalize_rag_chunk, normalize_chunks


def test_normalize_drops_empty_text():
    assert normalize_rag_chunk({"content_text": "   "}, 0) is None
    assert normalize_rag_chunk({"content_text": "", "title": None}, 0) is None


def test_normalize_uses_content_text_and_path():
    out = normalize_rag_chunk({"content_text": "셀값", "path": ["시트1", "표A"], "title": "표A"}, 3)
    assert out == {"chunk_index": 3, "text": "셀값", "titles_context": ["시트1", "표A"], "pages": []}


def test_normalize_title_fallback_when_no_path():
    out = normalize_rag_chunk({"content_text": "x", "title": "제목"}, 0)
    assert out["titles_context"] == ["제목"]


def test_normalize_chunks_reindexes_after_dropping_empties():
    rag = [{"content_text": "a"}, {"content_text": ""}, {"content_text": "b"}]
    out = normalize_chunks(rag)
    assert [c["chunk_index"] for c in out] == [0, 1]
    assert [c["text"] for c in out] == ["a", "b"]
```

- [ ] **Step 2: 실패 확인** — Run `.venv-kb/bin/python -m pytest service/tests/test_excel_parser_client.py -q` → FAIL (ImportError).

- [ ] **Step 3: 구현** — `service/excel_parser_client.py`

```python
"""Thin HTTP client for excel-rag-parser (:18055) — the facade's Excel chunk strategy.

The facade routes Excel uploads here instead of parse-svc+adaptive_chunk. excel-rag-parser
parses AND chunks the workbook region-by-region (LLM-free) and returns RagChunk dicts; this
client polls its async job and normalizes RagChunks into the facade chunk contract.
"""
from __future__ import annotations

import time

import httpx

_OK = "succeeded"
_FAIL = ("failed", "cancelled")
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def normalize_rag_chunk(rc: dict, index: int) -> dict | None:
    """RagChunk dict → facade 청크 {chunk_index, text, titles_context, pages}. 빈 텍스트면 None."""
    text = (rc.get("content_text") or rc.get("title") or "").strip()
    if not text:
        return None
    title = rc.get("title")
    path = rc.get("path") or []
    titles_context = path or ([title] if title else None)
    return {"chunk_index": index, "text": text, "titles_context": titles_context, "pages": []}


def normalize_chunks(rag_chunks: list[dict]) -> list[dict]:
    out: list[dict] = []
    for rc in rag_chunks:
        norm = normalize_rag_chunk(rc, len(out))
        if norm is not None:
            out.append(norm)
    return out


class ExcelRagParserClient:
    def __init__(self, base_url: str, timeout: float = 600.0,
                 poll_timeout: float = 1800.0, poll_interval: float = 2.0):
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(timeout=timeout)
        self.poll_timeout = poll_timeout
        self.poll_interval = poll_interval

    def parse_chunks(self, *, file_bytes: bytes, filename: str) -> list[dict]:
        """POST /parse/jobs/file → poll /parse/jobs/{id} → 정규화된 facade 청크."""
        r = self.http.post(
            f"{self.base}/parse/jobs/file",
            files={"file": (filename, file_bytes, _XLSX_MIME)},
            data={"doc_name": filename},
        )
        r.raise_for_status()
        job_id = (r.json() or {}).get("job_id")
        if not job_id:
            raise RuntimeError("excel-rag-parser POST /parse/jobs/file returned no job_id")
        deadline = time.monotonic() + self.poll_timeout
        while True:
            t = self.http.get(f"{self.base}/parse/jobs/{job_id}")
            t.raise_for_status()
            tj = t.json() or {}
            status = (tj.get("status") or "").lower()
            if status == _OK:
                return normalize_chunks((tj.get("result") or {}).get("chunks") or [])
            if status in _FAIL:
                raise RuntimeError(f"excel-rag-parser job {status}: {tj.get('error')}")
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"excel-rag-parser poll timeout after {self.poll_timeout:.0f}s (last={status})"
                )
            time.sleep(self.poll_interval)
```

- [ ] **Step 4: 통과 확인** — Run pytest → PASS (4 passed).
- [ ] **Step 5: 커밋** — `git add service/excel_parser_client.py service/tests/test_excel_parser_client.py && git commit -m "feat(facade): excel-rag-parser client + RagChunk→facade chunk normalization"`

---

### Task 2: facade `/parse` 포맷 라우팅

**Files:**
- Modify: `8.kb-pipeline/service/app.py`
- Test: `8.kb-pipeline/service/tests/test_parse_endpoint.py` (있으면 추가; 없으면 생성)

**Interfaces:**
- Consumes: Task 1 `ExcelRagParserClient`.
- Produces: `_is_excel(filename)->bool`; `get_excel_client()`; `/parse` 가 엑셀이면 `{enriched_content, n_blocks, modal_spans:[], chunks:[...], chunk_strategy:"excel_rag_parser"}`, 비엑셀이면 기존 `{enriched_content, n_blocks, modal_spans}`.

- [ ] **Step 1: 실패 테스트** — `service/tests/test_parse_endpoint.py` 에 추가

```python
def test_is_excel():
    from service.app import _is_excel
    assert _is_excel("a.xlsx") and _is_excel("A.XLSM") and _is_excel("b.xls")
    assert not _is_excel("a.pdf") and not _is_excel("noext")


def test_parse_routes_excel_to_excel_client(monkeypatch):
    from fastapi.testclient import TestClient
    import service.app as svc

    class _FakeExcel:
        def parse_chunks(self, *, file_bytes, filename):
            return [{"chunk_index": 0, "text": "셀A", "titles_context": ["시트1"], "pages": []}]

    svc.app.dependency_overrides[svc.get_excel_client] = lambda: _FakeExcel()
    try:
        c = TestClient(svc.app)
        r = c.post("/parse", files={"file": ("book.xlsx", b"PK\x03\x04", "application/octet-stream")},
                   data={})
        assert r.status_code == 200
        j = r.json()
        assert j["chunk_strategy"] == "excel_rag_parser"
        assert j["chunks"][0]["text"] == "셀A"
        assert j["modal_spans"] == []
    finally:
        svc.app.dependency_overrides.pop(svc.get_excel_client, None)


def test_parse_routes_nonexcel_to_parse_svc(monkeypatch):
    from fastapi.testclient import TestClient
    import service.app as svc

    class _FakeParse:
        def parse(self, *, file_bytes, filename, content_type=None):
            return {"enriched_content": "본문", "n_blocks": 1, "modal_spans": []}

    svc.app.dependency_overrides[svc.get_parse_client] = lambda: _FakeParse()
    try:
        c = TestClient(svc.app)
        r = c.post("/parse", files={"file": ("doc.pdf", b"%PDF", "application/pdf")}, data={})
        assert r.status_code == 200
        j = r.json()
        assert "chunks" not in j and j["enriched_content"] == "본문"
    finally:
        svc.app.dependency_overrides.pop(svc.get_parse_client, None)
```

- [ ] **Step 2: 실패 확인** — Run `.venv-kb/bin/python -m pytest service/tests/test_parse_endpoint.py -q` → FAIL (`_is_excel`/`get_excel_client` 없음).

- [ ] **Step 3: 구현** — `service/app.py`

(a) import 추가: `from service.excel_parser_client import ExcelRagParserClient`.

(b) `get_parse_client` 아래에 추가:

```python
_EXCEL_EXTS = {"xlsx", "xlsm", "xls"}


def _is_excel(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[-1].lower() in _EXCEL_EXTS


def get_excel_client():
    return ExcelRagParserClient(
        os.environ.get("KBP_EXCEL_URL", "http://localhost:18055"),
        timeout=float(os.environ.get("KBP_EXCEL_TIMEOUT", "1800")),
    )
```

(c) `/parse` 핸들러 교체:

```python
@app.post("/parse")
async def parse(file: UploadFile = File(...), content_type: str | None = Form(None),
                pc=Depends(get_parse_client), ec=Depends(get_excel_client)):
    """Parse one upload. Excel → excel-rag-parser (parse+chunk, LLM-free); else → parse-svc.

    Excel returns native chunks (+ ``chunk_strategy``) so the caller skips adaptive /chunk.
    """
    data = await file.read()
    safe_name = _safe_basename(file.filename or "upload")
    if _is_excel(safe_name):
        chunks = ec.parse_chunks(file_bytes=data, filename=safe_name)
        return {
            "enriched_content": "\n\n".join(c.get("text", "") for c in chunks),
            "n_blocks": len(chunks),
            "modal_spans": [],
            "chunks": chunks,
            "chunk_strategy": "excel_rag_parser",
        }
    return pc.parse(file_bytes=data, filename=safe_name,
                    content_type=content_type or file.content_type)
```

- [ ] **Step 4: 통과 확인** — Run pytest service/tests → PASS (신규 3 + 기존 무회귀).
- [ ] **Step 5: 커밋** — `git add service/app.py service/tests/test_parse_endpoint.py && git commit -m "feat(facade): route Excel uploads to excel-rag-parser in /parse"`

---

### Task 3: KbPipelineClient.parse — chunks/chunk_strategy passthrough (knowledge_base)

**Files:**
- Modify: `knowledge_base/backend/app/clients/kb_pipeline_client.py`
- Test: `knowledge_base` 의 kb_pipeline_client 테스트(있으면 추가; 없으면 생성)

**Interfaces:**
- Produces: `KbPipelineClient.parse(...)` 반환에 `chunks`(엑셀 정규화 청크 또는 None)·`chunk_strategy`(또는 None) 추가. 비엑셀 응답이면 둘 다 None(기존 동작 불변).

- [ ] **Step 1: 실패 테스트** — respx 로 facade `/parse` mock

```python
def test_parse_passthrough_excel_chunks(respx_mock):
    import httpx
    from app.clients.kb_pipeline_client import KbPipelineClient
    respx_mock.post("http://fac/parse").mock(return_value=httpx.Response(200, json={
        "enriched_content": "x", "n_blocks": 1, "modal_spans": [],
        "chunks": [{"chunk_index": 0, "text": "셀", "titles_context": None, "pages": []}],
        "chunk_strategy": "excel_rag_parser",
    }))
    c = KbPipelineClient(base_url="http://fac")
    out = c.parse(file_bytes=b"x", filename="b.xlsx", content_type=None)
    assert out["chunk_strategy"] == "excel_rag_parser"
    assert out["chunks"][0]["text"] == "셀"


def test_parse_nonexcel_chunks_none(respx_mock):
    import httpx
    from app.clients.kb_pipeline_client import KbPipelineClient
    respx_mock.post("http://fac/parse").mock(return_value=httpx.Response(200, json={
        "enriched_content": "본문", "n_blocks": 2, "modal_spans": [],
    }))
    c = KbPipelineClient(base_url="http://fac")
    out = c.parse(file_bytes=b"x", filename="d.pdf", content_type=None)
    assert out.get("chunks") is None and out.get("chunk_strategy") is None
```

- [ ] **Step 2: 실패 확인** — Run knowledge_base venv pytest → FAIL (chunks 미반환).

- [ ] **Step 3: 구현** — `kb_pipeline_client.py` `parse()` 반환 dict 에 2줄 추가

```python
        return {
            "enriched_content": body.get("enriched_content") or "",
            "n_blocks": _to_int(body.get("n_blocks")),
            "modal_spans": body.get("modal_spans") or [],
            "chunks": body.get("chunks"),               # 엑셀 strategy: 정규화 청크, 아니면 None
            "chunk_strategy": body.get("chunk_strategy"),
        }
```

- [ ] **Step 4: 통과 확인** — Run pytest → PASS.
- [ ] **Step 5: 커밋** — `git add backend/app/clients/kb_pipeline_client.py <test> && git commit -m "feat(kb_pipeline_client): passthrough excel chunks/chunk_strategy from facade /parse"`

---

### Task 4: kb_pipeline tail 엑셀 분기 (knowledge_base)

**Files:**
- Modify: `knowledge_base/backend/app/core/pipeline.py::_ingest_kb_pipeline_tail`
- Test: kb_pipeline tail 테스트(있으면 추가; 없으면 생성)

**Interfaces:**
- Consumes: Task 3 의 `parsed["chunks"]`/`parsed["chunk_strategy"]`.
- Produces: parse 결과에 `chunks` 가 있으면 adaptive `/chunk` 호출을 건너뛰고 그 청크 사용, `chunking_selection.method_selected="excel_rag_parser"`. 없으면 기존 분기 그대로.

- [ ] **Step 1: 실패 테스트** — 엑셀(parse 가 chunks 반환)이면 `kbp.chunk` 미호출 + chunk_texts 가 excel 청크 텍스트, 비엑셀이면 `kbp.chunk` 호출. (기존 tail 테스트 패턴 따라 KbPipelineLike fake 주입.)

```python
def test_tail_excel_skips_adaptive_chunk():
    # parse 가 chunks 반환 → kbp.chunk 호출 안 됨, insert 에 excel 청크 텍스트 전달.
    calls = {"chunk": 0, "insert_chunks": None}

    class _KBP:
        def parse(self, *, file_bytes, filename, content_type):
            return {"enriched_content": "셀A\n\n셀B", "n_blocks": 2, "modal_spans": [],
                    "chunks": [{"text": "셀A"}, {"text": "셀B"}], "chunk_strategy": "excel_rag_parser"}
        def chunk(self, **k):
            calls["chunk"] += 1
            return {"chunks": [], "method_selected": None}
        def insert(self, *, workspace_id, doc_id, title, chunks):
            calls["insert_chunks"] = list(chunks)
            class _O: document_id = doc_id
            return _O()

    # ... 기존 _ingest_kb_pipeline_tail 호출 헬퍼로 _KBP 주입 ...
    # 검증: calls["chunk"] == 0 ; calls["insert_chunks"] == ["셀A", "셀B"]
    # ; rec.chunking_selection["method_selected"] == "excel_rag_parser"
```

(테스트 하네스: 기존 tail 테스트가 KbPipelineLike·deps 를 어떻게 주입하는지 그대로 재사용. 없으면 최소 fake deps 로 `_ingest_kb_pipeline_tail` 직접 호출.)

- [ ] **Step 2: 실패 확인** — Run pytest → FAIL (현재는 엑셀도 kbp.chunk 호출).

- [ ] **Step 3: 구현** — `pipeline.py` 현재 1947–1965 블록을 아래로 교체

```python
    enriched_content = (parsed or {}).get("enriched_content") or ""
    excel_chunks = (parsed or {}).get("chunks")  # 엑셀 strategy: facade /parse 가 native 청크 반환
    if not enriched_content.strip() and not excel_chunks:
        return _fail("kb_pipeline parse 결과 enriched_content 가 비어있습니다.")

    # ── 2) chunk — 엑셀이면 excel-rag-parser native 청크(전략 라우터), 아니면 facade /chunk(adaptive). ──
    _emit("chunk")
    if excel_chunks is not None:
        chunk_objs = list(excel_chunks)
        chunk_texts = [str((c or {}).get("text") or "") for c in chunk_objs]
        if not chunk_texts:
            return _fail("kb_pipeline chunk 결과가 비어있습니다(청크 0개).")
        chunking_selection = {
            "method_selected": (parsed or {}).get("chunk_strategy") or "excel_rag_parser",
            "scores": {},
            "methods_compared": [],
        }
    else:
        try:
            chunked = kbp.chunk(enriched_content=enriched_content, doc_name=new_docs_id)
        except Exception as exc:  # noqa: BLE001 - 청킹 실패 → 적재 실패
            return _fail(f"kb_pipeline chunk 실패: {exc!r}")
        chunk_objs = list((chunked or {}).get("chunks") or [])
        chunk_texts = [str((c or {}).get("text") or "") for c in chunk_objs]
        if not chunk_texts:
            return _fail("kb_pipeline chunk 결과가 비어있습니다(청크 0개).")
        chunking_selection = {
            "method_selected": (chunked or {}).get("method_selected"),
            "scores": (chunked or {}).get("scores") or {},
            "methods_compared": (chunked or {}).get("methods_compared") or [],
        }
```

- [ ] **Step 4: 통과 확인** — Run pytest (신규 + 기존 tail/provider 무회귀). 비엑셀·타 provider 테스트 green.
- [ ] **Step 5: 커밋** — `git add backend/app/core/pipeline.py <test> && git commit -m "feat(kb_pipeline): excel lane — use excel-rag-parser chunks, skip adaptive chunk"`

---

### Task 5: 통합 스모크 (수동, 선택)

- [ ] excel-rag-parser(:18055)·facade(:19000) 가동 확인. 실제 xlsx 를 facade `/parse` 로 POST → `chunk_strategy="excel_rag_parser"` + `chunks` 비어있지 않음. (LLM 무관이라 deepseek/qwen 상태와 독립.)

---

## Self-Review

- **Spec 커버리지:** ExcelRagParserClient+정규화(§3.1/§5)=Task1; facade 라우팅(§3.2)=Task2; client passthrough(§3.3)=Task3; tail 분기(§3.4)=Task4. 엣지(§7) 1–5 = Task2/4 테스트.
- **Placeholder:** 모든 코드 step 에 실제 코드/명령. Task4 테스트 하네스만 "기존 패턴 재사용" 으로 위임(워크플로 구현자가 기존 tail 테스트 구조 따름).
- **Type 일관:** facade 청크 = `{chunk_index,text,titles_context,pages}`; normalize 반환 동일; tail 은 `c["text"]` 사용.
- **불변식:** 비엑셀 facade `/parse` byte 동일(분기 else); KbPipelineClient.parse additive(None); tail else-분기 = 기존 코드 그대로; 타 provider 미접촉.
