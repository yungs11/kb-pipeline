"""parse-svc MinIO 클라이언트 — 페이지 이미지 업로드(+키 조립).

knowledge_base ``clients/minio_client.py`` 미러(spec §5.1.1). parse-svc 가 PDF/이미지
페이지를 JPEG 로 래스터화해 dify 와 **동일 키 스킴**으로 MinIO 에 올린다 → 기존
knowledge_base UI(`/obj/{key}` same-origin 프록시)·검색 인용이 그대로 동작한다.

키 규칙(잠금, spec §3 D-키 규칙):
  * 버킷            ``document-parser``
  * 페이지 이미지   ``{docs_id}/{page_uuid}.jpeg`` (``page_uuid == "{docs_id}_{page_number}"``)

환경변수(spec §5.1.1):
  * ``MINIO_ENDPOINT``   (기본 ``localhost:9000``)
  * ``MINIO_ACCESS_KEY``
  * ``MINIO_SECRET_KEY``
  * ``MINIO_BUCKET``     (기본 ``document-parser``)
  * ``MINIO_SECURE``     (기본 ``false``)

``minio.Minio`` 는 ``from_settings``/``from_env`` 안에서 **lazy import**(테스트는 fake
클라이언트를 주입하므로 minio 패키지 없이도 모듈 import 가능).
"""

from __future__ import annotations

import io
import logging
import os
from typing import Any

log = logging.getLogger("kb_pipeline.parse_service.minio")

DEFAULT_BUCKET = "document-parser"
DEFAULT_ENDPOINT = "localhost:9000"


def _env_bool(value: str | None, *, default: bool = False) -> bool:
    """``MINIO_SECURE`` 같은 truthy 문자열 파싱(기본 false)."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class MinioStore:
    """minio 작업 래퍼(페이지 이미지 업로드 + 키 조립)."""

    def __init__(self, client: Any, *, bucket: str = DEFAULT_BUCKET) -> None:
        self._client = client
        self._bucket = bucket

    @property
    def bucket(self) -> str:
        return self._bucket

    @classmethod
    def from_settings(
        cls,
        endpoint: str,
        *,
        access_key: str,
        secret_key: str,
        secure: bool = False,
        bucket: str = DEFAULT_BUCKET,
    ) -> "MinioStore":
        """설정으로 실제 Minio 클라이언트를 생성한다."""
        from minio import Minio

        client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        return cls(client, bucket=bucket)

    @classmethod
    def from_env(cls) -> "MinioStore":
        """``MINIO_*`` 환경변수로 클라이언트를 생성한다(spec §5.1.1 기본값)."""
        return cls.from_settings(
            os.environ.get("MINIO_ENDPOINT", DEFAULT_ENDPOINT),
            access_key=os.environ.get("MINIO_ACCESS_KEY", ""),
            secret_key=os.environ.get("MINIO_SECRET_KEY", ""),
            secure=_env_bool(os.environ.get("MINIO_SECURE"), default=False),
            bucket=os.environ.get("MINIO_BUCKET", DEFAULT_BUCKET),
        )

    @staticmethod
    def page_image_object_key(docs_id: str, page_uuid: str) -> str:
        """페이지 이미지 객체 키 — ``{docs_id}/{page_uuid}.jpeg``."""
        return f"{docs_id}/{page_uuid}.jpeg"

    def _ensure_bucket(self) -> None:
        """버킷이 없으면 생성한다(best-effort; 권한/경합 예외는 호출자가 처리)."""
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)

    def put_page_image(self, docs_id: str, page_uuid: str, jpeg_bytes: bytes) -> str | None:
        """페이지 이미지(JPEG)를 dify 와 동일 키 스킴(``{docs_id}/{page_uuid}.jpeg``)으로 업로드.

        버킷이 없으면 생성한다. 콘텐츠타입은 ``image/jpeg`` 고정(챗 인용·``/obj`` 프록시가
        그대로 동작). **개별 페이지 업로드 실패는 비치명**(로그 후 ``None`` 반환) — 적재
        전체를 실패시키지 않고 그 페이지의 썸네일만 누락된다(spec §5.1.1).
        """
        key = self.page_image_object_key(docs_id, page_uuid)
        try:
            # 버킷은 인프라가 미리 만든다(dify/edgequake 와 공유: ``document-parser``).
            # ``bucket_exists``/``make_bucket`` 은 버킷-관리 권한을 요구해 제한된 업로드
            # 전용 자격증명에서 AccessDenied → 호출하지 않고 곧장 put_object 한다
            # (knowledge_base ``clients/minio_client.py`` 와 동일).
            self._client.put_object(
                self._bucket,
                key,
                io.BytesIO(jpeg_bytes),
                length=len(jpeg_bytes),
                content_type="image/jpeg",
            )
        except Exception:  # noqa: BLE001 - 개별 페이지 업로드 실패는 비치명(로그 후 계속).
            log.exception("put_page_image failed for %s", key)
            return None
        return key
