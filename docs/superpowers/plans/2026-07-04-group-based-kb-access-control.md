<!-- plan-version: v4 -->
<!-- codex-validation: READY v4 at 2026-07-04T15:31:48Z (adversarial substitute — codex backend hung; per user pref) -->
<!-- validation-note: codex:codex-rescue hung ~21min without verdict; substituted with adversarial general-purpose reviewer over 3 rounds (v1→13 must-fix, v2→3 must-fix, v3→1 must-fix), all resolved. Final v4 fix (POST /kb asserts 200 not 201) applied the reviewer's exact prescribed correction. -->

<!-- Cross-repo plan: kb-pipeline (this repo) + kb-backend (/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base). -->

<!-- CLARIFIED 2026-07-07 (POST /kb group_id 정책 확정, 사용자 결정):
     초기 구현은 group_id 를 **선택(`Form(default=None)`)** 으로 받고 미지정 시 소유자 개인
     기본그룹을 **자동생성**했다(plan 본문 R15 "required" 서술과 불일치 = 부분구현). 최종 결정:
     **group_id 필수(`Form(...)`) + 반드시 기존 그룹에 매핑** — 그룹을 먼저 만든 뒤 그 group_id
     로 KB 를 생성한다. 자동 기본그룹 생성·미매핑(NULL) 생성 **모두 금지**. 미지정→422, 존재하지
     않는 group_id→422. 즉 plan 본문(line 49/834/869/895)의 "required group_id" 서술이 정답이며
     실제 구현이 이제 그에 일치한다. (마이그레이션 백필 line 313 은 역사적 사실로 유지.)
     테스트: test_kb_group_assignment.py::test_create_kb_requires_group_id (raw client, 422),
     그룹 필수화에 따른 기존 KB생성 테스트들은 conftest `_make_autogrant_client`(그룹 auto-provision
     + owner grant)로 전제 제공. -->

# Group-Based KB Access Control + Postgres Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restrict every knowledge base to a single owning group so that only members of that group can read/search it, resolving access entirely in kb-backend before it ever calls the facade; and consolidate the two Postgres servers into one instance (two databases) for operations.

**Architecture:** Access control is an authorization layer that lives **only in kb-backend** — the ingestion pipeline (facade/parse/chunk/edgequake) is unchanged. kb-backend gains `groups` + `group_members` tables and a `knowledge_bases.group_id` FK (KB↔group is 1:1, user↔group is M:N). The ACL seam (`app/core/acl.py` + `app/dependencies.py` + `agents.py`/`comparison.py`/`jobs.py` call sites) switches from per-user `kb_shares` to group-membership checks. The facade (:19000) is locked down with a shared-secret header so nobody can bypass the kb-backend ACL by calling it directly. Postgres consolidation runs kb-backend's `kb_orchestrator` database inside the same server instance that hosts edgequake's `edgequake` database (two databases, one server), which requires making the edgequake launcher non-destructive (persistent volume) so restarts no longer wipe data.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (`Mapped[...]`, `select(...)` style — the repo layer uses `session.execute(select(...)).scalars()` and `session.get(...)`, NOT legacy `.query(...)`), Alembic (psycopg v3), PostgreSQL 16 + pgvector + Apache AGE (edgequake image `ghcr.io/raphaelmansuy/edgequake-postgres:latest`), Docker, pytest.

## Global Constraints

- **Pipeline/edgequake code is OUT OF SCOPE.** Do not modify `service/edgequake.py`, `parse_service/*`, `kb_pipeline/*`, or edgequake migrations. The only kb-pipeline-repo changes are the launcher script (Phase 0) and the facade shared-secret gate in `service/app.py` (Phase 4).
- **tenant_id stays the fixed constant** `00000000-0000-0000-0000-000000000002` — never overload it with group identity. Workspace-per-KB isolation already equals per-group isolation because KB↔group is 1:1.
- **Access = group membership only.** `can_read_kb(user, kb)` is true iff the user is a member of `kb.group`. Ownership grants **management** rights (create/delete/assign-group), NOT read-by-default. This is the user's explicit "대체(replace)" decision — individual `kb_shares` is retired.
- **KB↔group = 1:1** (`knowledge_bases.group_id` FK, one group per KB). **user↔group = M:N** (`group_members` join). A user may belong to many groups.
- **Group deletion → KB unsearchable, via `ON DELETE SET NULL`.** `knowledge_bases.group_id` uses `ON DELETE SET NULL`: deleting a group nulls its KBs' `group_id`, and `can_read_kb` returns `False` when `group_id IS NULL` → the KB becomes unsearchable (owner may reassign a new group later). `group_members` uses `ON DELETE CASCADE` (removing a group also drops its memberships). This satisfies the "모든 유저 퇴사→그룹 제거→검색 불가" requirement without any edgequake row re-tagging. (RESTRICT is explicitly rejected — it would block group deletion instead of nulling.)
- **SQLAlchemy 2.0 style is mandatory in the repo layer.** `backend/app/repositories.py` uses `select(...)`, `session.execute(...).scalars()`, `session.get(Model, id)`. All new repo code must match; do not introduce `.query(...)`.
- **Two repos:** kb-pipeline repo = `/Users/xxx/workspace/8.kb-pipeline`; kb-backend repo = `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base`. Every path below is prefixed accordingly.
- **kb-backend migrations use Alembic** (`alembic upgrade head`). Never `Base.metadata.create_all()` in production paths. `Base` is imported from `app.db` (NOT `app.models.base`); the mixins `UUIDPKMixin`/`TimestampMixin` are imported from `app.models.base`.
- **Test conventions (verified):** the session fixture is `db_session`; users are seeded via `seed_user(db_session, email=..., role=...)`; auth headers via `auth_header_for(user)`. There are NO `make_user`/token fixtures. A test that needs HTTP MUST use the **`app_client` fixture** (Task 2.1 adds it to `conftest.py`) which overrides `get_db` with a **generator** yielding `db_session` and calls `app.dependency_overrides.clear()` on teardown — copied exactly from the real pattern at `backend/tests/test_agents_api.py:23-30` / `test_api_auth.py:23-27`. **Never** set `app.dependency_overrides[get_db] = lambda: db_session` without a teardown `.clear()` — it leaks the override into every later test in the session and silently binds the top-level `client` fixture (`conftest.py:72`) to a dead session, producing spurious failures.
- **Tests that assert retired/old behavior and MUST be rewritten (verified 8 files reference shares/`kb_shares`/`can_read_kb`):** `test_shares_api.py` and `test_acl.py` need full rewrites (they assert the retired 201-grant + owner-OR-shares ACL); `test_delete_kb.py`, `test_agents_api.py`, `test_documents_api.py`, `test_comparison_api.py`, `test_models_smoke.py`, `test_chat_proxy.py` need fixture updates (seed group membership instead of `kb_shares`). Named here so the "run whole suite → PASS" gates are achievable, not surprises.
- **RLS hardening (edgequake non-superuser role + FORCE RLS) is explicitly deferred** to a later phase (tracked as W4) — NOT in this plan's scope.

---

## File Structure

### kb-pipeline repo (`/Users/xxx/workspace/8.kb-pipeline`)
- **Modify** `service/scripts/start_dedicated_edgequake.sh` — persistent named volume, idempotent create (stop wiping), bootstrap `kb`/`kb_orchestrator` (Phase 0).
- **Modify** `service/app.py` — shared-secret dependency (`X-Facade-Key`) gating stateful endpoints (Phase 4).
- **Create** `service/tests/test_facade_auth.py` — tests for the gate (Phase 4).

