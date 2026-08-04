"""facade 오브젝트 키 규칙 — **기존 객체와 byte-identical** 이어야 한다.

이미 적재된 객체를 마이그레이션하지 않는다. facade 가 키 규칙을 넘겨받는 순간
한 글자라도 어긋나면 썸네일이 통째로 404 가 되고, 원인이 "이미지가 사라졌다"로
보여서 진단이 오래 걸린다. 그래서 소비자 쪽 규칙을 **문자열 리터럴로 박아** 두고
대조한다(소비자 모듈을 import 하면 규칙이 같이 바뀌어 회귀를 못 잡는다).

대조 대상(실측 2026-08-04):
  kb  `clients/minio_client.py:46`  original_object_key(docs_id, file_name)
                                    → f"{docs_id}/original/{file_name}"
  kb  `clients/minio_client.py:51`  page_image_object_key(docs_id, page_uuid)
                                    → f"{docs_id}/{page_uuid}.jpeg"
  kb  `clients/minio_client.py:205` MinioBlobStore(prefix="parse-staging/")
                                    → f"{prefix}{key}"
"""
from __future__ import annotations

import pytest

from service.objects import ObjectStore, ObjectStoreError, build_key


# ── 기존 규칙과의 byte-identical 대조 ──────────────────────────────────────

def test_original_key_matches_kb_rule():
    docs_id, file_name = "a3f9c1", "여신 규정 (개정).xlsx"
    assert build_key("original", docs_id, file_name) == f"{docs_id}/original/{file_name}"


def test_page_key_matches_kb_and_parse_svc_rule():
    """`.jpeg` 는 facade 가 붙인다 — 소비자는 page_uuid 만 준다.

    지금은 kb 와 parse-svc 가 각자 `.jpeg` 를 하드코딩하고 있어, 한쪽만 확장자를
    바꾸면 쓰는 키와 읽는 키가 조용히 어긋난다.
    """
    docs_id, page_uuid = "a3f9c1", "0b7e4d2a-1c33-4f10-9a55-77e0c2b1d8ff"
    assert build_key("page", docs_id, page_uuid) == f"{docs_id}/{page_uuid}.jpeg"


def test_page_key_does_not_double_the_extension():
    """소비자가 실수로 확장자를 붙여 보내도 `.jpeg.jpeg` 를 만들지 않는다."""
    with pytest.raises(ObjectStoreError):
        build_key("page", "a3f9c1", "0b7e4d2a.jpeg")


def test_staging_key_matches_blob_store_prefix():
    """kb BlobStore 계약은 **평평한 키**다 — doc_id 를 끼워 넣으면 안 된다."""
    assert build_key("staging", "", "sess-42/input.bin") == "parse-staging/sess-42/input.bin"


# ── 경로 탈출 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "scope,doc_id,name",
    [
        ("original", "..", "x.pdf"),               # doc_id 로 상위 탈출
        ("original", "a3f9c1", "../../etc/passwd"),
        ("original", "/abs", "x.pdf"),            # 선행 슬래시
        ("original", "a3f9c1", "/abs.pdf"),
        ("original", "a3f9c1", ""),               # 빈 이름 → 디렉터리 키
        ("original", "", "x.pdf"),
        ("page", "a3f9c1", "..\x00"),             # NUL
        ("staging", "", ".."),
    ],
)
def test_unsafe_components_are_refused(scope, doc_id, name):
    with pytest.raises(ObjectStoreError):
        build_key(scope, doc_id, name)


def test_unknown_scope_is_refused():
    """오타 난 scope 가 조용히 빈 프리픽스 키가 되면 안 된다."""
    with pytest.raises(ObjectStoreError):
        build_key("orginal", "a3f9c1", "x.pdf")


def test_korean_and_space_in_filename_survive():
    """원본 파일명은 사람이 올린 그대로다 — 정규화하면 기존 객체와 어긋난다."""
    k = build_key("original", "a3f9c1", "표 3-1 (최종).xlsx")
    assert k.endswith("/표 3-1 (최종).xlsx")


# ── ObjectStore ────────────────────────────────────────────────────────────

class FakeObj:
    def __init__(self, name):
        self.object_name = name


class FakeMinio:
    def __init__(self, objects=(), get_raises=False):
        self.store = dict(objects)
        self.get_raises = get_raises
        self.removed = []
        self.made_buckets = []
        self.puts = []

    def make_bucket(self, b):
        self.made_buckets.append(b)

    def put_object(self, bucket, key, stream, length, content_type):
        self.puts.append((bucket, key, content_type, length))
        self.store[key] = stream.read()

    def get_object(self, bucket, key):
        if self.get_raises or key not in self.store:
            raise RuntimeError("NoSuchKey")

        class R:
            def __init__(self, b): self._b = b
            def read(self): return self._b
            def close(self): pass
            def release_conn(self): pass

        return R(self.store[key])

    def remove_object(self, bucket, key):
        self.removed.append(key)
        self.store.pop(key, None)

    def list_objects(self, bucket, prefix, recursive):
        assert recursive is True          # 얕은 나열은 하위 키를 못 지운다
        return [FakeObj(k) for k in list(self.store) if k.startswith(prefix)]


def _store(**kw):
    c = FakeMinio(**kw)
    return ObjectStore(c, bucket="document-parser"), c


def test_put_does_not_create_the_bucket():
    """업로드 전용 자격증명에서 make_bucket 은 AccessDenied 다."""
    s, c = _store()
    s.put("a/b.pdf", b"xy", content_type="application/pdf")
    assert c.made_buckets == []
    assert c.puts == [("document-parser", "a/b.pdf", "application/pdf", 2)]


def test_get_returns_none_for_missing_key():
    s, _ = _store()
    assert s.get("nope") is None


def test_get_returns_bytes():
    s, _ = _store(objects={"a/b.pdf": b"hello"})
    assert s.get("a/b.pdf") == b"hello"


def test_delete_reports_failure_without_raising():
    s, c = _store()

    def boom(bucket, key):
        raise RuntimeError("minio down")

    c.remove_object = boom
    assert s.delete("a/b.pdf") is False


def test_delete_prefix_removes_all_nested_keys():
    s, c = _store(objects={"doc1/original/a.pdf": b"1", "doc1/p1.jpeg": b"2",
                           "doc2/p1.jpeg": b"3"})
    assert s.delete_prefix("doc1/") == 2
    assert set(c.store) == {"doc2/p1.jpeg"}


@pytest.mark.parametrize("prefix", ["", "   ", "/", "*", None])
def test_delete_prefix_refuses_to_wipe_the_bucket(prefix):
    """공용 버킷이다 — 빈 프리픽스면 kb 원본·페이지 이미지·잡 staging 이 함께 날아간다."""
    s, c = _store(objects={"doc1/a.pdf": b"1"})
    with pytest.raises(ObjectStoreError):
        s.delete_prefix(prefix)
    assert c.removed == []
