<!-- plan-version: v4 -->
<!-- codex-validation: READY v4 at 2026-07-06T03:56:56Z (ultracode competitive validation — 4-lens panel × 3 rounds: 9→3→1→0 must-fix; codex backend hung, per-pref substitute) -->
<!-- Cross-repo: plan lives in kb-pipeline; implementation targets kb-backend (/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base) backend + frontend. -->

<!-- v3→v4: removed an orphan File-Structure clause (add_member stays on _resolve_user_by_email). -->

# Group Management Admin UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A developer-only admin UI to create/delete groups, assign/remove users to groups (via a user picker), and assign/move/unassign KBs to groups (group-centric, KB↔group 1:1), backed by new kb-backend endpoints.

**Architecture:** Additive on top of the merged group-access-control feature. Backend adds developer-gated read/assign endpoints in kb-backend (`app/routers/`), plus repo methods. Frontend adds two Next.js App Router routes under `/admin/groups` mirroring existing pure-CSS patterns (`app/kb/page.tsx` list/form, `components/ShareModal.tsx` modal/list, `lib/api.ts` `request<T>`), gates the nav on `role==='developer'`, and retires the user-to-user `ShareModal`.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 (`select(...)`) + Alembic; pytest (sqlite in-memory). Next.js 14 App Router (App dir), TypeScript, pure CSS (`app/globals.css`), no test framework (typecheck+build+manual smoke).

## Global Constraints

- **Repos:** backend = `/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base` (paths below prefixed `backend/…` for the FastAPI app, `frontend/…` for Next.js). Work on branch `feat/group-access-control` (already checked out; the running dev process is on it).
- **Access:** every new backend endpoint is gated by `get_current_developer` (`backend/app/dependencies.py`, checks `User.role == "developer"`). Frontend nav/route gating on `user.role === 'developer'` is UX only; the backend gate is the real boundary.
- **Model invariants:** KB↔group = 1:1 (`knowledge_bases.group_id`, `ON DELETE SET NULL`); user↔group = M:N (`group_members`). Assigning a KB to a group SETS `group_id` (moves it from any prior group). A KB with `group_id IS NULL` is unsearchable (owner included) until reassigned.
- **Backend SQLAlchemy style:** 2.0 — `select(...)`, `session.execute(...).scalars()`, `session.get(...)`. Do NOT use legacy `.query(...)`.
- **Backend test conventions (verified — CRITICAL):** sqlite in-memory + `Base.metadata.create_all` (`backend/tests/conftest.py:84,102`). **The only real pytest FIXTURES are `db_session` and `app_client`.** `seed_user` (conftest.py:115), `auth_header_for` (conftest.py:132), `make_group`, `make_kb` are **plain module-level FUNCTIONS, NOT fixtures** — they must be IMPORTED and CALLED, never put in a test's parameter list. Every new test file MUST start with `from .conftest import seed_user, auth_header_for, make_group, make_kb` (mirror `backend/tests/test_agents_api.py:15`). Signatures: `seed_user(db_session, *, email=..., role="user", password=...)` (keyword-only email/role) → returns User; `auth_header_for(user)` → headers dict; `make_group(db_session, code, name)`; `make_kb(db_session, owner_id, group_id, ...)`. Test defs take only `(db_session, app_client)`. Run pytest via `.venv/bin/python -m pytest` (bare `python`/`pytest` are not on PATH).
- **Frontend has NO test framework** (package.json scripts: dev/build/start/lint/typecheck only). The gate for every frontend task is `npm run typecheck` (`tsc --noEmit`) + `npm run build`, plus the documented manual smoke. Do NOT add jest/vitest/testing-library — out of scope (YAGNI).
- **Frontend patterns:** API via `request<T>(path, opts)` + `ApiError` (`frontend/lib/api.ts:126`), base `/api/backend`, bearer token auto-attached. Types in `frontend/lib/types.ts`. Styling = pure CSS classes from `frontend/app/globals.css` (`.card`, `.field`, `.badge`, `.row`, `.spread`, `.grid`, `.cols-2`, `.modal-overlay`, `.modal`, `.error-banner`, `.empty`, `.muted`). Nav is defined in `frontend/components/AuthGate.tsx` (nav items array near line 60; `roleLabel` near line 119). Import alias `@/…` — confirm `tsconfig.json` `paths` maps `@/*`; if not, use relative imports.
- **AuthGate is PER-PAGE, not global (verified):** `app/layout.tsx` renders only `{children}` — there is NO global AuthGate. Each page wraps its own JSX in `<AuthGate>…</AuthGate>` (see `app/kb/page.tsx`). **Every new page under `/admin/groups` MUST wrap its body in `<AuthGate>` itself**, or it renders unauthenticated AND the developer redirect guard (which lives inside AuthGate) never runs. **AuthGate is a NAMED export** (`AuthGate.tsx:70` `export function AuthGate`) — import as `import { AuthGate } from "@/components/AuthGate"` (never a default import).
- **Two-phase:** Phase A (backend endpoints) is independently testable via pytest. Phase B (frontend) consumes Phase A; verify Phase A green before Phase B.

---

## File Structure

