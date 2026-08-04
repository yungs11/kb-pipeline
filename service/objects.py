"""오브젝트 저장소 — facade 가 MinIO 를 은닉하는 **제어평면** 계층.

설계: ``docs/superpowers/specs/2026-08-04-facade-gate-object-api-design.md`` §3

**제어평면만 다룬다.** 브라우저의 썸네일·인용 이미지 읽기(`/obj/*` same-origin 프록시)는
여기로 오지 않는다 — 그건 데이터평면이고 실측상 검색 1회에 최대 ~4MB 라, facade 를
정적 파일 서버로 만들면 잡 접수·`/healthz` 가 스레드를 못 얻는다(§3.3).

여기서 얻는 값은 **키 규칙 소유**다. 지금은 `{docs_id}/original/{name}` 같은 규칙을
kb·parse-svc·facade 셋이 각자 알고 있어, 한 곳이 바뀌면 조용히 어긋난다.
"""
from __future__ import annotations

import io
import logging
import os
import re
from typing import Any, Literal

log = logging.getLogger("kb_pipeline.service.objects")

Scope = Literal["original", "staging", "page"]

#: 키 규칙 — **facade 가 소유한다**. 소비자는 scope·doc_id·이름만 준다.
#:
#: **기존 객체와 byte-identical 해야 한다**(이미 적재된 333MB 를 마이그레이션하지 않는다).
#: 실측으로 확인한 현행 규칙:
#:
#:   original : {docs_id}/original/{file_name}   ← kb minio_client.original_object_key
#:   page     : {docs_id}/{page_uuid}.jpeg       ← kb·parse-svc 양쪽 page_image_object_key
#:   staging  : parse-staging/{key}              ← kb MinioBlobStore(prefix="parse-staging/")
#:
#: `page` 의 `.jpeg` 는 **facade 가 붙인다** — 지금은 kb·parse-svc 가 각자 하드코딩하고
#: 있어 한쪽만 바뀌면 조용히 어긋난다. 소비자는 page_uuid 만 준다.
_LAYOUT = {
    "original": "{doc_id}/original/{name}",
    "page": "{doc_id}/{name}.jpeg",
    "staging": "parse-staging/{name}",
}

#: staging 은 doc_id 를 키에 넣지 않는다(kb 의 BlobStore 계약이 평평한 키다).
_NO_DOC_ID = {"staging"}

#: 경로 탈출 차단. `..`·선행 슬래시·제어문자를 막는다.
_SAFE = re.compile(r"^[^/\x00][^\x00]*$")


class ObjectStoreError(RuntimeError):
    pass


def build_key(scope: str, doc_id: str, name: str) -> str:
    """``(scope, doc_id, name)`` → 객체 키. 규칙을 이 함수 하나가 소유한다."""
    if scope not in _LAYOUT:
        raise ObjectStoreError(f"unknown scope: {scope!r}")
    parts = (name,) if scope in _NO_DOC_ID else (doc_id, name)
    for part in parts:
        if not part or not _SAFE.match(part) or ".." in part:
            raise ObjectStoreError(f"unsafe path component: {part!r}")
    if scope == "page" and "." in name:
        # 확장자는 여기서 붙인다. 소비자가 이미 붙여 보내면 `x.jpeg.jpeg` 가 되고,
        # 쓰는 키와 읽는 키가 어긋나 썸네일이 조용히 404 가 된다.
        raise ObjectStoreError(f"page name must be a bare page_uuid, got {name!r}")
    return _LAYOUT[scope].format(doc_id=doc_id, name=name)


class ObjectStore:
    """MinIO 래퍼. 클라이언트를 주입할 수 있어 테스트가 fake 로 돈다."""

    def __init__(self, client: Any, *, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_env(cls) -> "ObjectStore":
        from minio import Minio

        secure = (os.environ.get("MINIO_SECURE", "") or "").lower() in {"1", "true", "yes", "on"}
        client = Minio(
            os.environ.get("MINIO_ENDPOINT", "localhost:9000"),
            access_key=os.environ.get("MINIO_ACCESS_KEY", ""),
            secret_key=os.environ.get("MINIO_SECRET_KEY", ""),
            secure=secure,
        )
        return cls(client, bucket=os.environ.get("MINIO_BUCKET", "document-parser"))

    @property
    def bucket(self) -> str:
        return self._bucket

    def put(self, key: str, data: bytes, *, content_type: str) -> str:
        # make_bucket 은 부르지 않는다 — 제한된 업로드 전용 자격증명에서 AccessDenied 다
        # (parse_service/minio_client.py:99-103 과 동일 근거).
        self._client.put_object(
            self._bucket, key, io.BytesIO(data), length=len(data), content_type=content_type
        )
        return key

    def get(self, key: str) -> bytes | None:
        """객체 바이트. 없으면 ``None``(호출자가 404 로 옮긴다)."""
        try:
            resp = self._client.get_object(self._bucket, key)
        except Exception as exc:  # noqa: BLE001 - NoSuchKey 를 None 으로
            log.info("object get miss %r: %s", key, exc)
            return None
        try:
            return resp.read()
        finally:
            for m in ("close", "release_conn"):
                fn = getattr(resp, m, None)
                if fn:
                    fn()

    def delete(self, key: str) -> bool:
        try:
            self._client.remove_object(self._bucket, key)
            return True
        except Exception:  # noqa: BLE001 - 삭제 실패는 비치명
            log.warning("object delete failed %r", key, exc_info=True)
            return False

    def delete_prefix(self, prefix: str) -> int:
        """프리픽스 일괄 삭제. 문서·KB 삭제에서 쓴다.

        **빈 프리픽스는 거부한다** — 버킷 전체가 날아간다. 이 버킷은 kb 원본·페이지
        이미지·잡 큐 staging 이 프리픽스로 나눠 쓰는 공용 버킷이다.
        """
        prefix = (prefix or "").strip()
        if not prefix or prefix in {"/", "*"}:
            raise ObjectStoreError("refusing to delete an empty prefix")
        n = 0
        for obj in self._client.list_objects(self._bucket, prefix=prefix, recursive=True):
            if self.delete(obj.object_name):
                n += 1
        return n
