"""`parse-staging/` 스윕 — kb 가 남긴 임시 원본을 나이로만 수거한다.

설계 근거: ``docs/superpowers/specs/2026-08-03-facade-job-queue-deferred.md`` D20.

**잡 큐 GC(`gc.py`)와 다른 프리픽스, 다른 판정이다.**

| | `kbp-jobs/` (gc.py) | `parse-staging/` (여기) |
|---|---|---|
| 소유자 | facade (`kbp.jobs` 행이 근거) | **kb** (documents·batch 테이블이 수명을 안다) |
| 판정 | 행 참조 + 나이 (fail-closed) | **나이만** |

facade 는 `parse-staging/` 객체를 누가 아직 쓰는지 알 수 없다 — 참조가 kb DB 에 있다.
그래서 "행이 없으면 고아" 라는 gc.py 의 판정을 여기 쓸 수 없고, 순수 TTL 로만 지운다.
그 대신 TTL 을 **미리보기 세션 수명보다 넉넉하게** 잡는다.

실측 배경(2026-08-05): 이 프리픽스에 323건 214.7MB 가 한 달간 쌓여 있었다. 95% 가
미리보기만 하고 적재를 안 누른 이탈분이다. kb 도 함께 고쳤지만(적재 시 4종 정리,
배치 성공 시 정리) 이탈 세션은 kb 가 도달하는 코드가 없어 이 스윕이 유일한 수거 수단이다.

**두 갈래를 다른 TTL 로 본다.**

* `parse-staging/batch/…` — 배치 업로드 원본. 실패·게이트차단 항목의 재수행
  (`POST /batches/{id}/items/{id}/retry`)이 이 객체를 **그대로 다시 쓴다**. 짧게 잡으면
  재수행이 409 로 죽는다. 기본 7일.
* 그 외(`{parse_session}/original|sidecar|chunk_preview|preview_latest`) — 파싱
  미리보기 세션. 사용자가 파싱 결과를 보고 적재를 누르기까지의 수명이라 짧다. 기본 1시간.

TTL 이 0 이하면 그 갈래는 **정지**한다(전량 삭제가 아니다) — `gc.py` 와 같은 규약이다.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

log = logging.getLogger("kb_pipeline.service.jobs.staging_gc")

#: kb `MinioBlobStore` 가 쓰는 프리픽스. facade `/objects/*` 의 `staging` scope 와 같다.
DEFAULT_PREFIX = "parse-staging"


def staging_prefix() -> str:
    """staging 프리픽스 — **쓰는 쪽(`/objects`)과 지우는 쪽(스윕)이 같은 값을 봐야 한다.**

    어긋나면 격리는 안 되고 스윕만 헛돈다. 그래서 두 곳이 이 함수 하나를 부른다.

    같은 MinIO 버킷을 **여러 배포가 공유**하면 배포별로 다르게 잡아야 한다(D16) —
    스윕은 "내 DB 에 행이 없으면 고아" 로 판정하므로, 프리픽스가 같으면 남의 살아있는
    staging 을 지운다. 예: `KBP_STAGING_PREFIX=parse-staging-prod`.
    """
    return (os.environ.get("KBP_STAGING_PREFIX") or DEFAULT_PREFIX).strip("/")

#: 배치 업로드 갈래. 재수행이 참조하므로 TTL 을 따로 준다.
_BATCH_SEGMENT = "batch/"

#: 미리보기 세션이 만드는 leaf 이름. 이것만 우리가 아는 형식으로 인정한다 —
#: 모르는 키를 나이만 보고 지우면 나중에 kb 가 추가한 다른 용도의 객체를 말없이 날린다.
_PREVIEW_LEAVES = frozenset({"original", "sidecar", "chunk_preview", "preview_latest"})

#: `{32-hex 세션}/{leaf}` — kb `routers/kb.py` 가 `uuid.uuid4().hex` 로 만든다.
_PREVIEW_RE = re.compile(r"^[0-9a-fA-F]{32}/(" + "|".join(sorted(_PREVIEW_LEAVES)) + r")$")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        log.warning("%s 파싱 실패 — 기본값 %s 사용", name, default)
        return default


@dataclass
class StagingGcConfig:
    #: 미리보기 세션 TTL. 기본 1시간 — 파싱 결과를 보고 적재를 누르기까지의 수명.
    preview_ttl_seconds: int = field(
        default_factory=lambda: _int("KBP_STAGING_TTL_SECONDS", 3600))
    #: 배치 원본 TTL. 기본 7일 — 실패 항목 재수행 창을 지킨다.
    batch_ttl_seconds: int = field(
        default_factory=lambda: _int("KBP_STAGING_BATCH_TTL_SECONDS", 7 * 24 * 3600))
    #: 한 사이클 최대 삭제 수. 첫 사이클에 214MB 를 한 번에 지우지 않게 나눠 돈다.
    batch: int = field(default_factory=lambda: _int("KBP_STAGING_GC_BATCH", 500))


@dataclass
class StagingSweepResult:
    listed: int = 0
    expired: int = 0
    deleted: int = 0
    skipped_unknown: int = 0
    aborted: str | None = None


class StagingStore:
    """`parse-staging/` 나열·삭제. MinIO 접근을 여기 가둔다."""

    def __init__(self, client: Any, *, bucket: str, prefix: str = DEFAULT_PREFIX) -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    @classmethod
    def from_env(cls) -> "StagingStore":
        from minio import Minio

        secure = (os.environ.get("MINIO_SECURE", "") or "").lower() in {"1", "true", "yes", "on"}
        client = Minio(
            os.environ.get("MINIO_ENDPOINT", "localhost:9000"),
            access_key=os.environ.get("MINIO_ACCESS_KEY", ""),
            secret_key=os.environ.get("MINIO_SECRET_KEY", ""),
            secure=secure,
        )
        return cls(
            client,
            bucket=os.environ.get("MINIO_BUCKET", "document-parser"),
            prefix=staging_prefix(),
        )

    def iter_objects(self) -> Iterator[tuple[str, str, dt.datetime | None]]:
        """``(프리픽스 뗀 상대키, 전체키, last_modified)``.

        ``recursive=True`` **고정** — 기본값이면 common-prefix 유사객체가 나오고 그
        ``last_modified`` 가 ``None`` 이라 나이 비교가 무너진다(`blobs.py` 와 같은 함정).
        """
        base = f"{self._prefix}/"
        for obj in self._client.list_objects(self._bucket, prefix=base, recursive=True):
            key = obj.object_name
            if not key.startswith(base):
                continue
            yield (key[len(base):], key, getattr(obj, "last_modified", None))

    def delete(self, key: str) -> bool:
        try:
            self._client.remove_object(self._bucket, key)
            return True
        except Exception:  # noqa: BLE001 - 개별 삭제 실패는 비치명
            log.warning("staging 객체 삭제 실패 %r", key, exc_info=True)
            return False


def classify(rel_key: str) -> str | None:
    """상대키 → ``"batch"`` | ``"preview"`` | ``None``(모르는 형식).

    모르는 형식은 **건드리지 않는다**. 같은 프리픽스에 나중에 다른 용도가 생겨도
    나이만 보고 남의 객체를 지우지 않게 하는 안전장치다.
    """
    if rel_key.startswith(_BATCH_SEGMENT):
        return "batch"
    if _PREVIEW_RE.match(rel_key):
        return "preview"
    return None


def run_staging_sweep(
    store: StagingStore,
    *,
    cfg: StagingGcConfig | None = None,
    now: dt.datetime | None = None,
) -> StagingSweepResult:
    """TTL 경과 staging 객체를 지운다."""
    cfg = cfg or StagingGcConfig()
    now = now or dt.datetime.now(dt.timezone.utc)
    res = StagingSweepResult()

    ttls = {"preview": cfg.preview_ttl_seconds, "batch": cfg.batch_ttl_seconds}
    if all(v <= 0 for v in ttls.values()):
        res.aborted = "staging gc disabled"
        return res

    try:
        objects = list(store.iter_objects())
    except Exception:  # noqa: BLE001 - 나열 실패면 아무것도 지우지 않는다
        log.exception("staging 나열 실패 — 이번 사이클 중단")
        res.aborted = "list failed"
        return res

    victims: list[str] = []
    for rel, key, last_modified in objects:
        res.listed += 1
        kind = classify(rel)
        if kind is None:
            res.skipped_unknown += 1
            continue
        ttl = ttls[kind]
        if ttl <= 0:
            continue                        # 그 갈래만 정지
        if last_modified is None or last_modified.tzinfo is None:
            # 나이를 모르면 지우지 않는다. None 을 "오래됨" 으로 흡수하면 방금 올린
            # 원본을 지워 진행 중인 적재가 깨진다.
            continue
        if (now - last_modified).total_seconds() > ttl:
            res.expired += 1
            victims.append(key)

    for key in victims[: cfg.batch]:
        if store.delete(key):
            res.deleted += 1

    if res.deleted or res.expired:
        log.info(
            "staging sweep: listed=%d expired=%d deleted=%d unknown=%d "
            "(preview_ttl=%ds batch_ttl=%ds)",
            res.listed, res.expired, res.deleted, res.skipped_unknown,
            cfg.preview_ttl_seconds, cfg.batch_ttl_seconds,
        )
    return res
