"""global 검색 동시성 슬롯 + 리포트 조회 — **SQL 의미론**. 실 Postgres 필요.

여기 있는 것만 실 DB 가 필요하다. mode 검증·clamp·오류 매핑 같은 순수 로직은
`test_search_endpoint.py`(PG 불요)에 있다 — 그쪽까지 이 파일에 넣으면 `KBP_PG_DSN`
미설정 시 모듈 전체가 skip 되어 기본 dev 상태에서 한 줄도 안 돈다.

특히 고정하는 것:
  * **advisory lock 이 실제로 전역 상한을 지킨다** — 이게 §2.3 의 존재 이유다.
    잠금 없이 `SELECT count` → `INSERT` 하면 커밋 전 INSERT 가 다른 트랜잭션의 count 에
    보이지 않아 상한이 워커 수배로 깨진다(TOCTOU). threading.Semaphore 를 기각한 이유가
    그대로 남는 셈이다.
  * **테이블 부재는 fail-open(=없음), 그 외 psycopg.Error 는 raise** — DB 장애가
    "리포트가 아직 없다" 는 거짓 안내로 위장되면 안 된다.
  * `newest_report_time` 이 `(newest, oldest, count)` 를 돌려준다 — `max` 만으로는
    낡은 리포트를 감춘다.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

import service.app as app_mod
from kb_pipeline.search import newest_report_time, reports_exist
from service.jobs.schema import ensure_schema

pytestmark = pytest.mark.requires_pg

DSN = os.environ.get("KBP_PG_DSN")

if not DSN:
    pytest.skip("KBP_PG_DSN unset", allow_module_level=True)

WS = "11111111-2222-3333-4444-555555555555"

_REPORTS_DDL = """
CREATE TABLE IF NOT EXISTS public.community_reports (
    id bigserial PRIMARY KEY,
    workspace_id uuid NOT NULL,
    level int NOT NULL,
    community_id int NOT NULL,
    title text NOT NULL,
    summary text,
    findings jsonb,
    rank real,
    entity_ids text[],
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, level, community_id)
);
"""


@pytest.fixture()
def dsn():
    ensure_schema(DSN)
    with psycopg.connect(DSN) as conn:
        conn.execute("DELETE FROM kbp.global_search_slots")
        conn.commit()
    yield DSN
    with psycopg.connect(DSN) as conn:
        conn.execute("DELETE FROM kbp.global_search_slots")
        conn.commit()


@pytest.fixture()
def reports(dsn):
    """`community_reports` 를 만들고 비운다(실제로는 store_reports 가 lazy 생성한다)."""
    with psycopg.connect(dsn) as conn:
        conn.execute(_REPORTS_DDL)
        conn.execute("DELETE FROM public.community_reports WHERE workspace_id = %s", (WS,))
        conn.commit()
    yield dsn
    with psycopg.connect(dsn) as conn:
        conn.execute("DELETE FROM public.community_reports WHERE workspace_id = %s", (WS,))
        conn.commit()


def _add_report(dsn, cid: int, created_at: dt.datetime | None = None):
    with psycopg.connect(dsn) as conn:
        if created_at is None:
            conn.execute(
                "INSERT INTO public.community_reports"
                " (workspace_id, level, community_id, title) VALUES (%s,0,%s,%s)",
                (WS, cid, f"t{cid}"))
        else:
            conn.execute(
                "INSERT INTO public.community_reports"
                " (workspace_id, level, community_id, title, created_at)"
                " VALUES (%s,0,%s,%s,%s)", (WS, cid, f"t{cid}", created_at))
        conn.commit()


# ── 스키마 ─────────────────────────────────────────────────────────────────

def test_schema_creates_the_slots_table(dsn):
    with psycopg.connect(dsn) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='kbp'").fetchall()}
    assert "global_search_slots" in names


# ── 동시성 상한 (§2.3 의 존재 이유) ────────────────────────────────────────

def test_slot_respects_the_limit(dsn):
    assert app_mod._acquire_global_slot(dsn, 2) is not None
    assert app_mod._acquire_global_slot(dsn, 2) is not None
    assert app_mod._acquire_global_slot(dsn, 2) is None      # 상한 도달


def test_release_frees_a_slot(dsn):
    a = app_mod._acquire_global_slot(dsn, 1)
    assert app_mod._acquire_global_slot(dsn, 1) is None
    app_mod._release_global_slot(dsn, a)
    assert app_mod._acquire_global_slot(dsn, 1) is not None


def test_concurrent_acquire_never_exceeds_the_limit(dsn):
    """★ 이 테스트가 §2.3 의 핵심이다.

    advisory lock 이 없으면 커밋 전 INSERT 가 다른 트랜잭션의 `count(*)` 에 보이지 않아
    동시 요청이 각자 `count < limit` 을 읽고 각자 INSERT 한다 → 상한이 깨진다.
    `-w 4` 워커에서 threading.Semaphore 가 무효인 것과 같은 결과가 되므로, DB 카운터를
    도입한 의미가 사라진다.
    """
    limit = 2
    with ThreadPoolExecutor(max_workers=8) as ex:
        got = list(ex.map(lambda _: app_mod._acquire_global_slot(dsn, limit), range(8)))
    granted = [g for g in got if g is not None]
    assert len(granted) == limit, f"상한 {limit} 인데 {len(granted)}개가 승인됐다"
    with psycopg.connect(dsn) as conn:
        n = conn.execute("SELECT count(*) FROM kbp.global_search_slots").fetchone()[0]
    assert n == limit


def test_stale_slots_are_reclaimed(dsn, monkeypatch):
    """프로세스가 응답 없이 죽으면(OOM 등) 슬롯이 남는다 — TTL 청소가 회수해야 한다."""
    a = app_mod._acquire_global_slot(dsn, 1)
    assert a is not None
    with psycopg.connect(dsn) as conn:      # 그 슬롯을 아주 오래된 것으로 만든다
        conn.execute("UPDATE kbp.global_search_slots"
                     " SET claimed_at = now() - interval '10 days'")
        conn.commit()
    assert app_mod._acquire_global_slot(dsn, 1) is not None   # 회수됐다


def test_ttl_follows_llm_timeout_so_live_slots_survive(dsn, monkeypatch):
    """TTL 이 실제 소요보다 짧으면 **살아있는 슬롯이 지워져 상한이 사라진다.**

    `KBP_LLM_TIMEOUT` 관례값(300)을 global 에 넣으면 최악 30분인데, TTL 이 10분
    하드코딩이면 20분 남은 요청의 슬롯이 청소된다.
    """
    monkeypatch.setenv("KBP_GLOBAL_LLM_TIMEOUT", "300")
    a = app_mod._acquire_global_slot(dsn, 1)
    with psycopg.connect(dsn) as conn:      # 15분 전에 잡은 슬롯 = 아직 살아있다
        conn.execute("UPDATE kbp.global_search_slots"
                     " SET claimed_at = now() - interval '15 minutes'")
        conn.commit()
    # TTL = (5+1)*300*2 = 3600s 이므로 15분 된 슬롯은 살아있어야 한다
    assert app_mod._acquire_global_slot(dsn, 1) is None
    app_mod._release_global_slot(dsn, a)


def test_release_swallows_errors(dsn):
    """release 는 best-effort — 여기서 예외가 새면 이미 완성된 응답이 500 이 된다."""
    app_mod._release_global_slot("postgres://nobody@127.0.0.1:1/none", 1)  # 예외 없이 반환


# ── 리포트 조회 오류 의미론 ────────────────────────────────────────────────

def test_reports_exist_false_when_no_rows(reports):
    assert reports_exist(WS, reports) is False


def test_reports_exist_true_with_rows(reports):
    _add_report(reports, 1)
    assert reports_exist(WS, reports) is True


def test_reports_exist_is_scoped_to_the_workspace(reports):
    _add_report(reports, 1)
    assert reports_exist(str(uuid.uuid4()), reports) is False


def test_reports_exist_missing_table_is_not_an_error(dsn):
    """`community_reports` 는 store_reports 안에서 lazy 생성된다 — 부재는 "없음" 이다."""
    with psycopg.connect(dsn) as conn:
        conn.execute("DROP TABLE IF EXISTS public.community_reports")
        conn.commit()
    assert reports_exist(WS, dsn) is False
    assert newest_report_time(WS, dsn) == (None, None, 0)


def test_reports_exist_raises_on_real_db_errors():
    """★ DB 장애를 "리포트가 아직 없다" 로 위장하면 안 된다 — 호출자가 503 으로 바꾼다."""
    with pytest.raises(psycopg.Error):
        reports_exist(WS, "postgres://nobody@127.0.0.1:1/none")


# ── 신선도 (max 만으로는 낡은 리포트를 감춘다) ─────────────────────────────

def test_newest_report_time_returns_newest_oldest_and_count(reports):
    old = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    new = dt.datetime(2026, 8, 9, tzinfo=dt.timezone.utc)
    _add_report(reports, 1, old)
    _add_report(reports, 2, new)
    newest, oldest, n = newest_report_time(WS, reports)
    assert n == 2
    assert newest == new
    # ★ oldest 가 없으면 "38행이 두 달 전인데 1행만 어제 갱신" 을 구분할 수 없다
    assert oldest == old
    assert newest != oldest