### Backend (`backend/`)
- **Modify** `app/repositories.py` — add `SqlGroupMembersRepo.list_members`, `SqlKbRepo.list_all` + `SqlKbRepo.set_group`, new `SqlUserRepo` (`list`, `get_by_email`).
- **Create** `app/routers/users.py` — `GET /users` (developer).
- **Modify** `app/routers/groups.py` — add `GET /groups/{id}/members`, `GET /groups/{id}/kbs`, `PUT /groups/{id}/kbs/{kb_id}`, `DELETE /groups/{id}/kbs/{kb_id}`. Leave the existing `add_member` on `_resolve_user_by_email` (`shares.py`, raises 404 on missing) — do NOT rewrite that working path.
- **Create** `app/routers/admin.py` — `GET /admin/kbs` (developer, all KBs + current group).
- **Modify** `app/main.py` — register `users_router`, `admin_router`.
- **Create** `app/schemas/users.py` — `UserListItem`.
- **Modify** `app/schemas/groups.py` — `GroupMemberListItem`, `GroupKbItem`, `AdminKbItem`.
- **Create** `backend/tests/test_group_admin_api.py` — endpoint tests.

### Frontend (`frontend/`)
- **Modify** `lib/types.ts` — `GroupResponse`, `GroupMemberListItem`, `GroupKbItem`, `AdminKbItem`, `UserListItem`.
- **Modify** `lib/api.ts` — add group-admin API functions; remove shares functions.
- **Create** `app/admin/groups/page.tsx` — group list + create + delete.
- **Create** `app/admin/groups/[groupId]/page.tsx` — group detail (members panel + KB panel).
- **Create** `components/GroupMembersPanel.tsx`, `components/GroupKbsPanel.tsx` — the two detail panels.
- **Modify** `components/AuthGate.tsx` — add `/admin/groups` nav item gated on `role==='developer'`; fix `roleLabel` `admin`→`developer`; redirect non-developer away from `/admin/*`.
- **Delete** `components/ShareModal.tsx`; **Modify** `app/kb/[kbId]/page.tsx` (remove ShareModal usage/button); **Modify** `lib/api.ts` (remove `listShares/createShare/revokeShare`).

---

## Phase A — Backend Endpoints

### Task A1: Repository methods

**Files:**
- Modify: `backend/app/repositories.py`
- Test: `backend/tests/test_group_admin_api.py`

**Interfaces:**
- Produces: `SqlGroupMembersRepo.list_members(group_id) -> list[tuple[User]]` (returns `User` rows in the group); `SqlKbRepo.list_all() -> list[KnowledgeBase]`; `SqlKbRepo.set_group(kb_id, group_id: str | None) -> KnowledgeBase | None`; `SqlUserRepo(session).list() -> list[User]`, `.get_by_email(email) -> User | None`.

- [ ] **Step 1: Write failing test** — `backend/tests/test_group_admin_api.py`. **Start the file with the conftest function import** (these are functions, not fixtures):

```python
from .conftest import seed_user, auth_header_for, make_group, make_kb


def test_repos_members_kbs_users(db_session):
    from app.repositories import SqlGroupsRepo, SqlGroupMembersRepo, SqlKbRepo, SqlUserRepo
    u = seed_user(db_session, email="a@x.com")
    g = SqlGroupsRepo(db_session).create("g1", "G1")
    SqlGroupMembersRepo(db_session).add(str(g.id), str(u.id))
    kb = make_kb(db_session, owner_id=u.id, group_id=g.id)
    members = SqlGroupMembersRepo(db_session).list_members(str(g.id))
    assert [m.email for m in members] == ["a@x.com"]
    assert str(kb.id) in [str(k.id) for k in SqlKbRepo(db_session).list_all()]
    moved = SqlKbRepo(db_session).set_group(str(kb.id), None)
    assert moved.group_id is None
    assert SqlUserRepo(db_session).get_by_email("a@x.com").id == u.id
    assert "a@x.com" in [x.email for x in SqlUserRepo(db_session).list()]
```

- [ ] **Step 2: Run — expect fail.**

Run: `cd /Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base && .venv/bin/python -m pytest backend/tests/test_group_admin_api.py::test_repos_members_kbs_users -v`
Expected: FAIL (`AttributeError`/`ImportError`).

- [ ] **Step 3: Implement** in `app/repositories.py` (2.0 style; `select` already imported):

```python
from app.models.user import User  # if not already imported


class SqlUserRepo:
    def __init__(self, session): self._s = session
    def list(self) -> list[User]:
        return list(self._s.execute(select(User).order_by(User.email)).scalars().all())
    def get_by_email(self, email: str) -> User | None:
        return self._s.execute(select(User).where(User.email == email)).scalars().first()
```

Add to `SqlGroupMembersRepo`:

```python
    def list_members(self, group_id: str) -> list[User]:
        stmt = (select(User)
                .join(GroupMember, GroupMember.user_id == User.id)
                .where(GroupMember.group_id == group_id)
                .order_by(User.email))
        return list(self._s.execute(stmt).scalars().all())
```

Add to `SqlKbRepo` (match its existing class name + `self._s`):

```python
    def list_all(self) -> list[KnowledgeBase]:
        return list(self._s.execute(select(KnowledgeBase).order_by(KnowledgeBase.name)).scalars().all())

    def set_group(self, kb_id: str, group_id: str | None) -> KnowledgeBase | None:
        # NULL-safe: never pass None into uuid.UUID(...). group_id may be None (unassign).
        kb = self._s.get(KnowledgeBase, uuid.UUID(str(kb_id)))
        if kb is None:
            return None
        kb.group_id = uuid.UUID(str(group_id)) if group_id is not None else None
        self._s.commit(); self._s.refresh(kb)
        return kb
```

