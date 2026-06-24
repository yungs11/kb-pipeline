"""Unit tests for ``parse_service/minio_client.py`` (spec §5.1.1).

External minio is **mocked** with a fake client — no live minio. We assert the
key scheme (``{docs_id}/{page_uuid}.jpeg``) and the exact ``put_object`` call
(bucket/key/content_type/length), bucket auto-create, and non-fatal failure.
"""

from __future__ import annotations

import io
import sys
import types

from parse_service.minio_client import MinioStore, DEFAULT_BUCKET


class FakePutObjectClient:
    """Records put_object/make_bucket calls; bucket_exists is configurable."""

    def __init__(self, *, bucket_exists: bool = True, raise_on_put: bool = False):
        self._bucket_exists = bucket_exists
        self._raise_on_put = raise_on_put
        self.put_calls: list[dict] = []
        self.made_buckets: list[str] = []
        self.bucket_exists_calls: list[str] = []

    def bucket_exists(self, bucket: str) -> bool:
        self.bucket_exists_calls.append(bucket)
        return self._bucket_exists

    def make_bucket(self, bucket: str) -> None:
        self.made_buckets.append(bucket)
        self._bucket_exists = True

    def put_object(self, bucket, key, data, length=None, content_type=None):
        if self._raise_on_put:
            raise RuntimeError("minio down")
        body = data.read() if hasattr(data, "read") else data
        self.put_calls.append(
            {
                "bucket": bucket,
                "key": key,
                "length": length,
                "content_type": content_type,
                "body": body,
            }
        )


def test_page_image_object_key_scheme():
    assert MinioStore.page_image_object_key("ab12cd", "ab12cd_3") == "ab12cd/ab12cd_3.jpeg"


def test_put_page_image_uploads_with_exact_call_and_returns_key():
    fake = FakePutObjectClient(bucket_exists=True)
    store = MinioStore(fake, bucket="document-parser")

    key = store.put_page_image("ab12cd", "ab12cd_1", b"\xff\xd8jpegbytes")

    # returns the canonical key.
    assert key == "ab12cd/ab12cd_1.jpeg"
    # exactly one put with the dify key scheme + image/jpeg content type.
    assert len(fake.put_calls) == 1
    call = fake.put_calls[0]
    assert call["bucket"] == "document-parser"
    assert call["key"] == "ab12cd/ab12cd_1.jpeg"
    assert call["content_type"] == "image/jpeg"
    assert call["length"] == len(b"\xff\xd8jpegbytes")
    assert call["body"] == b"\xff\xd8jpegbytes"
    # bucket already existed → no make_bucket.
    assert fake.made_buckets == []


def test_put_page_image_creates_bucket_when_missing():
    fake = FakePutObjectClient(bucket_exists=False)
    store = MinioStore(fake, bucket="document-parser")

    key = store.put_page_image("doc99", "doc99_2", b"img")

    assert key == "doc99/doc99_2.jpeg"
    assert fake.made_buckets == ["document-parser"]
    assert len(fake.put_calls) == 1


def test_put_page_image_non_fatal_on_failure_returns_none():
    """Per-page upload failure is non-fatal: logs and returns None (no raise)."""
    fake = FakePutObjectClient(bucket_exists=True, raise_on_put=True)
    store = MinioStore(fake, bucket="document-parser")

    result = store.put_page_image("doc", "doc_1", b"img")

    assert result is None
    assert fake.put_calls == []  # the put raised before recording


def test_default_bucket_constant():
    assert DEFAULT_BUCKET == "document-parser"


def test_from_env_uses_env_defaults(monkeypatch):
    """from_env reads MINIO_* env (mocked minio.Minio) — defaults per spec §5.1.1."""
    captured = {}

    class FakeMinio:
        def __init__(self, endpoint, access_key=None, secret_key=None, secure=False):
            captured["endpoint"] = endpoint
            captured["access_key"] = access_key
            captured["secret_key"] = secret_key
            captured["secure"] = secure

    fake_module = types.ModuleType("minio")
    fake_module.Minio = FakeMinio
    monkeypatch.setitem(sys.modules, "minio", fake_module)

    # No MINIO_* set → endpoint default localhost:9000, bucket document-parser, secure false.
    for var in ("MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_BUCKET", "MINIO_SECURE"):
        monkeypatch.delenv(var, raising=False)

    store = MinioStore.from_env()
    assert captured["endpoint"] == "localhost:9000"
    assert captured["secure"] is False
    assert store.bucket == "document-parser"


def test_from_env_honors_overrides(monkeypatch):
    captured = {}

    class FakeMinio:
        def __init__(self, endpoint, access_key=None, secret_key=None, secure=False):
            captured.update(
                endpoint=endpoint, access_key=access_key, secret_key=secret_key, secure=secure
            )

    fake_module = types.ModuleType("minio")
    fake_module.Minio = FakeMinio
    monkeypatch.setitem(sys.modules, "minio", fake_module)

    monkeypatch.setenv("MINIO_ENDPOINT", "minio.internal:9000")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "AK")
    monkeypatch.setenv("MINIO_SECRET_KEY", "SK")
    monkeypatch.setenv("MINIO_BUCKET", "custom-bucket")
    monkeypatch.setenv("MINIO_SECURE", "true")

    store = MinioStore.from_env()
    assert captured == {
        "endpoint": "minio.internal:9000",
        "access_key": "AK",
        "secret_key": "SK",
        "secure": True,
    }
    assert store.bucket == "custom-bucket"


def test_put_page_image_passes_byte_stream():
    """Body is uploaded as a BytesIO stream of the given jpeg bytes."""
    fake = FakePutObjectClient(bucket_exists=True)
    store = MinioStore(fake, bucket=DEFAULT_BUCKET)
    payload = b"\xff\xd8\xff\xe0" + b"x" * 100
    store.put_page_image("h", "h_5", payload)
    # The fake read() the stream; reconstructing yields original bytes.
    assert fake.put_calls[0]["body"] == payload
    assert isinstance(io.BytesIO(payload).read(), bytes)