### kb-backend repo (`/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base`)
- **Create** `backend/app/models/group.py` — `Group` + `GroupMember` ORM models.
- **Modify** `backend/app/models/knowledge_base.py` — add `group_id` FK (`ON DELETE SET NULL`).
- **Modify** `backend/app/models/__init__.py` — register `Group`, `GroupMember`.
- **Modify** `backend/app/core/acl.py` — `can_read_kb`/`can_write_kb`/`resolve_agent_access` use a `GroupMembersRepoProtocol`.
- **Modify** `backend/app/repositories.py` — add `SqlGroupsRepo`, `SqlGroupMembersRepo` (2.0 `select` style); add `SqlKbRepo.list_by_group_ids`.
- **Modify** `backend/app/dependencies.py` — `get_readable_kb` via group repo; add `get_current_developer`.
- **Modify** `backend/app/routers/agents.py`, `backend/app/routers/comparison.py`, `backend/app/routers/jobs.py` — swap `SqlSharesRepo` → `SqlGroupMembersRepo` at ACL call sites.
- **Create** `backend/app/routers/groups.py` — group CRUD + membership (developer-gated).
- **Modify** `backend/app/routers/kb.py` — `POST /kb` accepts `group_id` as a `Form(...)` field; `GET /kb` lists by membership.
- **Modify** `backend/app/routers/shares.py` — three retired handlers each return `410 Gone` (keep `prefix="/kb"`).
- **Create** `backend/app/schemas/groups.py` — group/membership schemas.
- **Modify** `backend/app/schemas/jobs.py` — `KbCreateResponse`/`KbSummary` carry `group_id`.
- **Modify** `backend/tests/conftest.py` — add `make_group`/`make_kb` helpers + `seed_user(role="developer")` usage.
- **Create** `backend/migrations/versions/<rev>_groups_and_kb_group_fk.py` — Alembic migration.
- **Create** `backend/tests/test_acl_groups.py`, `backend/tests/test_groups_router.py`, `backend/tests/test_kb_group_assignment.py`.
- **Modify** `backend/app/config.py` — default `database_url` → `:5433/kb_orchestrator` (Phase 0 coupling).

---

## Phase 0 — Postgres Consolidation (one server, two databases)

> After this phase, one Postgres instance (`eq-pg-kbp`, port 5433) hosts **both** the `edgequake` database (pgvector+AGE) and the `kb_orchestrator` database (kb-backend). The launcher must stop destroying data.

### Task 0.1: Make the edgequake launcher non-destructive (persistent volume)

**Files:**
- Modify: `/Users/xxx/workspace/8.kb-pipeline/service/scripts/start_dedicated_edgequake.sh`

**Interfaces:**
- Produces: a Postgres server on `localhost:5433` whose data survives container recreation via named volume `eq-pg-kbp-data`.

- [ ] **Step 1: Read the current launcher** and locate the destructive block (do NOT rely on fixed line numbers — the file has secret reads on lines 3-7, then the `docker rm -f`…`docker run …ghcr.io/...edgequake-postgres:latest` block, then a multi-line `until [ "$ok" -ge 5 ]` readiness loop).

Run: `cat /Users/xxx/workspace/8.kb-pipeline/service/scripts/start_dedicated_edgequake.sh`
Expected: confirm the block `docker rm -f eq-pg-kbp 2>/dev/null || true` followed by `docker run -d --name eq-pg-kbp -p 5433:5432 \ ... ghcr.io/raphaelmansuy/edgequake-postgres:latest` with **no `-v` volume flag**.

- [ ] **Step 2: Replace that exact `docker rm -f … docker run …latest` block** (match by content, not line number) with an idempotent, volume-backed create:

```bash
# Idempotent Postgres: reuse the running container if present; otherwise create it
# with a PERSISTENT named volume so restarts never wipe edgequake OR kb_orchestrator.
if [ "$(docker inspect -f '{{.State.Running}}' eq-pg-kbp 2>/dev/null)" = "true" ]; then
  echo "eq-pg-kbp already running — reusing (data preserved)"
else
  docker rm -f eq-pg-kbp 2>/dev/null || true
  docker run -d --name eq-pg-kbp -p 5433:5432 \
    -v eq-pg-kbp-data:/var/lib/postgresql/data \
    -e POSTGRES_USER=edgequake -e POSTGRES_PASSWORD=edgequake_secret -e POSTGRES_DB=edgequake \
    ghcr.io/raphaelmansuy/edgequake-postgres:latest
fi
```

> The edgequake-postgres image runs an init pass that restarts mid-startup. On a **populated** named volume, Postgres' entrypoint detects an existing `PGDATA` and SKIPS `initdb`/init scripts (standard `postgres` image behavior) — so the volume is not re-initialized. The readiness loop below still guards against the transient init server. Keep the existing `until [ "$ok" -ge 5 ]` loop as-is.

- [ ] **Step 3: Verify the volume survives a restart.**

Run:
```bash
bash /Users/xxx/workspace/8.kb-pipeline/service/scripts/start_dedicated_edgequake.sh &
sleep 25
docker exec eq-pg-kbp psql -U edgequake -d edgequake -c "CREATE TABLE _persist_probe(x int); INSERT INTO _persist_probe VALUES (1);"
docker restart eq-pg-kbp && sleep 15
docker exec eq-pg-kbp psql -U edgequake -d edgequake -c "SELECT count(*) FROM _persist_probe;"
docker exec eq-pg-kbp psql -U edgequake -d edgequake -c "DROP TABLE _persist_probe;"
```
Expected: `count` = `1` (survived restart).

> ⚠️ The pre-existing (volumeless) container held non-durable data. If a live edgequake dataset must be preserved, `pg_dump` it BEFORE the first new-launcher run, then restore. Record this in `docs/runbook-v2-smoke.md`.

- [ ] **Step 4: Commit (kb-pipeline repo).**

```bash
cd /Users/xxx/workspace/8.kb-pipeline
git add service/scripts/start_dedicated_edgequake.sh
git commit -m "infra(edgequake): persistent volume + idempotent launcher (stop wiping PG)"
```

### Task 0.2: Bootstrap the `kb` role + `kb_orchestrator` database in the same instance

**Files:**
- Modify: `/Users/xxx/workspace/8.kb-pipeline/service/scripts/start_dedicated_edgequake.sh` (append after the `until [ "$ok" -ge 5 ]` readiness loop, before the edgequake binary boot / `DATABASE_URL=...` line).

**Interfaces:**
- Produces: role `kb` (password `kb`) + database `kb_orchestrator` on `localhost:5433`, coexisting with `edgequake`.

- [ ] **Step 1: Append the idempotent bootstrap** right after the readiness loop:

```bash
# --- Consolidation: ensure kb-backend role + database exist in the same instance ---
docker exec eq-pg-kbp psql -U edgequake -d edgequake -tAc \
  "SELECT 1 FROM pg_roles WHERE rolname='kb'" | grep -q 1 || \
  docker exec eq-pg-kbp psql -U edgequake -d edgequake -c \
  "CREATE ROLE kb LOGIN PASSWORD 'kb';"
docker exec eq-pg-kbp psql -U edgequake -d edgequake -tAc \
  "SELECT 1 FROM pg_database WHERE datname='kb_orchestrator'" | grep -q 1 || \
  docker exec eq-pg-kbp psql -U edgequake -d edgequake -c \
  "CREATE DATABASE kb_orchestrator OWNER kb;"
```

- [ ] **Step 2: Verify both databases exist and are isolated.**

Run:
```bash
docker exec eq-pg-kbp psql -U edgequake -d edgequake -c "\l" | grep -E "edgequake|kb_orchestrator"
docker exec eq-pg-kbp psql -U kb -d kb_orchestrator -c "SELECT current_database(), current_user;"
docker exec eq-pg-kbp psql -U kb -d kb_orchestrator -c "\dx"
```
Expected: both DBs listed; second prints `kb_orchestrator | kb`; `\dx` shows only `plpgsql` (no vector/age — correct, extensions are per-DB).

