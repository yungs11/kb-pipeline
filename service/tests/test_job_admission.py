"""plan_admissions() — 유량제어의 핵심 판정. DB 없이 도는 순수 함수.

설계 §3.2/§3.5/§3.6. 여기서 검증하는 계약:
  * kind 버킷 상한 / workspace 상한 / 로컬 슬롯을 **동시에** 만족해야 승인
  * 승인할 때마다 카운터를 즉시 갱신한다(스냅샷 판정 금지 — 안 그러면 한 틱에 후보
    전체가 승인되어 상한이 무너진다)
  * head-of-line blocking 회피: 한 후보가 막혀도 뒤 후보 검사를 계속한다
  * ingest 는 parse·chunk·insert 세 버킷을 **동시 점유**한다
  * workspace_key 가 None 이면 workspace 상한을 적용하지 않는다
  * 알 수 없는 kind 는 승인하지 않는다
"""
from __future__ import annotations

import pytest

from service.jobs.admission import BUCKETS_FOR_KIND, Candidate, expand_running_buckets, plan_admissions

LIMITS = {"parse": 4, "chunk": 2, "insert": 2}


def _c(job_id: str, kind: str, ws: str | None = None) -> Candidate:
    return Candidate(id=job_id, kind=kind, workspace_key=ws)


def _plan(candidates, *, running_by_kind=None, bucket_limits=None,
          workspace_limit=2, local_free=100):
    """(2) 집계 → 버킷 전개 → 승인. 실제 claim 경로와 같은 순서로 부른다."""
    running_by_kind = running_by_kind or {}
    buckets, by_ws = expand_running_buckets(running_by_kind)
    return plan_admissions(
        candidates,
        running_by_bucket=buckets,
        running_by_workspace=by_ws,
        bucket_limits=bucket_limits or LIMITS,
        workspace_limit=workspace_limit,
        local_free=local_free,
    )


# ── 버킷 상한 ──────────────────────────────────────────────────────────────

def test_bucket_limit_caps_admissions():
    """후보 10건·parse 상한 4 → 정확히 4건. 카운터를 갱신하지 않으면 10건이 승인된다."""
    candidates = [_c(f"p{i}", "parse") for i in range(10)]
    assert len(_plan(candidates)) == 4


def test_bucket_limit_accounts_for_already_running():
    """이미 3건 running 이면 상한 4 에서 1건만 더 승인한다."""
    candidates = [_c(f"p{i}", "parse") for i in range(5)]
    assert _plan(candidates, running_by_kind={("parse", None): 3}) == ["p0"]


def test_full_bucket_admits_nothing():
    candidates = [_c("p0", "parse")]
    assert _plan(candidates, running_by_kind={("parse", None): 4}) == []


# ── head-of-line blocking 회피 ─────────────────────────────────────────────

def test_blocked_kind_does_not_block_other_kinds():
    """앞선 후보가 전부 chunk 로 막혀도 같은 틱에 parse 가 승인된다 (설계 §3.6).

    v3 검증에서 나온 실제 결함: 후보 상위 N건이 전부 chunk 이고 chunk 버킷이 차 있으면
    parse 슬롯 4개가 비어 있는데도 그 틱에 아무것도 승인되지 않았다.
    """
    candidates = [_c("c0", "chunk"), _c("c1", "chunk"), _c("c2", "chunk"),
                  _c("p0", "parse")]
    admitted = _plan(candidates, running_by_kind={("chunk", None): 2})
    assert admitted == ["p0"]


def test_fifo_order_within_available_capacity():
    candidates = [_c("p0", "parse"), _c("p1", "parse"), _c("p2", "parse")]
    assert _plan(candidates, bucket_limits={"parse": 2}) == ["p0", "p1"]


# ── workspace 상한 ─────────────────────────────────────────────────────────

