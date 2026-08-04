"""facade 오브젝트 API — MinIO 은닉(제어평면).

지키는 계약:
  * **키 규칙은 facade 가 소유** — 소비자는 scope·doc_id·이름만 준다
  * 기존 객체와 byte-identical (마이그레이션 없음)
  * `staging` 은 평평한 키(kb `BlobStore` 계약)
  * 삭제는 key/prefix 중 **정확히 하나**
  * `X-Facade-Key` 게이트 대상
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from service.app import app, get_object_store
from service.objects import ObjectStore, ObjectStoreError


class FakeMinio:
    def __init__(self):
        self.store = {}
        self.puts = []

    def put_object(self, bucket, key, stream, length, content_type):
        self.puts.append((key, content_type))
        self.store[key] = stream.read()

    def get_object(self, bucket, key):
        if key not in self.store:
            raise RuntimeError("NoSuchKey")

        class R:
            def __init__(self, b): self._b = b
            def read(self): return self._b
            def close(self): pass

        return R(self.store[key])

    def remove_object(self, bucket, key):
        if key not in self.store:
            raise RuntimeError("NoSuchKey")
        del self.store[key]

    def list_objects(self, bucket, prefix, recursive):
        class O:
            def __init__(self, n): self.object_name = n

        return [O(k) for k in list(self.store) if k.startswith(prefix)]


@pytest.fixture()
def store():
    s = ObjectStore(FakeMinio(), bucket="document-parser")
    app.dependency_overrides[get_object_store] = lambda: s
    yield s
    app.dependency_overrides.pop(get_object_store, None)


@pytest.fixture()
def client():
    return TestClient(app)


def _put(client, path, data=b"xy", name="f.bin", ct="application/octet-stream"):
    return client.put(path, files={"file": (name, data, ct)})


# ── 업로드: 키 규칙 ────────────────────────────────────────────────────────

def test_put_original_produces_the_existing_key(client, store):
    r = _put(client, "/objects/original/a3f9c1/규정 (개정).xlsx")
    assert r.status_code == 200
    assert r.json()["key"] == "a3f9c1/original/규정 (개정).xlsx"


def test_put_page_appends_the_jpeg_extension(client, store):
    """소비자는 page_uuid 만 준다 — 확장자는 facade 가 붙인다."""
    uid = "0b7e4d2a-1c33-4f10-9a55-77e0c2b1d8ff"
    r = _put(client, f"/objects/page/a3f9c1/{uid}")
    assert r.json()["key"] == f"a3f9c1/{uid}.jpeg"


def test_put_staging_key_is_flat_with_prefix(client, store):
    """kb `BlobStore` 는 평평한 키다 — doc_id 를 끼워 넣으면 배치가 못 찾는다."""
    r = _put(client, "/objects/staging/sess-42/input.bin")
    assert r.json()["key"] == "parse-staging/sess-42/input.bin"


def test_put_stores_the_bytes(client, store):
    _put(client, "/objects/original/a3f9c1/x.pdf", data=b"PDF-BYTES")
    assert store.get("a3f9c1/original/x.pdf") == b"PDF-BYTES"


def test_put_honors_explicit_content_type_over_multipart(client, store):
    """소비자가 mime 을 알고 있으면 그게 이긴다 — 브라우저가 잘못 붙일 수 있다."""
    client.put("/objects/page/a3f9c1/p1",
               files={"file": ("p1", b"\xff\xd8", "application/octet-stream")},
               data={"content_type": "image/jpeg"})
    assert store._client.puts[0][1] == "image/jpeg"


# ── 업로드: 거부 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/objects/orginal/a3f9c1/x.pdf",          # scope 오타
    "/objects/original/../x.pdf",             # 상위 탈출
    "/objects/original/a3f9c1/../../etc/pw",
    "/objects/page/a3f9c1/p1.jpeg",           # 확장자 중복
    "/objects/original/a3f9c1",               # 이름 없음
])
def test_put_refuses_bad_keys_with_400(client, store, path):
    """소비자 잘못이라 400 이어야 원인이 보인다 — 500 이면 facade 버그로 읽힌다."""
    assert _put(client, path).status_code == 400


def test_refused_put_writes_nothing(client, store):
    _put(client, "/objects/original/../x.pdf")
    assert store._client.store == {}


# ── 회수 ───────────────────────────────────────────────────────────────────

def test_get_returns_the_bytes(client, store):
    store.put("parse-staging/s1", b"RAW", content_type="application/octet-stream")
    r = client.get("/objects", params={"key": "parse-staging/s1"})
    assert r.status_code == 200 and r.content == b"RAW"


def test_get_missing_key_is_404(client, store):
    assert client.get("/objects", params={"key": "nope"}).status_code == 404


def test_get_empty_key_is_400(client, store):
    assert client.get("/objects", params={"key": "  "}).status_code == 400


# ── 삭제 ───────────────────────────────────────────────────────────────────

def test_delete_single_key(client, store):
    store.put("a3f9c1/original/x.pdf", b"1", content_type="application/pdf")
    r = client.request("DELETE", "/objects", params={"key": "a3f9c1/original/x.pdf"})
    assert r.status_code == 200 and r.json() == {"deleted": 1}
    assert store._client.store == {}


def test_delete_prefix_removes_nested_keys(client, store):
    for k in ("doc1/original/a.pdf", "doc1/p1.jpeg", "doc2/p1.jpeg"):
        store.put(k, b"1", content_type="application/octet-stream")
    r = client.request("DELETE", "/objects", params={"prefix": "doc1/"})
    assert r.json() == {"deleted": 2}
    assert set(store._client.store) == {"doc2/p1.jpeg"}


@pytest.mark.parametrize("params", [
    {},                                          # 둘 다 없음
    {"key": "a", "prefix": "b"},                 # 둘 다 있음
])
def test_delete_requires_exactly_one_selector(client, store, params):
    """어느 쪽이 무시됐는지 모른 채 '지웠다'를 받으면 고아가 쌓인다."""
    assert client.request("DELETE", "/objects", params=params).status_code == 400


def test_delete_empty_prefix_is_refused(client, store):
    """공용 버킷이다 — 버킷 전체가 날아간다."""
    store.put("doc1/a.pdf", b"1", content_type="application/pdf")
    r = client.request("DELETE", "/objects", params={"prefix": "/"})
    assert r.status_code == 400
    assert set(store._client.store) == {"doc1/a.pdf"}


def test_delete_missing_key_reports_zero(client, store):
    """멱등 — 이미 없는 객체 삭제가 5xx 면 문서 삭제가 중간에 멈춘다."""
    r = client.request("DELETE", "/objects", params={"key": "nope"})
    assert r.status_code == 200 and r.json() == {"deleted": 0}


# ── 인증 ───────────────────────────────────────────────────────────────────

def test_object_routes_are_key_gated(monkeypatch, client, store):
    monkeypatch.setenv("KBP_FACADE_KEY", "s3cret")
    assert client.get("/objects", params={"key": "x"}).status_code == 401
    assert _put(client, "/objects/original/a3f9c1/x.pdf").status_code == 401
    assert client.request("DELETE", "/objects",
                          params={"key": "x"}).status_code == 401
    ok = client.request("DELETE", "/objects", params={"key": "x"},
                        headers={"X-Facade-Key": "s3cret"})
    assert ok.status_code == 200