> Verify `SqlKbRepo` is the real class name and that `uuid`, `select`, `KnowledgeBase`, `GroupMember`, `User` are imported at the top of `repositories.py`; add missing imports. If the file already has a `_to_uuid(val)` helper, reuse it for `kb_id` — but for `group_id` you MUST guard `None` first (`_to_uuid` does `uuid.UUID(str(val))` → crashes on `None`).

- [ ] **Step 4: Run — expect pass.**

Run: `.venv/bin/python -m pytest backend/tests/test_group_admin_api.py::test_repos_members_kbs_users -v`
Expected: PASS

- [ ] **Step 5: Commit.**

```bash
git add backend/app/repositories.py backend/tests/test_group_admin_api.py
git commit -m "feat(repo): list_members, list_all/set_group, SqlUserRepo for group admin"
```

### Task A2: `GET /users` (developer)

**Files:**
- Create: `backend/app/schemas/users.py`, `backend/app/routers/users.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_group_admin_api.py`

**Interfaces:**
- Produces: `GET /users` → `list[UserListItem{id, email, display_name, role}]`, `get_current_developer`-gated.

- [ ] **Step 1: Write failing test:**

```python
def test_get_users_developer_only(db_session, app_client):
    dev = seed_user(db_session, email="dev@x.com", role="developer")
    plain = seed_user(db_session, email="p@x.com", role="user")
    assert app_client.get("/users", headers=auth_header_for(plain)).status_code == 403
    r = app_client.get("/users", headers=auth_header_for(dev))
    assert r.status_code == 200
    emails = {u["email"] for u in r.json()}
    assert {"dev@x.com", "p@x.com"} <= emails
    assert set(r.json()[0].keys()) == {"id", "email", "display_name", "role"}
```

- [ ] **Step 2: Run — expect fail** (404: router not registered).

Run: `.venv/bin/python -m pytest backend/tests/test_group_admin_api.py::test_get_users_developer_only -v`

- [ ] **Step 3: Implement.** `app/schemas/users.py`:

```python
from pydantic import BaseModel

class UserListItem(BaseModel):
    id: str
    email: str
    display_name: str | None
    role: str
```

`app/routers/users.py`:

```python
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.dependencies import get_db, get_current_developer
from app.repositories import SqlUserRepo
from app.schemas.users import UserListItem

router = APIRouter(prefix="/users", tags=["users"])

@router.get("", response_model=list[UserListItem])
def list_users(session: Session = Depends(get_db), _dev=Depends(get_current_developer)):
    return [UserListItem(id=str(u.id), email=u.email,
                         display_name=u.display_name, role=u.role)
            for u in SqlUserRepo(session).list()]
```

Register in `app/main.py`: `from app.routers.users import router as users_router` + `app.include_router(users_router)`.

- [ ] **Step 4: Run — expect pass.**
- [ ] **Step 5: Commit.**

```bash
git add backend/app/schemas/users.py backend/app/routers/users.py backend/app/main.py backend/tests/test_group_admin_api.py
git commit -m "feat(users): GET /users (developer-gated) for group member picker"
```

### Task A3: `GET /groups/{id}/members` + `GET /groups/{id}/kbs`

**Files:**
- Modify: `backend/app/routers/groups.py`, `backend/app/schemas/groups.py`
- Test: `backend/tests/test_group_admin_api.py`

**Interfaces:**
- Produces: `GET /groups/{id}/members` → `list[GroupMemberListItem{user_id, email, display_name}]`; `GET /groups/{id}/kbs` → `list[GroupKbItem{kb_id, name, branch_code, status}]`. Both `get_current_developer`-gated, 404 if group missing.

- [ ] **Step 1: Write failing test:**

```python
def test_group_members_and_kbs(db_session, app_client):
    from app.repositories import SqlGroupsRepo, SqlGroupMembersRepo
    dev = seed_user(db_session, email="d3@x.com", role="developer")
    m = seed_user(db_session, email="m3@x.com")
    g = SqlGroupsRepo(db_session).create("g3", "G3")
    SqlGroupMembersRepo(db_session).add(str(g.id), str(m.id))
    make_kb(db_session, owner_id=m.id, group_id=g.id, name="KB-A")
    h = auth_header_for(dev)
    mem = app_client.get(f"/groups/{g.id}/members", headers=h).json()
    assert [x["email"] for x in mem] == ["m3@x.com"]
    kbs = app_client.get(f"/groups/{g.id}/kbs", headers=h).json()
    assert [x["name"] for x in kbs] == ["KB-A"]
    assert app_client.get("/groups/00000000-0000-0000-0000-000000000000/members", headers=h).status_code == 404
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement.** Add to `app/schemas/groups.py`:

```python
class GroupMemberListItem(BaseModel):
    user_id: str
    email: str
    display_name: str | None

class GroupKbItem(BaseModel):
    kb_id: str
    name: str
    branch_code: int
    status: str
```

Add to `app/routers/groups.py` (reuse `SqlGroupsRepo`, `SqlGroupMembersRepo`, add `SqlKbRepo` import; `_group_or_404` helper):

```python
from app.repositories import SqlKbRepo
from app.schemas.groups import GroupMemberListItem, GroupKbItem

