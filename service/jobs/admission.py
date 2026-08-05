"""승인 판정 — 유량제어가 실제로 일어나는 곳. DB 를 모른다(순수 함수).

설계 §3.2/§3.4/§3.5/§3.6.

claim 트랜잭션은 advisory lock 으로 직렬화되므로(§3.1) 이 함수가 보는 카운터는
그 순간의 권위 있는 값이다. 여기서 하는 일은 "누구를 running 으로 올릴지" 하나뿐이고,
DB 접근·HTTP·시간에 의존하지 않아 테스트가 전부 순수 단위테스트로 끝난다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Mapping

#: kind → 점유하는 버킷 집합.
#:
#: ingest 는 parse-svc·adaptive_chunk·edgequake 를 **모두** 호출하므로(``app.py`` 의
#: /ingest 오케스트레이션) 세 버킷을 동시에 예약한다. parse 버킷만 잡으면
#: KBP_JOB_LIMIT_INSERT("임베딩 서버 처리량 종속")의 근거가 무너진다 — ingest 4건 +
#: chunk 2건이면 adaptive 동시 6, edgequake 동시 6 이 된다.
#:
#: claim 시점에 한꺼번에 예약하고 종료 시 함께 푼다. 실행 중 중첩 취득이 아니므로
#: 데드락이 없다. 귀결(수용): 동시 ingest 상한 = min(버킷 상한) = 2.
BUCKETS_FOR_KIND: dict[str, tuple[str, ...]] = {
    "parse": ("parse",),
    "chunk": ("chunk",),
    "insert": ("insert",),
    "ingest": ("parse", "chunk", "insert"),
    # 커뮤니티 재빌드(Louvain + LLM 요약). 자기 버킷을 쓴다 — 적재 버킷을 나눠 쓰면
    # 무거운 빌드 하나가 parse 슬롯을 먹어 업로드가 밀린다.
    "community": ("community",),
}

#: workspace 상한을 적용하지 않는 키(설계 §3.4). 현행 /parse·/chunk 에는 workspace
#: 개념이 없어 kb 트래픽 대부분이 여기 해당한다. 한 버킷에 몰아 상한을 걸면
#: per-workspace 2 가 사실상 전역 상한이 되어 현행보다 처리량이 나빠진다.
_EXEMPT_WORKSPACE = None


@dataclass(frozen=True)
class Candidate:
    """claim (3) 후보 조회가 돌려주는 최소 정보."""

    id: str
    kind: str
    workspace_key: str | None


def expand_running_buckets(
    running_by_kind: Mapping[tuple[str, str | None], int],
) -> tuple[dict[str, int], dict[str, int]]:
    """claim (2) 의 ``{(kind, workspace_key): count}`` 집계를 판정용 카운터로 전개한다.

    **kind 를 그대로 버킷 키로 쓰면 안 된다** — 버킷 상한표에 ``ingest`` 항목이 없어서
    running ingest 가 어느 버킷도 점유하지 않게 되고, ``BUCKETS_FOR_KIND`` 가 막으려던
    과승인이 그대로 재현된다. running ingest 1건은 parse·chunk·insert 각 1을 점유한다.

    :returns: ``(버킷별 점유 수, workspace 별 점유 수)``. workspace 카운터는 잡 단위라
        ingest 라도 1 로 센다(자원이 아니라 테넌트 공정성 축이기 때문).
    """
    by_bucket: dict[str, int] = {}
    by_workspace: dict[str, int] = {}
    for (kind, workspace_key), count in running_by_kind.items():
        for bucket in BUCKETS_FOR_KIND.get(kind, ()):
            by_bucket[bucket] = by_bucket.get(bucket, 0) + count
        if workspace_key is not _EXEMPT_WORKSPACE:
            by_workspace[workspace_key] = by_workspace.get(workspace_key, 0) + count
    return by_bucket, by_workspace


def plan_admissions(
    candidates: Iterable[Candidate],
    *,
    running_by_bucket: Mapping[str, int],
    running_by_workspace: Mapping[str, int],
    bucket_limits: Mapping[str, int],
    workspace_limit: int,
    local_free: int,
) -> list[str]:
    """FIFO 순서로 훑으며 버킷·workspace·로컬 슬롯이 모두 허용하는 잡만 승인한다.

    승인할 때마다 세 카운터를 **즉시 갱신**하고 이후 후보는 갱신된 값으로 판정한다.
    스냅샷만 보고 판정하면 한 틱에 후보 전체가 승인되어 상한이 통째로 무너진다.

    한 후보가 막혀도 뒤 후보 검사를 계속한다(head-of-line blocking 회피). 그래서
    chunk 버킷이 꽉 차 있어도 뒤에 있는 parse 잡이 같은 틱에 승인된다.

    ``workspace_key`` 가 ``None`` 이면 workspace 상한을 적용하지 않는다(§3.4).
    ``BUCKETS_FOR_KIND`` 에 없는 kind 는 **승인하지 않는다** — 무제한 승인은 상한을
    우회하는 구멍이라, 모르는 것은 막는 쪽이 안전하다.

    :returns: 승인된 job id 목록(입력 순서 유지). 호출자는 이 목록으로 조건부 UPDATE 를
        수행하고 그 ``RETURNING`` 집합만 실제 실행한다(§3.1 (5)).
    """
    bucket_used = dict(running_by_bucket)
    workspace_used = dict(running_by_workspace)
    free = local_free
    admitted: list[str] = []

    for cand in candidates:
        if free <= 0:
            break
        buckets = BUCKETS_FOR_KIND.get(cand.kind)
        if not buckets:
            continue  # 알 수 없는 kind — 승인하지 않는다
        if any(
            bucket_used.get(b, 0) >= bucket_limits.get(b, 0) for b in buckets
        ):
            continue
        ws = cand.workspace_key
        if ws is not _EXEMPT_WORKSPACE and workspace_used.get(ws, 0) >= workspace_limit:
            continue

        for b in buckets:
            bucket_used[b] = bucket_used.get(b, 0) + 1
        if ws is not _EXEMPT_WORKSPACE:
            workspace_used[ws] = workspace_used.get(ws, 0) + 1
        free -= 1
        admitted.append(cand.id)

    return admitted


# ── 설정 로딩 ──────────────────────────────────────────────────────────────
#
# 기본값 근거는 설계 §3.7. 요약하면 각 버킷 상한은 그 구간의 실제 다운스트림 병목에
# 맞춘다 — parse=4 는 parse-svc 의 `gunicorn -w 4`, chunk=2 는 adaptive 의 4방법 경쟁
# 비용, insert=2 는 임베딩 서버 처리량.


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def bucket_limits_from_env() -> dict[str, int]:
    return {
        "parse": _env_int("KBP_JOB_LIMIT_PARSE", 4),
        "chunk": _env_int("KBP_JOB_LIMIT_CHUNK", 2),
        "insert": _env_int("KBP_JOB_LIMIT_INSERT", 2),
        # 전역 단일 그래프라 동시에 여러 개를 돌릴 이유가 없다. 같은 workspace 를 두 번
        # 빌드하면 뒤엣것이 앞엣것의 결과를 덮을 뿐이다.
        "community": _env_int("KBP_JOB_LIMIT_COMMUNITY", 1),
    }


def workspace_limit_from_env() -> int:
    return _env_int("KBP_JOB_LIMIT_PER_WORKSPACE", 2)
