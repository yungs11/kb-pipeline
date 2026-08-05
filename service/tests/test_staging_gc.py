"""`parse-staging/` 스윕 — kb 가 남긴 임시본을 나이로만 수거한다 (D20).

지키는 계약:
  * **모르는 키는 건드리지 않는다** — 나이만 보고 남의 객체를 지우면 안 된다
  * 배치 원본은 **재수행 창**(기본 7일)을 지킨다 — 짧으면 retry 가 409 로 죽는다
  * 미리보기는 짧게(기본 1시간) — 적재를 안 누르고 이탈한 세션이 여기서만 수거된다
  * `last_modified` 가 없거나 naive 면 삭제하지 않는다
  * TTL<=0 은 전량 삭제가 아니라 **정지**
  * 나열 실패는 삭제 0건
"""
from __future__ import annotations

import datetime as dt

import pytest

from service.jobs.staging_gc import (
    StagingGcConfig,
    StagingSweepResult,
    classify,
    run_staging_sweep,
)

NOW = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.timezone.utc)
SESSION = "0123456789abcdef0123456789abcdef"


class FakeStore:
    def __init__(self, objects=()):
        # objects: [(rel_key, age_seconds)] 또는 [(rel_key, age, tz)]
        self.objects = list(objects)
        self.deleted: list[str] = []
        self.list_fails = False

    def iter_objects(self):
        if self.list_fails:
            raise RuntimeError("minio down")
        for item in self.objects:
            rel, age = item[0], item[1]
            tz = item[2] if len(item) > 2 else True
            if age is None:
                lm = None
            else:
                lm = NOW - dt.timedelta(seconds=age)
                if not tz:
                    lm = lm.replace(tzinfo=None)
            yield (rel, f"parse-staging/{rel}", lm)

    def delete(self, key):
        self.deleted.append(key)
        return True


def _sweep(store, **kw) -> StagingSweepResult:
    return run_staging_sweep(store, cfg=StagingGcConfig(**kw), now=NOW)


# ── 분류 ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rel,expected", [
    (f"{SESSION}/original", "preview"),
    (f"{SESSION}/sidecar", "preview"),
    (f"{SESSION}/chunk_preview", "preview"),
    (f"{SESSION}/preview_latest", "preview"),
    ("batch/b-1/i-1/original/문서 (1).pdf", "batch"),
    (f"{SESSION}/unknown_leaf", None),        # 모르는 leaf
    ("somedoc/page_1.jpeg", None),            # 남의 키 형식
    ("original", None),                       # 세션 없는 평평한 키
])
def test_classify(rel, expected):
    assert classify(rel) == expected


def test_unknown_keys_are_never_deleted():
    """나중에 kb 가 같은 프리픽스에 다른 용도를 만들어도 말없이 날리면 안 된다."""
    s = FakeStore([(f"{SESSION}/mystery", 99999), ("somedoc/page_1.jpeg", 99999)])
    res = _sweep(s)
    assert res.deleted == 0 and s.deleted == []
    assert res.skipped_unknown == 2


# ── 미리보기 TTL ───────────────────────────────────────────────────────────

def test_expired_preview_is_deleted():
    s = FakeStore([(f"{SESSION}/original", 7200)])
    assert _sweep(s, preview_ttl_seconds=3600).deleted == 1


def test_fresh_preview_survives():
    """진행 중인 미리보기 세션을 지우면 사용자가 적재를 누를 때 404 가 난다."""
    s = FakeStore([(f"{SESSION}/original", 600)])
    assert _sweep(s, preview_ttl_seconds=3600).deleted == 0


def test_all_four_preview_leaves_are_swept():
    s = FakeStore([(f"{SESSION}/{leaf}", 7200)
                   for leaf in ("original", "sidecar", "chunk_preview", "preview_latest")])
    assert _sweep(s, preview_ttl_seconds=3600).deleted == 4


# ── 배치 TTL — 재수행 창 ───────────────────────────────────────────────────

def test_batch_original_survives_the_preview_ttl():
    """배치 원본에 1시간을 적용하면 실패 항목 재수행이 통째로 죽는다.

    `routers/batches.py` 의 retry 는 이 객체를 그대로 다시 쓰고, 없으면 409 로 거절한다.
    """
    s = FakeStore([("batch/b-1/i-1/original/a.pdf", 7200)])
    res = _sweep(s, preview_ttl_seconds=3600, batch_ttl_seconds=7 * 24 * 3600)
    assert res.deleted == 0


def test_batch_original_expires_on_its_own_ttl():
    s = FakeStore([("batch/b-1/i-1/original/a.pdf", 8 * 24 * 3600)])
    assert _sweep(s, batch_ttl_seconds=7 * 24 * 3600).deleted == 1


def test_two_lanes_use_independent_ttls():
    s = FakeStore([(f"{SESSION}/original", 7200),               # preview: 만료
                   ("batch/b-1/i-1/original/a.pdf", 7200)])     # batch: 생존
    res = _sweep(s, preview_ttl_seconds=3600, batch_ttl_seconds=7 * 24 * 3600)
    assert res.deleted == 1
    assert s.deleted == [f"parse-staging/{SESSION}/original"]


# ── 정지 / 안전 ────────────────────────────────────────────────────────────

def test_ttl_zero_stops_that_lane_only():
    """0 은 '전량 삭제' 가 아니라 '그 갈래 정지' 다."""
    s = FakeStore([(f"{SESSION}/original", 99999),
                   ("batch/b-1/i-1/original/a.pdf", 30 * 24 * 3600)])
    res = _sweep(s, preview_ttl_seconds=0, batch_ttl_seconds=7 * 24 * 3600)
    assert res.deleted == 1
    assert s.deleted == ["parse-staging/batch/b-1/i-1/original/a.pdf"]


def test_both_ttls_disabled_is_a_full_stop():
    s = FakeStore([(f"{SESSION}/original", 99999)])
    res = _sweep(s, preview_ttl_seconds=0, batch_ttl_seconds=0)
    assert res.aborted == "staging gc disabled" and s.deleted == []


def test_missing_last_modified_is_not_deleted():
    """나이를 모르는 걸 '오래됨' 으로 흡수하면 방금 올린 원본이 날아간다."""
    s = FakeStore([(f"{SESSION}/original", None)])
    assert _sweep(s).deleted == 0


def test_naive_last_modified_is_not_deleted():
    s = FakeStore([(f"{SESSION}/original", 99999, False)])
    assert _sweep(s).deleted == 0


def test_list_failure_deletes_nothing():
    s = FakeStore([(f"{SESSION}/original", 99999)])
    s.list_fails = True
    res = _sweep(s)
    assert res.aborted == "list failed" and s.deleted == []


def test_batch_cap_limits_deletions_per_cycle():
    """첫 사이클에 214MB 를 한 번에 지우지 않는다 — 나눠 돈다."""
    s = FakeStore([(f"{SESSION}/original", 99999)] * 10)
    res = _sweep(s, batch=3)
    assert res.expired == 10 and res.deleted == 3


# ── 기본값 ─────────────────────────────────────────────────────────────────

def test_defaults_are_one_hour_and_seven_days(monkeypatch):
    for k in ("KBP_STAGING_TTL_SECONDS", "KBP_STAGING_BATCH_TTL_SECONDS"):
        monkeypatch.delenv(k, raising=False)
    cfg = StagingGcConfig()
    assert cfg.preview_ttl_seconds == 3600
    assert cfg.batch_ttl_seconds == 7 * 24 * 3600


def test_garbage_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("KBP_STAGING_TTL_SECONDS", "off")
    assert StagingGcConfig().preview_ttl_seconds == 3600