def _group_or_404(session, group_id):
    g = SqlGroupsRepo(session).get(group_id)
    if g is None:
        raise HTTPException(404, "group not found")
    return g

@router.get("/{group_id}/members", response_model=list[GroupMemberListItem])
def list_group_members(group_id: str, session: Session = Depends(get_db), _dev=Depends(get_current_developer)):
    _group_or_404(session, group_id)
    return [GroupMemberListItem(user_id=str(u.id), email=u.email, display_name=u.display_name)
            for u in SqlGroupMembersRepo(session).list_members(group_id)]

@router.get("/{group_id}/kbs", response_model=list[GroupKbItem])
def list_group_kbs(group_id: str, session: Session = Depends(get_db), _dev=Depends(get_current_developer)):
    _group_or_404(session, group_id)
    kbs = [k for k in SqlKbRepo(session).list_all() if str(k.group_id) == str(group_id)]
    return [GroupKbItem(kb_id=str(k.id), name=k.name, branch_code=k.branch_code, status=k.status) for k in kbs]
```

- [ ] **Step 4: Run — expect pass.**
- [ ] **Step 5: Commit.**

```bash
git add backend/app/routers/groups.py backend/app/schemas/groups.py backend/tests/test_group_admin_api.py
git commit -m "feat(groups): GET members + kbs list (developer-gated)"
```

### Task A4: `GET /admin/kbs` (all KBs + current group)

**Files:**
- Create: `backend/app/routers/admin.py`
- Modify: `backend/app/schemas/groups.py`, `backend/app/main.py`
- Test: `backend/tests/test_group_admin_api.py`

**Interfaces:**
- Produces: `GET /admin/kbs` → `list[AdminKbItem{kb_id, name, group_id, group_code, owner_email}]`, developer-gated. `group_id`/`group_code` null when unassigned.

- [ ] **Step 1: Write failing test:**

```python
def test_admin_kbs_lists_all_with_group(db_session, app_client):
    from app.repositories import SqlGroupsRepo
    dev = seed_user(db_session, email="d4@x.com", role="developer")
    owner = seed_user(db_session, email="o4@x.com")
    g = SqlGroupsRepo(db_session).create("g4", "G4")
    make_kb(db_session, owner_id=owner.id, group_id=g.id, name="Assigned")
    make_kb(db_session, owner_id=owner.id, group_id=None, name="Unassigned")
    rows = app_client.get("/admin/kbs", headers=auth_header_for(dev)).json()
    by = {r["name"]: r for r in rows}
    assert by["Assigned"]["group_code"] == "g4"
    assert by["Unassigned"]["group_id"] is None
    assert by["Assigned"]["owner_email"] == "o4@x.com"
```

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement.** Add to `app/schemas/groups.py`:

```python
class AdminKbItem(BaseModel):
    kb_id: str
    name: str
    group_id: str | None
    group_code: str | None
    owner_email: str | None
```

`app/routers/admin.py`:

```python
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.dependencies import get_db, get_current_developer
from app.repositories import SqlKbRepo
from app.schemas.groups import AdminKbItem

router = APIRouter(prefix="/admin", tags=["admin"])

@router.get("/kbs", response_model=list[AdminKbItem])
def list_all_kbs(session: Session = Depends(get_db), _dev=Depends(get_current_developer)):
    out = []
    for k in SqlKbRepo(session).list_all():
        # use ORM relationships (KnowledgeBase.owner, .group) — NOT reflection.
        out.append(AdminKbItem(
            kb_id=str(k.id), name=k.name,
            group_id=str(k.group_id) if k.group_id is not None else None,
            group_code=k.group.code if k.group is not None else None,
            owner_email=k.owner.email if k.owner is not None else None,
        ))
    return out
