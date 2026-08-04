"""잡 객체 저장소 — 업로드 staging + 큰 payload/result 오프로딩.

설계 §2.2/§7.5.

MinIO 는 이미 스택에 있다(페이지 이미지용). 같은 버킷(``document-parser``)을 재사용하고
``{prefix}/{job_id}/...`` 키로 격리한다.

``make_bucket`` 은 **호출하지 않는다** — 제한된 업로드 전용 자격증명에서 ``AccessDenied``
가 난다(``parse_service/minio_client.py`` 와 동일 근거). 대신 기동 시 존재 확인만 한다.
"""
from __future__ import annotations

import io
import json
import logging
import os
import uuid
from typing import Any

log = logging.getLogger("kb_pipeline.service.jobs.blobs")

DEFAULT_BUCKET = "document-parser"
DEFAULT_PREFIX = "kbp-jobs"
DEFAULT_ENDPOINT = "localhost:9000"

#: 이 크기 이하는 jsonb 인라인, 초과하면 MinIO 로 뺀다(§2.2).
DEFAULT_INLINE_MAX_BYTES = 262144


def _env_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def inline_max_bytes() -> int:
    try:
        return int(os.environ.get("KBP_JOB_INLINE_MAX_BYTES", "") or DEFAULT_INLINE_MAX_BYTES)
    except ValueError:
        return DEFAULT_INLINE_MAX_BYTES


def max_upload_bytes() -> int:
    """업로드 상한. 스트리밍을 안 하므로(D3) 이 값이 최악 메모리를 결정한다(§7.5)."""
    try:
        return int(os.environ.get("KBP_JOB_MAX_UPLOAD_BYTES", "") or 52428800)
    except ValueError:
        return 52428800


class JobBlobStore:
    """MinIO 래퍼. 클라이언트를 주입할 수 있어 테스트가 fake 로 돈다."""

    def __init__(self, client: Any, *, bucket: str = DEFAULT_BUCKET,
                 prefix: str = DEFAULT_PREFIX) -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    @classmethod
    def from_env(cls) -> "JobBlobStore":
        from minio import Minio  # lazy — minio 없이도 모듈 import 가능

        client = Minio(
            os.environ.get("MINIO_ENDPOINT", DEFAULT_ENDPOINT),
            access_key=os.environ.get("MINIO_ACCESS_KEY", ""),
            secret_key=os.environ.get("MINIO_SECRET_KEY", ""),
            secure=_env_bool(os.environ.get("MINIO_SECURE"), default=False),
        )
        return cls(
            client,
            bucket=os.environ.get("KBP_JOB_MINIO_BUCKET")
            or os.environ.get("MINIO_BUCKET", DEFAULT_BUCKET),
            prefix=os.environ.get("KBP_JOB_MINIO_PREFIX", DEFAULT_PREFIX),
        )

    # ── 키 ─────────────────────────────────────────────────────────────────

    def key(self, job_id: uuid.UUID | str, name: str) -> str:
        return f"{self._prefix}/{job_id}/{name}"

    # ── 기동 확인 ──────────────────────────────────────────────────────────

    def check_bucket(self) -> bool:
        """버킷 도달 가능 여부를 **실제 요청 1회**로 확인한다.

        ``list_objects`` 는 지연 제너레이터라 호출만 하면 HTTP 요청이 나가지 않는다 —
        1건을 순회해야 한다. ``max_keys`` 파라미터는 minio-py 에 **없다**.

        이 설계에서 버킷 부재의 심각도가 올라간다. 현행에서는 페이지 썸네일만 누락되고
        파싱은 성공하지만, 잡 방식에서는 staging put 실패 = ``/parse``·``/ingest`` 접수
        전면 실패다. 그래도 **WARN 으로만** 남긴다 — 첫 배포에서는 버킷이 비어 있는 게
        정상이라 ERROR 면 오탐이고, 실제 실패는 첫 put 에서 명확히 드러난다.
        """
        try:
            next(iter(self._client.list_objects(
                self._bucket, prefix=f"{self._prefix}/", recursive=True)), None)
            return True
        except Exception as exc:  # noqa: BLE001 - 기동을 막지 않는다
            log.warning(
                "job blob bucket %r not reachable at startup (%s); "
                "uploads will fail until it exists", self._bucket, exc,
            )
            return False

    # ── 바이트 ─────────────────────────────────────────────────────────────

    def put_bytes(self, key: str, data: bytes, *, content_type: str) -> str:
        self._client.put_object(
            self._bucket, key, io.BytesIO(data), length=len(data),
            content_type=content_type,
        )
        return key

    def get_bytes(self, key: str) -> bytes:
        resp = self._client.get_object(self._bucket, key)
        try:
            return resp.read()
        finally:
            close = getattr(resp, "close", None)
            release = getattr(resp, "release_conn", None)
            if close:
                close()
            if release:
                release()

    def delete(self, key: str | None) -> None:
        """객체 삭제. 실패는 비치명(로그만) — 적재 성공을 되돌리지 않는다."""
        if not key:
            return
        try:
            self._client.remove_object(self._bucket, key)
        except Exception:  # noqa: BLE001
            log.warning("failed to remove job object %r", key, exc_info=True)

    # ── JSON (인라인 / 오프로딩) ───────────────────────────────────────────

    def store_json(
        self, job_id: uuid.UUID | str, name: str, obj: Any
    ) -> tuple[dict[str, Any] | None, str | None]:
        """``(inline, ref)`` 중 하나만 채워 돌려준다(§2.2).

        임계 이하면 jsonb 로 그대로 넣고, 초과하면 MinIO 로 뺀다. parse 결과는 대개
        후자다 — ``enriched_content`` 하나로도 임계를 넘는다.
        """
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if len(raw) <= inline_max_bytes():
            return (obj, None)
        key = self.key(job_id, f"{name}.json")
        self.put_bytes(key, raw, content_type="application/json")
        return (None, key)

    def load_json(
        self, inline: dict[str, Any] | None, ref: str | None
    ) -> dict[str, Any] | None:
        """``*_ref`` 가 있으면 **반드시** MinIO 에서 읽어 복원한다(§2.2 불변식).

        복원 실패를 빈 본문으로 갈음하면 안 된다. kb 클라이언트가
        ``body.get("enriched_content") or ""`` 로 조용히 흡수해서, §0.2 가 202 전환을
        포기하며 막으려던 '빈 문서 청킹·적재' 가 그대로 재현된다.

        :raises Exception: 복원 실패(호출자가 500 으로 올린다).
        """
        if ref:
            return json.loads(self.get_bytes(ref).decode("utf-8"))
        return inline