def test_workspace_limit_applies_per_tenant():
    """한 KB 가 큐를 독점하지 못한다 — ws-a 는 2건까지, ws-b 는 별도 예산."""
    candidates = [_c("a0", "parse", "ws-a"), _c("a1", "parse", "ws-a"),
                  _c("a2", "parse", "ws-a"), _c("b0", "parse", "ws-b")]
    assert _plan(candidates, workspace_limit=2) == ["a0", "a1", "b0"]


def test_null_workspace_is_exempt_from_workspace_limit():
    """workspace_key=None 이면 workspace 상한 미적용 (설계 §3.4).

    현행 /parse·/chunk 에는 workspace 개념이 없어 kb 트래픽 대부분이 None 이다.
    상한을 적용하면 per-workspace 2 가 사실상 전역 상한이 되어 **현행보다 처리량이
    나빠진다** — 유량제어가 아니라 처리량 파괴다.
    """
    candidates = [_c(f"p{i}", "parse") for i in range(4)]
    assert len(_plan(candidates, workspace_limit=2)) == 4  # parse 상한 4 까지 간다


def test_null_and_named_workspaces_are_independent():
    candidates = [_c("n0", "parse"), _c("n1", "parse"),
                  _c("a0", "parse", "ws-a"), _c("a1", "parse", "ws-a")]
    assert _plan(candidates, workspace_limit=1) == ["n0", "n1", "a0"]


# ── ingest 3버킷 동시 점유 ─────────────────────────────────────────────────

def test_ingest_reserves_all_three_buckets():
    assert BUCKETS_FOR_KIND["ingest"] == ("parse", "chunk", "insert")


def test_ingest_blocked_when_any_bucket_full():
    """insert 버킷만 차 있어도 ingest 는 승인되지 않는다 (세 버킷 모두 필요)."""
    candidates = [_c("g0", "ingest", "ws-a")]
    assert _plan(candidates, running_by_kind={("insert", "ws-x"): 2}) == []


def test_running_ingest_occupies_chunk_bucket():
    """running ingest 2건이면 신규 chunk 잡이 승인되지 않는다.

    (2) 집계는 kind 로 오는데 이를 버킷으로 전개하지 않으면 running ingest 가 어느
    버킷도 점유하지 않아(상한표에 'ingest' 항목이 없다) 과승인이 난다 — 설계 §3.5 가
    막으려던 바로 그 결함.
    """
    candidates = [_c("c0", "chunk", "ws-b")]
    assert _plan(candidates, running_by_kind={("ingest", "ws-a"): 2}) == []


def test_concurrent_ingest_capped_by_narrowest_bucket():
    """동시 ingest 상한 = min(4, 2, 2) = 2 (설계 §3.5 의 수용된 귀결)."""
    candidates = [_c(f"g{i}", "ingest", f"ws-{i}") for i in range(5)]
    assert _plan(candidates) == ["g0", "g1"]


# ── 로컬 슬롯 ──────────────────────────────────────────────────────────────

def test_local_free_caps_admissions():
    """worker 프로세스의 로컬 스레드 슬롯이 전역 상한보다 작으면 그쪽이 이긴다."""
    candidates = [_c(f"p{i}", "parse") for i in range(4)]
    assert _plan(candidates, local_free=2) == ["p0", "p1"]


def test_zero_local_free_admits_nothing():
    assert _plan([_c("p0", "parse")], local_free=0) == []


# ── 방어 ───────────────────────────────────────────────────────────────────

def test_unknown_kind_is_never_admitted():
    """알 수 없는 kind 를 무제한 승인하면 상한이 통째로 우회된다."""
    assert _plan([_c("x0", "bogus"), _c("p0", "parse")]) == ["p0"]


def test_empty_candidates():
    assert _plan([]) == []


@pytest.mark.parametrize("kind", ["parse", "chunk", "insert", "ingest"])
def test_every_kind_has_buckets(kind):
    assert BUCKETS_FOR_KIND[kind]