```

> `KnowledgeBase.owner` and `KnowledgeBase.group` are real relationships (`backend/app/models/knowledge_base.py`: `owner: Mapped["User"]`, `group: Mapped["Group | None"]`). Access is lazy (fine at admin scale). To avoid N+1 on a large listing, make `SqlKbRepo.list_all()` eager-load: `select(KnowledgeBase).options(selectinload(KnowledgeBase.owner), selectinload(KnowledgeBase.group)).order_by(KnowledgeBase.name)` (`from sqlalchemy.orm import selectinload`). Register in `app/main.py`: `from app.routers.admin import router as admin_router` + `app.include_router(admin_router)`.

- [ ] **Step 4: Run — expect pass.**
- [ ] **Step 5: Commit.**

```bash
git add backend/app/routers/admin.py backend/app/schemas/groups.py backend/app/main.py backend/tests/test_group_admin_api.py
git commit -m "feat(admin): GET /admin/kbs (all KBs + current group, developer-gated)"
```

### Task A5: Assign / move / unassign KB ↔ group

**Files:**
- Modify: `backend/app/routers/groups.py`
- Test: `backend/tests/test_group_admin_api.py`

**Interfaces:**
- Produces: `PUT /groups/{group_id}/kbs/{kb_id}` → `{kb_id, group_id}` (sets `group_id`; moves from any prior group; 404 if group or kb missing; idempotent). `DELETE /groups/{group_id}/kbs/{kb_id}` → 204 (sets `group_id=NULL` only if the KB is currently in this group; else 404).

- [ ] **Step 1: Write failing test:**

```python
def test_assign_move_unassign_kb(db_session, app_client):
    from app.repositories import SqlGroupsRepo, SqlKbRepo
    dev = seed_user(db_session, email="d5@x.com", role="developer")
    owner = seed_user(db_session, email="o5@x.com")
    g1 = SqlGroupsRepo(db_session).create("g5a", "G5A")
    g2 = SqlGroupsRepo(db_session).create("g5b", "G5B")
    kb = make_kb(db_session, owner_id=owner.id, group_id=g1.id, name="Mover")
    h = auth_header_for(dev)
    # move g1 -> g2
    r = app_client.put(f"/groups/{g2.id}/kbs/{kb.id}", headers=h)
    assert r.status_code == 200 and r.json()["group_id"] == str(g2.id)
    assert str(SqlKbRepo(db_session).get(str(kb.id)).group_id) == str(g2.id)
    # unassign from g2
    assert app_client.delete(f"/groups/{g2.id}/kbs/{kb.id}", headers=h).status_code == 204
    assert SqlKbRepo(db_session).get(str(kb.id)).group_id is None
    # unassign from a group it is NOT in -> 404
    assert app_client.delete(f"/groups/{g1.id}/kbs/{kb.id}", headers=h).status_code == 404
    # assign to missing group -> 404
    assert app_client.put(f"/groups/00000000-0000-0000-0000-000000000000/kbs/{kb.id}", headers=h).status_code == 404
```

> Confirm `SqlKbRepo(session).get(kb_id)` exists (used by `get_readable_kb`). If the repo's getter has a different name, match it.

- [ ] **Step 2: Run — expect fail.**

- [ ] **Step 3: Implement** in `app/routers/groups.py`:

```python
@router.put("/{group_id}/kbs/{kb_id}")
def assign_kb(group_id: str, kb_id: str, session: Session = Depends(get_db), _dev=Depends(get_current_developer)):
    _group_or_404(session, group_id)
    kb = SqlKbRepo(session).set_group(kb_id, group_id)
    if kb is None:
        raise HTTPException(404, "kb not found")
    return {"kb_id": str(kb.id), "group_id": str(kb.group_id)}

@router.delete("/{group_id}/kbs/{kb_id}", status_code=204)
def unassign_kb(group_id: str, kb_id: str, session: Session = Depends(get_db), _dev=Depends(get_current_developer)):
    _group_or_404(session, group_id)
    kb = SqlKbRepo(session).get(kb_id)
    # guard NULL explicitly BEFORE any str()/uuid conversion (group_id is nullable).
    if kb is None or kb.group_id is None or str(kb.group_id) != str(group_id):
        raise HTTPException(404, "kb not in this group")
    SqlKbRepo(session).set_group(kb_id, None)
```

- [ ] **Step 4: Run — expect pass.**
- [ ] **Step 5: Run the whole new suite + confirm no regressions.**

Run: `.venv/bin/python -m pytest backend/tests/test_group_admin_api.py -v && .venv/bin/python -m pytest backend/tests -q 2>&1 | tail -5`
Expected: new suite green; full suite has only the 11 pre-existing docguard-gate failures (unchanged).

- [ ] **Step 6: Commit.**

```bash
git add backend/app/routers/groups.py backend/tests/test_group_admin_api.py
git commit -m "feat(groups): PUT/DELETE kb assignment (1:1 move / unassign, developer-gated)"
```

---

## Phase B — Frontend (`frontend/`)

> Gate for every task: `cd frontend && npm run typecheck && npm run build` must pass. Manual smoke uses the running dev backend (`:8088`) via the dev server (`npm run dev`, `:4001`). No test framework.

### Task B1: Types + API functions; remove shares

**Files:**
- Modify: `frontend/lib/types.ts`, `frontend/lib/api.ts`

**Interfaces:**
- Produces: TS types + `request<T>`-based functions: `listGroups()`, `createGroup({code,name})`, `deleteGroup(id)`, `getGroupMembers(id)`, `addGroupMember(id, email)`, `removeGroupMember(id, userId)`, `getGroupKbs(id)`, `assignKbToGroup(groupId, kbId)`, `unassignKbFromGroup(groupId, kbId)`, `listAllKbsAdmin()`, `listUsers()`. Removes `listShares/createShare/revokeShare`.

- [ ] **Step 1: Add types** to `lib/types.ts`:

```typescript
export interface GroupResponse { id: string; code: string; name: string; }
export interface GroupMemberListItem { user_id: string; email: string; display_name: string | null; }
export interface GroupKbItem { kb_id: string; name: string; branch_code: number; status: string; }
export interface AdminKbItem { kb_id: string; name: string; group_id: string | null; group_code: string | null; owner_email: string | null; }
export interface UserListItem { id: string; email: string; display_name: string | null; role: string; }
```

- [ ] **Step 2: Add API functions** to `lib/api.ts` (mirror the existing `request<T>` usage at `listKbs`/`createKb`):

```typescript
export const listGroups = () => request<GroupResponse[]>("/groups");
export const createGroup = (input: { code: string; name: string }) =>
  request<GroupResponse>("/groups", { method: "POST", body: JSON.stringify(input), headers: { "Content-Type": "application/json" } });