- [ ] **Step 3: Commit.**

```bash
cd /Users/xxx/workspace/8.kb-pipeline
git add service/scripts/start_dedicated_edgequake.sh
git commit -m "infra(consolidation): ensure kb_orchestrator DB + kb role in eq-pg-kbp"
```

### Task 0.3: Point kb-backend at the consolidated instance

**Files:**
- Modify: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/app/config.py:49` (default DSN)
- Modify: kb-backend `.env` (`DATABASE_URL`)

**Interfaces:**
- Consumes: `kb_orchestrator` DB from Task 0.2.
- Produces: kb-backend connected to `postgresql+psycopg://kb:kb@localhost:5433/kb_orchestrator` by BOTH the `.env` and the code default (so CI / env-less runs don't silently hit the old `:5432`).

- [ ] **Step 1: Update the default** in `app/config.py:49`:

```python
    database_url: str = "postgresql+psycopg://kb:kb@localhost:5433/kb_orchestrator"
```

> This is a live coupling, not just an `.env` line: `migrations/env.py` reads the URL from `get_settings().database_url`, and any environment without `DATABASE_URL` set falls back to this default. The dev `.env` may still point at sqlite for local unit tests — leave that as-is; this default is the deployed-Postgres fallback.

- [ ] **Step 2: Set the deploy `.env`:**

```
DATABASE_URL=postgresql+psycopg://kb:kb@localhost:5433/kb_orchestrator
```

- [ ] **Step 3: Run existing migrations against the consolidated DB.**

Run:
```bash
cd /Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base
DATABASE_URL=postgresql+psycopg://kb:kb@localhost:5433/kb_orchestrator alembic upgrade head
docker exec eq-pg-kbp psql -U kb -d kb_orchestrator -c "\dt" | grep -E "users|knowledge_bases|kb_shares"
```
Expected: all existing migrations apply; tables present.

- [ ] **Step 4: Commit.**

```bash
cd /Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base
git add backend/app/config.py
git commit -m "infra(consolidation): default DSN → eq-pg-kbp:5433/kb_orchestrator"
```

---

## Phase 1 — Group Data Model (kb-backend)

### Task 1.1: Add `Group` and `GroupMember` models

**Files:**
- Create: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/app/models/group.py`
- Modify: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/app/models/__init__.py`

**Interfaces:**
- Produces: `Group(id, code, name, created_at, updated_at)`, `GroupMember(id, group_id, user_id, ...)` with `UNIQUE(group_id, user_id)`.

- [ ] **Step 1: Write the model file.** Import `Base` from `app.db` and the mixins from `app.models.base` (verified: `app/models/base.py` defines the mixins but NOT `Base`; every model does `from app.db import Base` — see `app/models/knowledge_base.py:22`):

```python
# app/models/group.py
from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import UUIDPKMixin, TimestampMixin


class Group(UUIDPKMixin, TimestampMixin, Base):
    """접근 그룹. KB 는 정확히 하나의 group 에 속하고, group 에는 N 명의 user 가 속한다."""

    __tablename__ = "groups"

    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    members: Mapped[list["GroupMember"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class GroupMember(UUIDPKMixin, TimestampMixin, Base):
    """user↔group M:N 조인. 유저는 여러 그룹에 속할 수 있다."""

    __tablename__ = "group_members"
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_group_member"),)

    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    group: Mapped["Group"] = relationship(back_populates="members")
```

- [ ] **Step 2: Register in `app/models/__init__.py`** — add `from app.models.group import Group, GroupMember` and append `"Group"`, `"GroupMember"` to `__all__`.

- [ ] **Step 3: Verify models register on `Base.metadata`.**

Run:
```bash
cd /Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base
python -c "from app.models import Base, Group, GroupMember; print(sorted(t for t in Base.metadata.tables if t in {'groups','group_members'}))"
```
Expected: `['group_members', 'groups']`

- [ ] **Step 4: Commit.**

```bash
git add backend/app/models/group.py backend/app/models/__init__.py
git commit -m "feat(models): add Group + GroupMember (user↔group M:N)"
```

### Task 1.2: Add `group_id` FK to `KnowledgeBase` (`ON DELETE SET NULL`)

**Files:**
- Modify: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/app/models/knowledge_base.py:29-73`

**Interfaces:**
- Produces: `KnowledgeBase.group_id: Mapped[uuid.UUID | None]` FK→`groups.id` `ON DELETE SET NULL` (nullable; enforced non-null for NEW KBs at the router layer). Deleting a group nulls this → KB unsearchable.

- [ ] **Step 1: Add the column + relationship** after `edgequake_workspace_id`:

```python
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    group: Mapped["Group | None"] = relationship()
```

**Do NOT add a runtime `from app.models.group import Group` import.** The file has no `TYPE_CHECKING` block; it resolves forward refs via string annotations + `# noqa: F821` (see `owner: Mapped["User"]` at `knowledge_base.py:68`). The string annotation `Mapped["Group | None"]` and `ForeignKey("groups.id", ...)` need no import — SQLAlchemy resolves `Group` from the mapper registry at configure time (guaranteed because `app/models/__init__.py` imports `group` — Task 1.1 Step 2). Adding a runtime import risks a circular import. If your linter flags the string ref, append `# noqa: F821` exactly as the file already does for `User`/`Document`.

- [ ] **Step 2: Verify.**

Run: `python -c "from app.models import KnowledgeBase; c=KnowledgeBase.__table__.columns['group_id']; print(c.nullable, [fk.ondelete for fk in c.foreign_keys])"`
Expected: `True ['SET NULL']`

- [ ] **Step 3: Commit.**

```bash
git add backend/app/models/knowledge_base.py
git commit -m "feat(models): KnowledgeBase.group_id FK ON DELETE SET NULL (KB↔group 1:1)"
```

### Task 1.3: Alembic migration (create tables, add FK, backfill existing KBs)

**Files:**
- Create: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/migrations/versions/<rev>_groups_and_kb_group_fk.py`

**Interfaces:**
- Consumes: models from 1.1/1.2 (`env.py` wires `target_metadata=app.models.Base.metadata`, verified).
- Produces: `groups`, `group_members` tables + `knowledge_bases.group_id` (`SET NULL` FK), with existing KBs backfilled into a per-owner default group so current owners keep read access.

- [ ] **Step 1: Autogenerate.**

Run:
```bash
cd /Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base
DATABASE_URL=postgresql+psycopg://kb:kb@localhost:5433/kb_orchestrator alembic revision --autogenerate -m "groups and kb group fk"
```
Expected: new file creating `groups`, `group_members`, adding `knowledge_bases.group_id`. **Verify** the FK is emitted with `ondelete="SET NULL"`; if autogenerate omits it, add it to the `op.create_foreign_key(...)`/`op.add_column` call by hand.

- [ ] **Step 2: Add the data-migration backfill** to `upgrade()` AFTER the DDL:

```python
import uuid
import sqlalchemy as sa

def upgrade() -> None:
    # ... autogenerated create_table('groups'), ('group_members'),
    #     add_column('knowledge_bases', sa.Column('group_id', ...)) ...

    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, owner_id FROM knowledge_bases WHERE group_id IS NULL"
    )).fetchall()
    for kb_id, owner_id in rows:
        gid = str(uuid.uuid4())
        code = f"kb-{str(kb_id).replace('-', '')[:8]}"
        conn.execute(sa.text(
            "INSERT INTO groups (id, code, name, created_at, updated_at) "
            "VALUES (:id, :code, :name, now(), now())"
        ), {"id": gid, "code": code, "name": f"Default group for {code}"})
        conn.execute(sa.text(
            "INSERT INTO group_members (id, group_id, user_id, created_at, updated_at) "
            "VALUES (:id, :gid, :uid, now(), now())"
        ), {"id": str(uuid.uuid4()), "gid": gid, "uid": str(owner_id)})
        conn.execute(sa.text(
            "UPDATE knowledge_bases SET group_id = :gid WHERE id = :kb"
        ), {"gid": gid, "kb": str(kb_id)})
