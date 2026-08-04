"""GC 판정 로직 — DB·MinIO 없이 더블로 돈다.

여기서 지키는 계약:
  * **fail-closed** — 판정 입력이 하나라도 실패하면 삭제 0건
  * **grace** — 제출 직후(행 INSERT 전) 객체를 지우지 않는다
  * `last_modified` 가 None·naive 면 삭제하지 않는다
  * **sanity 가드가 §0 의 상태에서 스윕을 막지 않는다**(v2 의 결함)
  * TTL<=0 은 전량 삭제가 아니라 정지
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest

from service.jobs.gc import GcConfig, run_orphan_sweep, run_ttl_gc, ttl_seconds

NOW = dt.datetime(2026, 8, 4, 12, 0, tzinfo=dt.timezone.utc)


class FakeRepo:
    def __init__(self, *, present=None, refs=None, purge=None,
                 present_fails=False, refs_fails=False):
        self._present = set(present or ())
        self._refs = set(refs or ())
        self._purge = purge
        self.present_fails = present_fails
        self.refs_fails = refs_fails
        self.purge_calls = []

    def purge_expired(self, *, ttl_seconds, batch):
        self.purge_calls.append((ttl_seconds, batch))
        if ttl_seconds is None or ttl_seconds <= 0:
            return None
        return list(self._purge or [])

    def job_ids_present(self, ids):
        return None if self.present_fails else {i for i in ids if i in self._present}

    def refs_in_use(self, keys):
        return None if self.refs_fails else {k for k in keys if k in self._refs}


class FakeBlobs:
    def __init__(self, objects=()):
        # objects: [(job_id|None, key, last_modified)]
        self.objects = list(objects)
        self.deleted = []
        self.list_fails = False

    def iter_job_objects(self):
        if self.list_fails:
            raise RuntimeError("minio down")
        yield from self.objects

    def delete(self, key):
        self.deleted.append(key)


def _obj(key_id=None, name="input.bin", age_s=99999, tz=True):
    jid = key_id or uuid.uuid4()
    lm = NOW - dt.timedelta(seconds=age_s)
    if not tz:
        lm = lm.replace(tzinfo=None)
    return (jid, f"kbp-jobs/{jid}/{name}", lm)


def _sweep(repo, blobs, **kw):
    return run_orphan_sweep(repo, blobs, cfg=GcConfig(**{"ttl_seconds": 3600, **kw}),
                            now=NOW)


# ── TTL 삭제 ───────────────────────────────────────────────────────────────

def test_ttl_gc_deletes_all_three_refs():
    rows = [{"id": uuid.uuid4(), "kind": "parse", "legacy": False,
             "completed_at": NOW, "input_ref": "a", "payload_ref": "b",
             "result_ref": "c"}]
    b = FakeBlobs()
    assert run_ttl_gc(FakeRepo(purge=rows), b, cfg=GcConfig(ttl_seconds=3600)) == 1
    assert b.deleted == ["a", "b", "c"]


def test_ttl_gc_disabled_deletes_nothing():
    """TTL<=0 은 비상 정지다 — 전량 삭제 레버가 되면 안 된다."""
    rows = [{"id": uuid.uuid4(), "completed_at": NOW, "input_ref": "a"}]
    b = FakeBlobs()
    assert run_ttl_gc(FakeRepo(purge=rows), b, cfg=GcConfig(ttl_seconds=0)) == 0
    assert b.deleted == []


def test_ttl_gc_warns_on_null_completed_at(caplog):
    rows = [{"id": uuid.uuid4(), "kind": "parse", "legacy": False,
             "completed_at": None, "input_ref": "a"}]
    with caplog.at_level("WARNING"):
        run_ttl_gc(FakeRepo(purge=rows), FakeBlobs(), cfg=GcConfig(ttl_seconds=3600))
    assert any("NULL completed_at" in r.message for r in caplog.records)


# ── 고아 스윕: 기본 판정 ───────────────────────────────────────────────────

def test_sweep_deletes_orphan_with_no_row():
    o = _obj()
    b = FakeBlobs([o])
    res = _sweep(FakeRepo(), b)
    assert res.deleted == 1 and b.deleted == [o[1]]


def test_sweep_keeps_object_whose_job_row_exists():
    o = _obj()
    b = FakeBlobs([o])
    res = _sweep(FakeRepo(present=[o[0]]), b)
    assert res.deleted == 0 and b.deleted == []


def test_sweep_keeps_object_referenced_by_a_row_key():
    """행 id 와 키가 어긋나도 *_ref 로 살아있으면 보존한다(4번 조건)."""
    o = _obj()
    b = FakeBlobs([o])
    res = _sweep(FakeRepo(refs=[o[1]]), b)     # id 는 없지만 키가 참조됨
    assert res.deleted == 0


def test_sweep_respects_grace():
    """제출 직후(행 INSERT 전) 객체를 지우면 살아있는 잡이 깨진다."""
    fresh = _obj(age_s=60)
    b = FakeBlobs([fresh])
    res = _sweep(FakeRepo(), b, orphan_grace=21600)
    assert res.deleted == 0 and res.candidates == 0


def test_sweep_skips_objects_without_last_modified():
    jid = uuid.uuid4()
    b = FakeBlobs([(jid, f"kbp-jobs/{jid}/input.bin", None)])
    assert _sweep(FakeRepo(), b).deleted == 0


def test_sweep_skips_naive_last_modified():
    b = FakeBlobs([_obj(tz=False)])
    assert _sweep(FakeRepo(), b).deleted == 0


def test_sweep_ignores_foreign_keys():
    """우리 형식이 아닌 키는 건드리지 않는다(같은 버킷에 페이지 이미지가 있다)."""
    b = FakeBlobs([(None, "somedoc/page_1.jpeg", NOW - dt.timedelta(days=9))])
    res = _sweep(FakeRepo(), b)
    assert res.listed == 0 and res.deleted == 0 and b.deleted == []


# ── fail-closed ────────────────────────────────────────────────────────────

def test_sweep_aborts_when_listing_fails():
    b = FakeBlobs([_obj()]); b.list_fails = True
    res = _sweep(FakeRepo(), b)
    assert res.aborted == "list failed" and b.deleted == []


def test_sweep_aborts_when_job_ids_present_fails():
    b = FakeBlobs([_obj()])
    res = _sweep(FakeRepo(present_fails=True), b)
    assert res.aborted == "job_ids_present failed" and b.deleted == []


def test_sweep_aborts_when_refs_in_use_fails():
    """refs_in_use 가 빈 set 을 돌려주면 4번 방어가 무력화된다 — 반드시 중단."""
    b = FakeBlobs([_obj()])
    res = _sweep(FakeRepo(refs_fails=True), b)
    assert res.aborted == "refs_in_use failed" and b.deleted == []


# ── sanity 가드 — v2 의 결함 ───────────────────────────────────────────────

def test_sanity_guard_does_not_block_the_scenario_it_exists_for():
    """§0 의 실측 상태: 잡 행 0 + 오래된 고아 14개 → **반드시 수거해야 한다**.

    v2 는 분모를 '후보'로 두고 하한이 없어 14/14 = 1.0 > 0.9 로 보류했다. 스윕이
    필요한 유일한 상태가 정확히 스윕이 안 도는 상태였다.
    """
    b = FakeBlobs([_obj() for _ in range(14)])
    res = _sweep(FakeRepo(), b)
    assert res.aborted is None
    assert res.deleted == 14


def test_sanity_guard_holds_back_when_ratio_high_and_volume_large():
    """대량인데 거의 전부 고아면 뭔가 잘못된 것 — 보류한다."""
    b = FakeBlobs([_obj() for _ in range(200)])
    res = _sweep(FakeRepo(), b, orphan_min_for_ratio=100, orphan_max_ratio=0.9)
    assert res.aborted and "ratio" in res.aborted
    assert b.deleted == []


def test_sanity_denominator_is_full_listing_not_candidates():
    """분모는 grace 필터 **이전** 전체 나열이다.

    고아 150 + 살아있는(행 있음) 객체 150 → 비율 0.5 라 통과해야 한다. 분모를 후보로
    잡으면 150/150 = 1.0 으로 잘못 보류된다.
    """
    live = [_obj() for _ in range(150)]
    orphans = [_obj() for _ in range(150)]
    b = FakeBlobs(live + orphans)
    res = _sweep(FakeRepo(present=[o[0] for o in live]), b,
                 orphan_min_for_ratio=100, orphan_max_ratio=0.9)
    assert res.aborted is None
    assert res.deleted == 150


def test_sweep_batch_limits_deletions_only():
    b = FakeBlobs([_obj() for _ in range(10)])
    res = _sweep(FakeRepo(), b, batch=3)
    assert res.listed == 10 and res.orphans == 10 and res.deleted == 3


def test_sweep_disabled_when_ttl_disabled():
    b = FakeBlobs([_obj()])
    res = run_orphan_sweep(FakeRepo(), b, cfg=GcConfig(ttl_seconds=0), now=NOW)
    assert res.aborted == "gc disabled" and b.deleted == []


# ── TTL 파서 ───────────────────────────────────────────────────────────────

def test_ttl_parser_rejects_garbage(monkeypatch):
    """파싱 실패가 '기본값으로 동작'이 되면 안 된다 — 정지 신호(None)여야 한다."""
    monkeypatch.delenv("KBP_JOB_TTL_SECONDS", raising=False)
    monkeypatch.setenv("KBP_JOB_TTL_HOURS", "off")
    assert ttl_seconds() is None


def test_ttl_parser_seconds_wins(monkeypatch):
    monkeypatch.setenv("KBP_JOB_TTL_SECONDS", "90")
    monkeypatch.setenv("KBP_JOB_TTL_HOURS", "72")
    assert ttl_seconds() == 90


def test_ttl_parser_default(monkeypatch):
    monkeypatch.delenv("KBP_JOB_TTL_SECONDS", raising=False)
    monkeypatch.delenv("KBP_JOB_TTL_HOURS", raising=False)
    assert ttl_seconds() == 72 * 3600


@pytest.mark.parametrize("val", ["0", "-1"])
def test_ttl_parser_zero_or_negative_is_stop_not_purge_all(monkeypatch, val):
    monkeypatch.delenv("KBP_JOB_TTL_SECONDS", raising=False)
    monkeypatch.setenv("KBP_JOB_TTL_HOURS", val)
    assert ttl_seconds() <= 0        # run_* 가 이 값을 정지로 해석한다