export const deleteGroup = (id: string) => request<void>(`/groups/${id}`, { method: "DELETE" });
export const getGroupMembers = (id: string) => request<GroupMemberListItem[]>(`/groups/${id}/members`);
export const addGroupMember = (id: string, user_email: string) =>
  request<unknown>(`/groups/${id}/members`, { method: "POST", body: JSON.stringify({ user_email }), headers: { "Content-Type": "application/json" } });
export const removeGroupMember = (id: string, userId: string) => request<void>(`/groups/${id}/members/${userId}`, { method: "DELETE" });
export const getGroupKbs = (id: string) => request<GroupKbItem[]>(`/groups/${id}/kbs`);
export const assignKbToGroup = (groupId: string, kbId: string) => request<{ kb_id: string; group_id: string }>(`/groups/${groupId}/kbs/${kbId}`, { method: "PUT" });
export const unassignKbFromGroup = (groupId: string, kbId: string) => request<void>(`/groups/${groupId}/kbs/${kbId}`, { method: "DELETE" });
export const listAllKbsAdmin = () => request<AdminKbItem[]>("/admin/kbs");
export const listUsers = () => request<UserListItem[]>("/users");
```

> Verify the exact `request<T>` options shape (method/body/headers) against `lib/api.ts:126` and how `createKb` builds requests; the group create/member-add here send JSON (the backend `POST /groups` + `POST /groups/{id}/members` accept JSON bodies, unlike `POST /kb` which is multipart). Confirm `request` sets `Content-Type` or requires it explicitly, and import the new types.

> **B1 only ADDS** types + new API functions. Do NOT remove the shares functions here — `components/ShareModal.tsx` still imports them, so removing them now would break `npm run typecheck` (B1's own gate). ALL shares removal happens in Task B6 (same commit as the ShareModal deletion).

- [ ] **Step 3: Gate.**

Run: `cd frontend && npm run typecheck`
Expected: passes (new functions/types added; nothing removed → no dangling imports).

- [ ] **Step 4: Commit.**

```bash
git add frontend/lib/types.ts frontend/lib/api.ts
git commit -m "feat(fe/api): group-admin API client functions + types"
```

### Task B2: `/admin/groups` — list + create + delete

**Files:**
- Create: `frontend/app/admin/groups/page.tsx`

**Interfaces:**
- Consumes: `listGroups`, `createGroup`, `deleteGroup`.
- Produces: a client page listing groups (code, name), a create form (code+name), delete with confirm (warns unassigned KBs become unsearchable).

- [ ] **Step 1: Implement the page** mirroring `app/kb/page.tsx` (`"use client"`, `useEffect` load, `.card`/`.field`/`.error-banner`/`.empty` classes, `ApiError` catch → banner). Include:
  - a `CreateGroupForm` (inputs `code`, `name`; submit → `createGroup` → prepend to list; error banner).
  - a list of `.card` rows: `code` + `name` + a link to `/admin/groups/{id}` + a delete button that `window.confirm("이 그룹을 삭제하면 소속 KB 는 그룹이 해제되어(group_id=NULL) 검색 불가가 됩니다. 계속할까요?")` then `deleteGroup`.

```tsx
"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { listGroups, createGroup, deleteGroup } from "@/lib/api";
import type { GroupResponse } from "@/lib/types";
import { AuthGate } from "@/components/AuthGate"; // NAMED export (AuthGate.tsx:70) — NOT a default import
// The page's default export MUST wrap its body in <AuthGate>…</AuthGate> (per-page gate),
// mirroring app/kb/page.tsx:5 — otherwise it renders unauthenticated and the /admin redirect never runs.
```

> Provide the full component body following the exact structure/classes of `app/kb/page.tsx` (CreateKbForm + list), **wrapped in `<AuthGate>`**. AuthGate is a **named export** — `import { AuthGate } from "@/components/AuthGate"` (same as `app/kb/page.tsx:5`). Keep copy in Korean to match the app.

- [ ] **Step 2: Gate.** `cd frontend && npm run typecheck && npm run build` → passes.
- [ ] **Step 3: Manual smoke.** With backend `:8088` up and a developer token: visit `/admin/groups`, create a group, see it listed, delete it. Document result.
- [ ] **Step 4: Commit.**

```bash
git add frontend/app/admin/groups/page.tsx
git commit -m "feat(fe): /admin/groups list + create + delete"
```

### Task B3: Group detail — members panel (user picker)

**Files:**
- Create: `frontend/components/GroupMembersPanel.tsx`, `frontend/app/admin/groups/[groupId]/page.tsx`

**Interfaces:**
- Consumes: `getGroupMembers`, `listUsers`, `addGroupMember`, `removeGroupMember`.
- Produces: a panel showing current members (email/display_name) with remove buttons, and a picker (`<select>` of `listUsers()` excluding current members) + "추가" button → `addGroupMember(groupId, selectedEmail)`.

- [ ] **Step 1: Implement `GroupMembersPanel`** mirroring `components/ShareModal.tsx` list/form pattern (`.spread` rows, `.badge`, danger button, `.error-banner`). Load members + users on mount; picker is a `<select>` whose options are `users.filter(u => !members.some(m => m.user_id === u.id))`. On add: `addGroupMember` then reload members. On remove: `removeGroupMember(groupId, member.user_id)` then reload.
- [ ] **Step 2: Implement the detail page** `app/admin/groups/[groupId]/page.tsx` (`"use client"`, read `params.groupId`) rendering, **wrapped in `<AuthGate>…</AuthGate>`** (per-page gate, same as B2), a header (group code/name) + `<GroupMembersPanel groupId={groupId} />` (KB panel added in B4).

> Full component bodies required; mirror ShareModal's states (loading/error) and globals.css classes.

- [ ] **Step 3: Gate.** typecheck + build pass.
- [ ] **Step 4: Manual smoke.** Add a user to a group via the picker, see them listed, remove them.
- [ ] **Step 5: Commit.**

```bash
git add frontend/components/GroupMembersPanel.tsx frontend/app/admin/groups/[groupId]/page.tsx
git commit -m "feat(fe): group detail members panel (user picker)"
```

### Task B4: Group detail — KB panel (assign/move/unassign)

**Files:**
- Create: `frontend/components/GroupKbsPanel.tsx`
- Modify: `frontend/app/admin/groups/[groupId]/page.tsx` (render the panel)

**Interfaces:**
- Consumes: `getGroupKbs`, `listAllKbsAdmin`, `assignKbToGroup`, `unassignKbFromGroup`.
- Produces: a panel showing KBs currently in the group (with unassign buttons) + a picker of all KBs (`listAllKbsAdmin`, showing each KB's current group_code) + "지정" button. If the picked KB is already in another group, `window.confirm("이 KB 는 현재 그룹 '{group_code}' 에 속합니다. 이 그룹으로 이동할까요?")` before `assignKbToGroup`.

- [ ] **Step 1: Implement `GroupKbsPanel`** (mirror members panel structure). Picker options = `allKbs` (optionally excluding those already in this group). On assign: if `kb.group_id && kb.group_id !== groupId` → confirm move; then `assignKbToGroup(groupId, kb.kb_id)`; reload both lists. On unassign: `unassignKbFromGroup(groupId, kb.kb_id)` (confirm "이 KB 는 그룹 해제 후 검색 불가가 됩니다"), reload.
- [ ] **Step 2: Render** `<GroupKbsPanel groupId={groupId} />` in the detail page below the members panel.
- [ ] **Step 3: Gate.** typecheck + build pass.
- [ ] **Step 4: Manual smoke.** Assign a KB to the group, move a KB from another group (confirm dialog), unassign a KB.
- [ ] **Step 5: Commit.**

```bash
git add frontend/components/GroupKbsPanel.tsx frontend/app/admin/groups/[groupId]/page.tsx
git commit -m "feat(fe): group detail KB panel (assign/move/unassign)"
```

### Task B5: Nav gate (developer) + `/admin` protection

**Files:**
- Modify: `frontend/components/AuthGate.tsx`

**Interfaces:**
- Produces: an `/admin/groups` nav entry shown only when `user.role === 'developer'`; non-developer visiting `/admin/*` is redirected to `/kb`; `roleLabel` handles `developer`.

- [ ] **Step 1: Developer-gated nav entry — computed in the component body, NOT in the module const.** `PRIMARY_NAV` (`AuthGate.tsx:59`) is a module-level const with no access to `user`, so a role-gated item cannot be added there. Inside the AuthGate component body (where `user` is in scope) compute a local list and render THAT:

```tsx
  const nav = user?.role === "developer"
    ? [...PRIMARY_NAV, { label: "그룹 관리", icon: IcoDb, href: "/admin/groups" }]
    : PRIMARY_NAV;
```

Then change the nav render (`AuthGate.tsx:171`) to map over `nav` instead of `PRIMARY_NAV`. Do NOT mutate the module-level const. Confirm the item shape `{ label, icon, href }` matches PRIMARY_NAV entries and that `IcoDb` (or another existing icon) is in scope.

- [ ] **Step 2: Redirect guard — evaluate synchronously in the mount effect against the freshly-read stored user (NOT the `user` state), before `setUser`, and re-run on navigation.** `user` state is `null` on first paint (`AuthGate.tsx:74`) and set only inside the mount effect; deciding from `user` would bounce a legitimate developer on first render. In the existing mount effect (dep array currently `[router]`, `AuthGate.tsx:86`), add the guard and the `pathname` dep:

```tsx
  useEffect(() => {
    // ...existing token check (redirect to /login if missing)...
    const stored = getStoredUser();
    if (pathname.startsWith("/admin") && stored?.role !== "developer") {
      router.replace("/kb");
      return;
    }
    setUser(stored);
    // ...existing setReady + 401-handler registration...
  }, [router, pathname]);
```

`pathname` is from `usePathname()` (already imported, `AuthGate.tsx:4`). ONLY add the stored-user guard + `pathname` dep — keep all existing effect logic (token read, `setReady`, 401 handler) intact.
- [ ] **Step 3: Fix `roleLabel`** — the real line (`AuthGate.tsx:119-120`) is `user?.role === "admin" ? "관리자" : user?.role ?? "사용자"` and has NO `developer` branch (a developer currently sees the literal `"developer"`). Replace with:

```tsx
  const roleLabel =
    user?.role === "developer" ? "개발자"
    : user?.role === "admin" ? "관리자"
    : user?.role ?? "사용자";
```
- [ ] **Step 4: Gate.** typecheck + build pass.
- [ ] **Step 5: Manual smoke.** As a developer, see the nav item + reach `/admin/groups`. As a plain user, the item is hidden and direct `/admin/groups` redirects to `/kb`.
- [ ] **Step 6: Commit.**

```bash
git add frontend/components/AuthGate.tsx
git commit -m "feat(fe): developer-gated /admin/groups nav + route guard"
```

### Task B6: Retire ShareModal

**Files:**
- Delete: `frontend/components/ShareModal.tsx`
- Modify: `frontend/app/kb/[kbId]/page.tsx` (remove ShareModal import/usage/button), `frontend/lib/api.ts` (remove `listShares/createShare/revokeShare` + `ShareResponse` if unused elsewhere)

- [ ] **Step 1: Remove usage.** In `app/kb/[kbId]/page.tsx` delete the ShareModal import, the state that opens it, and the "공유" button/trigger. Grep first: `grep -rn "ShareModal\|listShares\|createShare\|revokeShare" frontend/` and remove every reference.
- [ ] **Step 2: Delete** `components/ShareModal.tsx`; remove the three shares functions (`listShares`, `createShare`, `revokeShare`, near `lib/api.ts:483–499`) AND the now-unused `ShareResponse` type import (`lib/api.ts:36`) + its definition in `lib/types.ts` if nothing else references it (grep first).
- [ ] **Step 3: Gate.** `cd frontend && npm run typecheck && npm run build` → passes (no dangling imports).
- [ ] **Step 4: Manual smoke.** KB detail renders without a share button; no console/type errors.
- [ ] **Step 5: Commit.**

```bash
git add -A frontend
git commit -m "chore(fe): retire ShareModal (user-to-user sharing superseded by groups)"
```

---

## Self-Review

- **Spec coverage:** developer-only gate → A2–A5 (`get_current_developer`) + B5. GET /users (picker) → A2/B3. members list → A3/B3. group kbs list → A3/B4. all-KBs picker → A4/B4. assign/move/unassign (1:1) → A5/B4. group CRUD UI → B2. member add/remove reuse existing endpoints → B3. ShareModal retire → B6. All spec sections covered.
- **No new frontend test framework** (constraint honored): gates are typecheck+build+manual smoke.
- **Open confirmations for the implementer (verify against real code before finalizing):** `SqlKbRepo` real class name + getter name + whether `select`/`KnowledgeBase`/`GroupMember`/`User` are imported in `repositories.py`; `POST /groups` + `POST /groups/{id}/members` accept JSON bodies (confirm current handler signatures in `groups.py` — they use pydantic request models, so JSON is correct); `request<T>` options shape + whether it auto-sets `Content-Type` (`lib/api.ts:126`); the nav array structure + `IcoDb`/icon usage in `AuthGate.tsx`; `app/kb/[kbId]/page.tsx` exact ShareModal trigger to remove; `User.display_name` nullability.
- **Type consistency:** `AdminKbItem`/`GroupKbItem`/`GroupMemberListItem`/`UserListItem` fields identical across backend schemas (Task A2–A4) and frontend types (B1). `group_id`/`group_code` nullable in both.
- **Response-shape convention (verified, do NOT drift):** KB responses key on **`kb_id`** (`AdminKbItem`, `GroupKbItem`, and the existing `KbCreateResponse`/`KbSummary` at `backend/app/schemas/jobs.py` — there is NO `id` on KB responses). Group responses key on **`id`** (`GroupResponse`). Group members on `user_id`/`email`. Every test assertion and frontend mapper follows this: KB → `kb_id` (list links use `kb.kb_id`), group → `id` (list links use `group.id`), member → `user_id`. The assign endpoint returns `{kb_id, group_id}`.
- **v2→v3 fixes (competitive validation round 2, 3 must-fix resolved — all AuthGate/nav):** (a) AuthGate is a NAMED export → B2/B3 use `import { AuthGate }` (no default import); (b) B5 redirect guard evaluates the freshly-read `getStoredUser()` synchronously inside the mount effect BEFORE `setUser` (not the null-on-first-paint `user` state) and adds `pathname` to the dep array so it re-runs on navigation; (c) B5 nav entry is computed in the component body (`const nav = role==='developer' ? [...PRIMARY_NAV, item] : PRIMARY_NAV`) and the render maps over `nav` — the module-level `PRIMARY_NAV` const is never mutated.
- **v1→v2 fixes (competitive validation, 9 must-fix resolved):** (1) `seed_user`/`auth_header_for`/`make_group`/`make_kb` are module FUNCTIONS imported from conftest, not fixtures — all test defs take only `(db_session, app_client)`; (2) shares fn is `revokeShare`, not `deleteShare`; (3) B1 no longer removes shares fns (moved entirely to B6) so each gate stays green; (4) AuthGate is per-page — new `/admin` pages wrap themselves in `<AuthGate>`; (5) `roleLabel` gets a concrete `developer` branch; (6) `/admin/kbs` uses `k.owner.email`/`k.group.code` relationships, not the reflection trap; (7) `set_group` + unassign guard NULL `group_id` before any `uuid.UUID(...)`/`_to_uuid` (no `uuid.UUID('None')` crash); (8) `SqlKbRepo.set_group` is spelled out (A1) and the assign route validates the group via `_group_or_404`; (9) `kb_id` vs `id` convention pinned above.