```

> Confirm `groups`/`group_members` actually have `created_at`/`updated_at` (they do — via `TimestampMixin`). If the mixin uses `server_default=now()`, the explicit `now()` inserts above are still valid.

- [ ] **Step 3: Apply + verify backfill.**

Run:
```bash
DATABASE_URL=postgresql+psycopg://kb:kb@localhost:5433/kb_orchestrator alembic upgrade head
docker exec eq-pg-kbp psql -U kb -d kb_orchestrator -c "\dt" | grep -E "groups|group_members"
docker exec eq-pg-kbp psql -U kb -d kb_orchestrator -c "SELECT count(*) FROM knowledge_bases WHERE group_id IS NULL;"
```
Expected: both tables present; NULL group_id count = `0`.

- [ ] **Step 4: Round-trip the down/up path.**

Run: `alembic downgrade -1 && alembic upgrade head` (with the DATABASE_URL env)
Expected: clean, no error.

- [ ] **Step 5: Commit.**

```bash
git add migrations/versions/*groups_and_kb_group_fk.py
git commit -m "feat(db): migration for groups, group_members, kb.group_id + backfill"
```

---

## Phase 2 — ACL Switch to Group Membership (kb-backend)

### Task 2.1: Group repositories (2.0 `select` style) + test fixtures

**Files:**
- Modify: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/app/repositories.py`
- Modify: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/tests/conftest.py`

**Interfaces:**
- Produces: `SqlGroupMembersRepo(session).is_member(group_id, user_id) -> bool`, `.group_ids_for_user(user_id) -> list[str]`, `.add(...)`, `.remove(...)`; `SqlGroupsRepo` with `create/get/get_by_code/list/delete`. Fixtures `make_group(db_session, code, name) -> Group`, `make_kb(db_session, owner_id, group_id, **kw) -> KnowledgeBase`.

- [ ] **Step 1: Add conftest helpers** to `backend/tests/conftest.py` (mirror the existing `seed_user` helper's construction + commit style):

```python
def make_group(db_session, code="grp", name="Group"):
    from app.models.group import Group
    g = Group(code=code, name=name)
    db_session.add(g); db_session.commit(); db_session.refresh(g)
    return g

def make_kb(db_session, owner_id, group_id, name="KB", branch_code=1, provider="kb_pipeline"):
    import uuid
    from app.models.knowledge_base import KnowledgeBase
    kb = KnowledgeBase(
        owner_id=owner_id, group_id=group_id, name=name, branch_code=branch_code,
        provider=provider, collection_name=f"kb_{uuid.uuid4().hex[:12]}",
    )
    db_session.add(kb); db_session.commit(); db_session.refresh(kb)
    return kb
```

Also add an `app_client` fixture (copy the real teardown-clearing pattern from `test_agents_api.py:23-30`) so HTTP tests share `db_session` without leaking overrides:

```python
import pytest
from fastapi.testclient import TestClient

@pytest.fixture
def app_client(db_session):
    from app.main import app
    from app.dependencies import get_db
    def _override_db():
        yield db_session
    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
```

> Match the real fixture in `test_agents_api.py` exactly if it differs (e.g. it may already live in conftest under another name — reuse that instead of duplicating).

- [ ] **Step 2: Write the failing repo test** — `backend/tests/test_acl_groups.py`:

```python
def test_group_members_repo_is_member(db_session):
    from app.repositories import SqlGroupMembersRepo, SqlGroupsRepo
    from tests.conftest import make_group  # or import the fixtures as module funcs
    from app.models.user import User
    import uuid
    u = User(email="a@x.com", password_hash="x", role="user")
    db_session.add(u); db_session.commit(); db_session.refresh(u)
    g = SqlGroupsRepo(db_session).create("grp-a", "A")
    repo = SqlGroupMembersRepo(db_session)
    assert repo.is_member(str(g.id), str(u.id)) is False
    repo.add(str(g.id), str(u.id))
    assert repo.is_member(str(g.id), str(u.id)) is True
    assert str(g.id) in repo.group_ids_for_user(str(u.id))
```

> Use the repo's actual seeding conventions; if `seed_user` exists it is preferable to constructing `User` inline — call `seed_user(db_session, email="a@x.com")`.

- [ ] **Step 3: Run — expect fail.**

Run: `cd /Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base && pytest backend/tests/test_acl_groups.py::test_group_members_repo_is_member -v`
Expected: FAIL (`ImportError: cannot import name 'SqlGroupMembersRepo'`).

- [ ] **Step 4: Implement the repos** in `app/repositories.py`, matching the file's 2.0 style (`from sqlalchemy import select` is already imported there):

```python
from app.models.group import Group, GroupMember


class SqlGroupMembersRepo:
    def __init__(self, session): self._s = session

    def is_member(self, group_id: str, user_id: str) -> bool:
        stmt = select(GroupMember.id).where(
            GroupMember.group_id == group_id, GroupMember.user_id == user_id
        )
        return self._s.execute(stmt).first() is not None

    def group_ids_for_user(self, user_id: str) -> list[str]:
        stmt = select(GroupMember.group_id).where(GroupMember.user_id == user_id)
        return [str(gid) for gid in self._s.execute(stmt).scalars().all()]

    def add(self, group_id: str, user_id: str) -> None:
        self._s.add(GroupMember(group_id=group_id, user_id=user_id)); self._s.commit()

    def remove(self, group_id: str, user_id: str) -> None:
        stmt = select(GroupMember).where(
            GroupMember.group_id == group_id, GroupMember.user_id == user_id
        )
        obj = self._s.execute(stmt).scalars().first()
        if obj is not None:
            self._s.delete(obj); self._s.commit()


class SqlGroupsRepo:
    def __init__(self, session): self._s = session

    def create(self, code: str, name: str) -> Group:
        g = Group(code=code, name=name); self._s.add(g); self._s.commit(); self._s.refresh(g); return g

    def get(self, group_id: str) -> Group | None:
        return self._s.get(Group, group_id)

    def get_by_code(self, code: str) -> Group | None:
        return self._s.execute(select(Group).where(Group.code == code)).scalars().first()

    def list(self) -> list[Group]:
        return list(self._s.execute(select(Group).order_by(Group.code)).scalars().all())

    def delete(self, group_id: str) -> None:
        g = self._s.get(Group, group_id)
        if g is not None:
            self._s.delete(g); self._s.commit()
```

- [ ] **Step 5: Run — expect pass.**

Run: `pytest backend/tests/test_acl_groups.py::test_group_members_repo_is_member -v`
Expected: PASS

- [ ] **Step 6: Commit.**

```bash
git add backend/app/repositories.py backend/tests/test_acl_groups.py backend/tests/conftest.py
git commit -m "feat(repo): SqlGroupsRepo + SqlGroupMembersRepo (2.0 select style) + test helpers"
```

### Task 2.2: Rewrite ACL to use group membership

**Files:**
- Modify: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/app/core/acl.py`

**Interfaces:**
- Consumes: `GroupMembersRepoProtocol` with `is_member(group_id, user_id) -> bool`.
- Produces: `can_read_kb(user_id, kb, members_repo)` = member of `kb.group_id`; `can_write_kb(user_id, kb, members_repo)` = `user_id == kb.owner_id`; `resolve_agent_access(user_id, agent, kb, members_repo)`. **All three keep 4/3-arg positional shape so existing positional callers still bind.**

- [ ] **Step 1: Write failing ACL tests** appended to `test_acl_groups.py`:

```python
def test_can_read_kb_requires_group_membership(db_session):
    from app.core import acl
    from app.repositories import SqlGroupMembersRepo, SqlGroupsRepo
    from app.models.user import User
    from tests.conftest import make_kb
    owner = User(email="ow@x.com", password_hash="x", role="user")
    member = User(email="mm@x.com", password_hash="x", role="user")
    outsider = User(email="oo@x.com", password_hash="x", role="user")
    db_session.add_all([owner, member, outsider]); db_session.commit()
    g = SqlGroupsRepo(db_session).create("grp-b", "B")
    kb = make_kb(db_session, owner_id=owner.id, group_id=g.id)
    repo = SqlGroupMembersRepo(db_session); repo.add(str(g.id), str(member.id))
    assert acl.can_read_kb(str(member.id), kb, repo) is True
    assert acl.can_read_kb(str(outsider.id), kb, repo) is False
    assert acl.can_read_kb(str(owner.id), kb, repo) is False  # owner=management only

def test_can_write_kb_is_owner_only(db_session):
    from app.core import acl
    from app.repositories import SqlGroupMembersRepo, SqlGroupsRepo
    from app.models.user import User
    from tests.conftest import make_kb
    owner = User(email="ow2@x.com", password_hash="x", role="user")
    member = User(email="mm2@x.com", password_hash="x", role="user")
    db_session.add_all([owner, member]); db_session.commit()
    g = SqlGroupsRepo(db_session).create("grp-c", "C")
    kb = make_kb(db_session, owner_id=owner.id, group_id=g.id)
    repo = SqlGroupMembersRepo(db_session); repo.add(str(g.id), str(member.id))
    assert acl.can_write_kb(str(owner.id), kb, repo) is True
    assert acl.can_write_kb(str(member.id), kb, repo) is False
```

- [ ] **Step 2: Run — expect fail.**

Run: `pytest backend/tests/test_acl_groups.py -v -k "can_read or can_write"`
Expected: FAIL (current `can_read_kb` checks `kb_shares`).

- [ ] **Step 3: Rewrite `app/core/acl.py`** (framework-agnostic):

```python
from typing import Protocol


class GroupMembersRepoProtocol(Protocol):
    def is_member(self, group_id: str, user_id: str) -> bool: ...


def can_read_kb(user_id, kb, members_repo: GroupMembersRepoProtocol) -> bool:
    """읽기/검색 = KB group 멤버십. owner 라는 사실만으로는 read 권한 없음(관리권한과 분리)."""
    if getattr(kb, "group_id", None) is None:
        return False
    return members_repo.is_member(str(kb.group_id), str(user_id))


def can_write_kb(user_id, kb, members_repo: GroupMembersRepoProtocol) -> bool:
    """write(업로드/삭제/그룹지정) = owner(관리자)만."""
    return str(kb.owner_id) == str(user_id)


def resolve_agent_access(user_id, agent, kb, members_repo: GroupMembersRepoProtocol) -> bool:
    if not can_read_kb(user_id, kb, members_repo):
        return False
    if str(kb.owner_id) == str(user_id):
        return True
    return str(agent.owner_id) == str(user_id)
```

- [ ] **Step 4: Run — expect pass.**

Run: `pytest backend/tests/test_acl_groups.py -v`
Expected: PASS

- [ ] **Step 5: Commit.**

```bash
git add backend/app/core/acl.py backend/tests/test_acl_groups.py
git commit -m "feat(acl): read=group membership; write=owner; resolve_agent_access via members repo"
```

### Task 2.3: Wire dependencies + migrate ALL ACL call sites off `SqlSharesRepo`

**Files:**
- Modify: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/app/dependencies.py:499-534` (`get_readable_kb`, `get_owned_kb`; add `get_current_developer`)
- Modify: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/app/routers/agents.py:24,85-86`
- Modify: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/app/routers/comparison.py:40,193-194`
- Modify: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/app/routers/jobs.py:19,63-64`

**Interfaces:**
- Consumes: `SqlGroupMembersRepo`, updated `acl`.
- Produces: NO remaining construction of `SqlSharesRepo` feeding into `acl.*`. Every `can_read_kb`/`can_write_kb`/`resolve_agent_access` call passes a `SqlGroupMembersRepo(session)`.

> **Rationale (from review):** `SqlSharesRepo` has no `is_member`, so after Task 2.2 any call site still passing a shares repo into `acl.can_read_kb` raises `AttributeError` at runtime. These 4 files must move together.

- [ ] **Step 1: Update `dependencies.py`.** Replace the `SqlSharesRepo(session)` construction in `get_readable_kb` and `get_owned_kb` (lines ~512-513, ~530-531) with `SqlGroupMembersRepo(session)`; update imports (`from app.repositories import ... SqlGroupMembersRepo`; drop `SqlSharesRepo` if now unused). Add:

```python
def get_current_developer(current_user=Depends(get_current_user)):
    if getattr(current_user, "role", "user") != "developer":
        raise HTTPException(status_code=403, detail="developer role required")
    return current_user
```

(`HTTPException` is already imported at `dependencies.py:17`.)

- [ ] **Step 2: Update `agents.py`** — where it builds `SqlSharesRepo(session)` (line ~24 import, ~85-86 call into `resolve_agent_access`), construct `SqlGroupMembersRepo(session)` instead and pass it positionally.

- [ ] **Step 3: Update `comparison.py`** (lines ~40, ~193-194) — same swap for its `acl.can_read_kb(...)` call.

- [ ] **Step 4: Update `jobs.py`** (lines ~19, ~63-64) — same swap for its `acl.can_read_kb(...)` call.

- [ ] **Step 5: Grep to prove no ACL call site still uses shares.**

Run:
```bash
cd /Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base
grep -rn "SqlSharesRepo" backend/app | grep -vE "repositories.py|routers/shares.py"
```
Expected: **no output** (the class may remain defined in `repositories.py` and referenced only by the retired shares router, but nothing feeds it into `acl`).

- [ ] **Step 6: Run the whole suite** (fixtures that previously seeded `kb_shares` for read access must be updated to add group membership).

Run: `pytest backend/tests -v`
Expected: PASS (update any now-failing fixture to `SqlGroupMembersRepo(...).add(...)`).

- [ ] **Step 7: Commit.**

```bash
git add backend/app/dependencies.py backend/app/routers/agents.py backend/app/routers/comparison.py backend/app/routers/jobs.py backend/tests
git commit -m "feat(acl): migrate deps + agents/comparison/jobs call sites to group membership"
```

---

## Phase 3 — Group Management API + KB Assignment (kb-backend)

### Task 3.1: Group schemas

**Files:**
- Create: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/app/schemas/groups.py`

- [ ] **Step 1: Write schemas.**

```python
from pydantic import BaseModel


class GroupCreateRequest(BaseModel):
    code: str
    name: str


class GroupResponse(BaseModel):
    id: str
    code: str
    name: str


class GroupMemberRequest(BaseModel):
    user_email: str


class GroupMemberResponse(BaseModel):
    group_id: str
    user_id: str
    user_email: str
```

- [ ] **Step 2: Commit.**

```bash
git add backend/app/schemas/groups.py
git commit -m "feat(schemas): group create/response + membership"
```

### Task 3.2: Groups router (developer-gated CRUD + membership)

**Files:**
- Create: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/app/routers/groups.py`
- Modify: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/app/main.py`

**Interfaces:**
- Produces: `POST /groups`, `GET /groups`, `DELETE /groups/{group_id}`, `POST /groups/{group_id}/members`, `DELETE /groups/{group_id}/members/{user_id}` — all `get_current_developer`-gated. Deleting a group nulls its KBs' `group_id` (via FK `SET NULL`) → those KBs become unsearchable.

> **Bootstrapping note:** groups are created by a **developer**-role user (`User.role == "developer"`, verified as a real column value). A plain user cannot create a KB until a developer has created a group and (per Task 3.3) the plain user supplies its `group_id`. This ordering is intentional: group provisioning is an admin operation.

- [ ] **Step 1: Write failing router test** — `backend/tests/test_groups_router.py` (uses the local-client + `seed_user`/`auth_header_for` pattern):

```python
def test_create_group_and_add_member(db_session, app_client, seed_user, auth_header_for):
    dev = seed_user(db_session, email="dev@x.com", role="developer")
    member = seed_user(db_session, email="member@x.com", role="user")
    r = app_client.post("/groups", json={"code": "team-1", "name": "Team 1"},
                        headers=auth_header_for(dev))
    assert r.status_code == 201
    gid = r.json()["id"]
    r2 = app_client.post(f"/groups/{gid}/members", json={"user_email": "member@x.com"},
                         headers=auth_header_for(dev))
    assert r2.status_code == 201
    assert r2.json()["user_id"] == str(member.id)

def test_group_routes_forbidden_for_plain_user(db_session, app_client, seed_user, auth_header_for):
    user = seed_user(db_session, email="plain@x.com", role="user")
    r = app_client.post("/groups", json={"code": "x", "name": "x"}, headers=auth_header_for(user))
    assert r.status_code == 403
```

> `seed_user`/`auth_header_for` are the real conftest fixtures. If their exact signatures differ (e.g. `seed_user(email=..., role=...)` without the session arg because they close over `db_session`), match the real signature — inspect `conftest.py` first.

- [ ] **Step 2: Run — expect fail.**

Run: `pytest backend/tests/test_groups_router.py -v`
Expected: FAIL (404 — router not registered).

- [ ] **Step 3: Implement the router.** Reuse the existing email→user resolver from the shares router (`_resolve_user_by_email(session, email)` in `app/routers/shares.py:29`, which **raises HTTP 404** on a missing user — so no `if user is None` branch is needed):

```python
# app/routers/groups.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.dependencies import get_db, get_current_developer
from app.repositories import SqlGroupsRepo, SqlGroupMembersRepo
from app.routers.shares import _resolve_user_by_email
from app.schemas.groups import (
    GroupCreateRequest, GroupResponse, GroupMemberRequest, GroupMemberResponse,
)

router = APIRouter(prefix="/groups", tags=["groups"])


@router.post("", status_code=201, response_model=GroupResponse)
def create_group(body: GroupCreateRequest, session: Session = Depends(get_db),
                 _dev=Depends(get_current_developer)):
    repo = SqlGroupsRepo(session)
    if repo.get_by_code(body.code):
        raise HTTPException(409, "group code already exists")
    g = repo.create(body.code, body.name)
    return GroupResponse(id=str(g.id), code=g.code, name=g.name)


@router.get("", response_model=list[GroupResponse])
def list_groups(session: Session = Depends(get_db), _dev=Depends(get_current_developer)):
    return [GroupResponse(id=str(g.id), code=g.code, name=g.name)
            for g in SqlGroupsRepo(session).list()]


@router.delete("/{group_id}", status_code=204)
def delete_group(group_id: str, session: Session = Depends(get_db),
                 _dev=Depends(get_current_developer)):
    # FK ON DELETE SET NULL nulls any KB.group_id → those KBs become unsearchable.
    SqlGroupsRepo(session).delete(group_id)


@router.post("/{group_id}/members", status_code=201, response_model=GroupMemberResponse)
def add_member(group_id: str, body: GroupMemberRequest, session: Session = Depends(get_db),
               _dev=Depends(get_current_developer)):
    if SqlGroupsRepo(session).get(group_id) is None:
        raise HTTPException(404, "group not found")
    user = _resolve_user_by_email(session, body.user_email)  # raises 404 if missing
    SqlGroupMembersRepo(session).add(group_id, str(user.id))
    return GroupMemberResponse(group_id=group_id, user_id=str(user.id), user_email=user.email)


@router.delete("/{group_id}/members/{user_id}", status_code=204)
def remove_member(group_id: str, user_id: str, session: Session = Depends(get_db),
                  _dev=Depends(get_current_developer)):
    SqlGroupMembersRepo(session).remove(group_id, user_id)
```

> Verify `_resolve_user_by_email` name/signature in `shares.py` before importing; if it's private-by-convention and you prefer not to cross-import a soon-to-be-retired module, lift it into `app/repositories.py` as `SqlUserRepo.get_by_email` and import from there instead. Register in `app/main.py`: `from app.routers.groups import router as groups_router` + `app.include_router(groups_router)`.

- [ ] **Step 4: Run — expect pass.**

Run: `pytest backend/tests/test_groups_router.py -v`
Expected: PASS

- [ ] **Step 5: Commit.**

```bash
git add backend/app/routers/groups.py backend/app/main.py backend/tests/test_groups_router.py
git commit -m "feat(router): /groups CRUD + membership (developer-gated)"
```

### Task 3.3: KB creation requires a group; listing by membership

**Files:**
- Modify: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/app/routers/kb.py:73-165`
- Modify: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/app/schemas/jobs.py:14-40`
- Modify: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/app/repositories.py` (add `SqlKbRepo.list_by_group_ids`)

**Interfaces:**
- Consumes: `SqlGroupsRepo`, `SqlGroupMembersRepo`.
<!-- CONFIRMED 2026-07-07: 아래 "required group_id" 가 최종 정답. 초기 구현의 선택+자동생성은 폐기, 필수+기존그룹 매핑으로 정정됨. 상단 CLARIFIED 배너 참조. -->
- Produces: `POST /kb` gains a **required `group_id` `Form(...)` field** (the endpoint is multipart `Form`, NOT a JSON body — verified `kb.py:73-82`), validated to exist; response `KbCreateResponse` carries `group_id`; `GET /kb` returns KBs whose `group_id ∈ group_ids_for_user(current_user)`.

- [ ] **Step 1: Write failing test** — `backend/tests/test_kb_group_assignment.py` (multipart form via `data=`, response field is `kb_id`):

```python
def test_create_kb_with_group_and_list_by_membership(db_session, app_client, seed_user, auth_header_for):
    dev = seed_user(db_session, email="d@x.com", role="developer")
    user = seed_user(db_session, email="u@x.com", role="user")
    g = app_client.post("/groups", json={"code": "kbteam", "name": "KB Team"},
                        headers=auth_header_for(dev)).json()
    # plain user creates a KB assigned to group g (multipart form fields)
    r = app_client.post("/kb", data={"name": "K", "branch_code": "1", "provider": "kb_pipeline",
                                     "group_id": g["id"]}, headers=auth_header_for(user))
    assert r.status_code == 200, r.text  # POST /kb has no status_code override → 200 (matches test_agents_api.py)
    kb_id = r.json()["kb_id"]
    assert r.json()["group_id"] == g["id"]
    # user is NOT yet a member → GET /kb hides it
    listed = app_client.get("/kb", headers=auth_header_for(user)).json()
    assert all(item["kb_id"] != kb_id for item in listed)
    # add membership → now visible
    app_client.post(f"/groups/{g['id']}/members", json={"user_email": "u@x.com"},
                    headers=auth_header_for(dev))
    listed2 = app_client.get("/kb", headers=auth_header_for(user)).json()
    assert any(item["kb_id"] == kb_id for item in listed2)
```

> Verify the real `POST /kb` form field names (`name`, `branch_code`, `provider`) and the `GET /kb` response item field (`kb_id` vs `id`) against `kb.py` + `schemas/jobs.py` before finalizing; adjust the asserted keys to match.

- [ ] **Step 2: Run — expect fail.**

Run: `pytest backend/tests/test_kb_group_assignment.py -v`
Expected: FAIL (create ignores `group_id`; response lacks it).

- [ ] **Step 3: Add `group_id` to response schemas** in `app/schemas/jobs.py` — add `group_id: str | None = None` to `KbCreateResponse` (line ~14) and `KbSummary` (line ~27).

<!-- CONFIRMED 2026-07-07: Step 4 대로 `group_id: str = Form(...)` (필수)가 최종 구현. 미지정→422,
     존재하지 않는 group_id→422. 자동 기본그룹 생성 없음. 상단 CLARIFIED 배너 참조. -->
- [ ] **Step 4: Add the `group_id` Form field + validation** to `POST /kb` in `kb.py:73-82`. Add parameter `group_id: str = Form(...)` alongside the existing `Form(...)` params, and before constructing the KB:

```python
    if SqlGroupsRepo(session).get(group_id) is None:
        raise HTTPException(status_code=422, detail="group_id does not exist")
```

Then pass `group_id=group_id` into the existing `KnowledgeBase(...)` construction (which already derives `collection_name=f"kb_{kb_id.hex}"` inline at kb.py:120-125 — do NOT invent a helper; keep the existing derivation). Ensure the returned `KbCreateResponse(...)` includes `group_id=str(group_id)`.

- [ ] **Step 5: Add `SqlKbRepo.list_by_group_ids`** to `repositories.py` (2.0 style):

```python
    def list_by_group_ids(self, group_ids: list[str]) -> list[KnowledgeBase]:
        if not group_ids:
            return []
        stmt = select(KnowledgeBase).where(KnowledgeBase.group_id.in_(group_ids))
        return list(self._s.execute(stmt).scalars().all())
```

- [ ] **Step 6: Rewrite `GET /kb`** in `kb.py` (currently `SqlKbRepo(session).list_readable(str(current_user.id))` at ~line 160) to filter by membership:

```python
    member_group_ids = SqlGroupMembersRepo(session).group_ids_for_user(str(current_user.id))
    kbs = SqlKbRepo(session).list_by_group_ids(member_group_ids)
```

Keep the existing response-mapping (each item → `KbSummary`), now including `group_id`.

- [ ] **Step 7: Run — expect pass.**

Run: `pytest backend/tests/test_kb_group_assignment.py -v`
Expected: PASS

- [ ] **Step 8: Commit.**

```bash
git add backend/app/routers/kb.py backend/app/schemas/jobs.py backend/app/repositories.py backend/tests/test_kb_group_assignment.py
git commit -m "feat(kb): create requires group_id (Form); list KBs by group membership"
```

### Task 3.4: Retire the user-to-user shares router (410 Gone)

**Files:**
- Modify: `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend/app/routers/shares.py`

**Interfaces:**
- Produces: the three existing paths (`POST /kb/{kb_id}/shares`, `GET /kb/{kb_id}/shares`, `DELETE /kb/{kb_id}/shares/{user_id}`) each return `410 Gone`, as **three distinct handler functions**, keeping the router's existing `prefix="/kb"` (verified `shares.py:26`). `kb_shares` table stays (dormant).

> **Review fix:** do NOT stack `@router.post("")`+`@router.get("")` on one function (FastAPI keeps only the last decorator) and do NOT change the prefix to `/kb/{kb_id}/shares` (collides with the kb_router shape). Keep three functions with the original path suffixes.

- [ ] **Step 1: Write failing test** — append to `test_groups_router.py`:

```python
def test_legacy_shares_endpoint_gone(db_session, app_client, seed_user, auth_header_for):
    from app.repositories import SqlGroupsRepo
    from tests.conftest import make_kb
    owner = seed_user(db_session, email="sh@x.com", role="user")
    g = SqlGroupsRepo(db_session).create("shg", "S")
    kb = make_kb(db_session, owner_id=owner.id, group_id=g.id)
    r = app_client.post(f"/kb/{kb.id}/shares", json={"shared_with_email": "x@x.com"},
                        headers=auth_header_for(owner))
    assert r.status_code == 410
```

- [ ] **Step 2: Run — expect fail** (currently 201/409/404).

Run: `pytest backend/tests/test_groups_router.py::test_legacy_shares_endpoint_gone -v`
Expected: FAIL

- [ ] **Step 3: Partial edit — replace ONLY the three handler bodies with `raise _GONE`, and KEEP `_resolve_user_by_email` + its imports intact** (Task 3.2's `groups.py` imports `_resolve_user_by_email` from this module, so deleting it breaks `app.main` import). This is NOT a full-file rewrite.

  - Keep the module's existing imports and the `_resolve_user_by_email(session, email)` function (line ~29) exactly as-is.
  - Keep `router = APIRouter(prefix="/kb", ...)` and the three existing route decorators/paths (`POST /{kb_id}/shares`, `GET /{kb_id}/shares`, `DELETE /{kb_id}/shares/{user_id}`), each as a **distinct function**.
  - Add near the top (after imports): `_GONE = HTTPException(status_code=410, detail="user-to-user sharing retired; use groups")` (ensure `HTTPException` is imported).
  - Replace each of the three handler **bodies** so they take no dependencies and immediately `raise _GONE`:

```python
@router.post("/{kb_id}/shares")
def create_share(kb_id: str):
    raise _GONE

@router.get("/{kb_id}/shares")
def list_shares(kb_id: str):
    raise _GONE

@router.delete("/{kb_id}/shares/{user_id}")
def delete_share(kb_id: str, user_id: str):
    raise _GONE
```

  - Delete the now-unused share-specific imports (e.g. `SqlSharesRepo`, share schemas) ONLY IF nothing else in the file references them — but do NOT remove `_resolve_user_by_email`, `HTTPException`, `Session`, `select`, or `User` if `_resolve_user_by_email` still uses them.

> Alternative (cleaner, optional): lift `_resolve_user_by_email` into `app/repositories.py` as `SqlUserRepo.get_by_email`, update Task 3.2's import to use it, then this module keeps nothing but the three 410 stubs. Pick ONE approach; the default above (keep the resolver in `shares.py`) requires the least churn.

- [ ] **Step 4: Run — expect pass.**

Run: `pytest backend/tests/test_groups_router.py::test_legacy_shares_endpoint_gone -v`
Expected: PASS

- [ ] **Step 5: Update/remove old share tests** that asserted 201 grants — change them to expect 410 or delete them. Run the whole suite:

Run: `pytest backend/tests -v`
Expected: PASS

- [ ] **Step 6: Commit.**

```bash
git add backend/app/routers/shares.py backend/tests
git commit -m "feat(shares): retire user-to-user sharing (410 Gone); superseded by groups"
```

---

## Phase 4 — Facade Lockdown (kb-pipeline)

> Closes the bypass hole: today anyone who can reach facade (:19000) and knows a `workspace_id` reads that KB, because facade is unauthenticated. Require a shared secret that only kb-backend holds, so the kb-backend ACL is the sole entry path.

### Task 4.1: Shared-secret gate on the facade

**Files:**
- Modify: `/Users/xxx/workspace/8.kb-pipeline/service/app.py`
- Create: `/Users/xxx/workspace/8.kb-pipeline/service/tests/test_facade_auth.py`

**Interfaces:**
- Produces: `require_facade_key` dependency rejecting requests without a valid `X-Facade-Key` (vs env `KBP_FACADE_KEY`) with 401; applied to the **real stateful route set**: `/search`, `/insert`, `/insert/status`, `/ingest`, `/chunks`, `/doc`, `/communities/build`. When `KBP_FACADE_KEY` is unset the gate is disabled (dev; warn at startup). `/parse` and `/chunk` (stateless, no workspace) stay ungated.

> **Review fix:** the facade has NO `/ingest/submit` and NO `/ingest/status` (removed — see the comment at `service/app.py:12`). Do not reference them. The real decorators are `/healthz, /parse, /chunk, /search, /insert, /insert/status, /ingest, /chunks, /doc, /communities/build`.

- [ ] **Step 1: Write the failing test** — `service/tests/test_facade_auth.py`:

```python
from fastapi.testclient import TestClient

def test_search_rejected_without_key(monkeypatch):
    monkeypatch.setenv("KBP_FACADE_KEY", "s3cret")
    import importlib, service.app as app_mod
    importlib.reload(app_mod)
    client = TestClient(app_mod.app)
    r = client.post("/search", json={"workspace_id": "kb-x", "query": "q"})
    assert r.status_code == 401
    r2 = client.post("/search", json={"workspace_id": "kb-x", "query": "q"},
                     headers={"X-Facade-Key": "s3cret"})
    assert r2.status_code != 401  # passes the gate (may 5xx if edgequake down — fine)

def test_gate_disabled_when_env_unset(monkeypatch):
    monkeypatch.delenv("KBP_FACADE_KEY", raising=False)
    import importlib, service.app as app_mod
    importlib.reload(app_mod)
    client = TestClient(app_mod.app)
    r = client.post("/search", json={"workspace_id": "kb-x", "query": "q"})
    assert r.status_code != 401  # gate disabled
```

- [ ] **Step 2: Run — expect fail.**

Run: `cd /Users/xxx/workspace/8.kb-pipeline && pytest service/tests/test_facade_auth.py -v`
Expected: FAIL (no gate → not 401).

- [ ] **Step 3: Implement the dependency** in `service/app.py` (read the env at module scope so `importlib.reload` picks up `monkeypatch`):

```python
import os
from fastapi import Header, HTTPException, Depends

_FACADE_KEY = os.environ.get("KBP_FACADE_KEY")

def require_facade_key(x_facade_key: str | None = Header(default=None)):
    if _FACADE_KEY is None:
        return  # gate disabled (dev)
    if x_facade_key != _FACADE_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-Facade-Key")
```

Add `dependencies=[Depends(require_facade_key)]` to each of these route decorators: `/search`, `/insert`, `/insert/status`, `/ingest`, `/chunks`, `/doc`, `/communities/build`. Emit a `logging.warning(...)` at startup when `_FACADE_KEY is None`.

- [ ] **Step 4: Run — expect pass.**

Run: `pytest service/tests/test_facade_auth.py -v`
Expected: PASS

- [ ] **Step 5: Ensure existing facade tests still pass** (they run with `KBP_FACADE_KEY` unset → gate disabled → no 401).

Run: `pytest service/tests -v`
Expected: PASS

- [ ] **Step 6: Commit.**

```bash
cd /Users/xxx/workspace/8.kb-pipeline
git add service/app.py service/tests/test_facade_auth.py
git commit -m "feat(facade): X-Facade-Key shared-secret gate on stateful endpoints"
```

### Task 4.2: kb-backend sends the facade key on every facade call

**Files:**
- Modify: kb-backend facade/ingest client + `app/config.py` (locate via grep).

**Interfaces:**
- Consumes: `KBP_FACADE_KEY` from kb-backend settings.
- Produces: every kb-backend→facade request (ingest, insert, insert/status, search-path if it hits facade, chunks, doc-delete, communities) carries `X-Facade-Key`.

- [ ] **Step 1: Locate the facade client(s).**

Run: `cd /Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base && grep -rn "19000\|kb_pipeline\|facade" backend/app --include=*.py | grep -iE "http|client|post|get|url|base"`
Expected: the client module(s) + base URL config. Note: the chat/search path may go through internal provider clients (`EdgequakeClient`) rather than the facade — only the ingest/insert/status/chunks/doc/communities calls hit `:19000`. Gate matching: whatever kb-backend calls on `:19000` must send the header.

- [ ] **Step 2: Add `facade_key: str | None = None`** to `app/config.py` (env `KBP_FACADE_KEY`).

- [ ] **Step 3: Attach the header** on each facade HTTP call: `headers={**existing, "X-Facade-Key": settings.facade_key}` — include only when `settings.facade_key` is set.

- [ ] **Step 4: Verify end-to-end** with both services up and matching `KBP_FACADE_KEY`:

Manual smoke: as a group member, create a KB, upload a doc, and (if search routes via facade) search → all 200. Then `curl -X POST localhost:19000/ingest ...` WITHOUT the header → 401.
Expected: kb-backend path works; direct facade call refused.

- [ ] **Step 5: Commit.**

```bash
git add backend/app/config.py backend/app/<facade_client>.py
git commit -m "feat(kb-backend): send X-Facade-Key on facade calls"
```

---

## Deferred (NOT in this plan) — W4 RLS Hardening

Tracked separately: create a non-superuser edgequake DB role, connect the edgequake app as it, and enable `FORCE ROW LEVEL SECURITY` so workspace isolation is enforced at the DB even if application code regresses. Defense-in-depth for workspace leakage; orthogonal to the group ACL (fully enforced in kb-backend by this plan). Do not start it here.

---

## Self-Review

- **Spec coverage:** Req1 (KB↔group, now 1:1) → Task 1.2/1.3 (`group_id` FK). Req2 (non-members blocked) → Task 2.2/2.3 (ACL, all call sites) + Phase 4 (facade lockdown). Req3 (group↔user mapping + KB created with group) → Task 1.1 (`group_members`), Task 3.2 (membership API), Task 3.3 (KB create with `group_id`). Req4 (logged-in user's groups drive access) → Task 2.3/3.3 (`group_ids_for_user`). "그룹 삭제→검색 불가" → `SET NULL` FK + `can_read_kb` NULL guard (Global Constraints + Task 1.2/3.2). DB consolidation → Phase 0. All covered.
- **Review items resolved (v1→v2):** (1) `Base` from `app.db`; (2) repos use 2.0 `select`; (3) reuse `_resolve_user_by_email` (raises), no fictional `SqlUserRepo`; (4) Task 2.3 migrates agents/comparison/jobs + grep-proof; (5/6) `POST /kb` is `Form(...)`, response field `kb_id`, keep inline `collection_name` derivation, add `group_id`; (7) add `SqlKbRepo.list_by_group_ids`; (8) tests use `db_session`/`seed_user`/`auth_header_for` + local get_db override, add `make_group`/`make_kb`; (9) gate the real route set (no `/ingest/submit`,`/ingest/status`); (10) launcher edits pinned to block content; (11) shares retirement = three distinct 410 handlers, keep `prefix="/kb"`; (12) FK `SET NULL` makes group-deletion→unsearchable coherent; (13) update `config.py` default DSN, not just `.env`.
- **Review items resolved (v2→v3):** (A) Task 3.4 Step 3 is now an explicit PARTIAL edit that KEEPS `_resolve_user_by_email` (deleting it broke Task 3.2's import); (B) HTTP tests use a proper `app_client` fixture with `app.dependency_overrides.clear()` teardown (the leaking `lambda` override contaminated later tests) — added to Task 2.1, and all Task 3.2/3.3/3.4 tests updated; (C) Task 1.2 wording fixed — no `TYPE_CHECKING` block exists; use string annotation + `# noqa: F821`, no runtime import; (D) named the 8 shares/`can_read_kb`-referencing test files that need rewrite/fixture updates (Global Constraints), so the "whole suite → PASS" gates are achievable.
- **Review item resolved (v3→v4):** `POST /kb` has no `status_code` override → returns **200** (confirmed `kb.py:72` + `test_agents_api.py:36`). Task 3.3's create test now asserts `200`, not `201` (route behavior left unchanged).
- **Open confirmations for the implementer (verify against real code before finalizing each task):** `seed_user`/`auth_header_for` exact signatures (conftest); `_resolve_user_by_email` name/visibility (shares.py:29); `POST /kb` real Form field names + `KbCreateResponse`/`KbSummary` field names (kb.py, jobs.py); `SqlKbRepo` class name + where `select` is imported (repositories.py); the `TimestampMixin` default mechanism (server_default vs python default) for the migration backfill.
