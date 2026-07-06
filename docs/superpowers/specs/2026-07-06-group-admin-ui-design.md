# 그룹 관리 admin UI — 설계 (2026-07-06)

> 선행: 그룹 기반 KB 접근제어(`2026-07-04-group-based-kb-access-control`, PR #1 머지·dev 라이브). 이 문서는 그 위에 **developer 전용 그룹 관리 화면**을 얹는다. 레포: kb-backend(`/Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base`).

## 목적

developer 역할 사용자가 (1) 그룹 생성/삭제, (2) 그룹에 **사용자 할당/해제**, (3) 그룹에 **KB 지정/해제**를 할 수 있는 관리 UI. KB↔group 은 1:1(KB 하나=그룹 하나), user↔group 은 M:N.

## 결정 (브레인스토밍 확정)

- **접근권한**: `developer` 역할만(백엔드 `get_current_developer` 게이트와 일치). 프론트의 기존 `role=='admin'` 참조는 `developer` 로 정리. 추가 롤 신설 없음.
- **KB 지정 방향**: **그룹 상세 중심** — 그룹 상세 화면에서 전체 KB 중 골라 이 그룹에 지정(1:1이라 이미 다른 그룹이면 이동). KB-중심(생성/상세 셀렉터)은 비범위.
- **사용자 추가**: **사용자 피커**(목록에서 선택) — `GET /users` 신설 + 드롭다운. 이메일 자유입력 아님.
- **ShareModal(user↔user 공유)**: 백엔드 이미 410 은퇴 → 프론트에서 제거.

## 백엔드 (FastAPI, 전부 `get_current_developer` 게이트, 2.0 select 스타일)

신설 엔드포인트:

| 메서드·경로 | 응답 | 비고 |
|---|---|---|
| `GET /users` | `[{id, email, display_name, role}]` | 멤버 피커용. 전체 목록(현 사용자 2명, 서버검색 미도입=YAGNI) |
| `GET /groups/{group_id}/members` | `[{user_id, email, display_name}]` | 멤버 목록(users JOIN) |
| `GET /groups/{group_id}/kbs` | `[{kb_id, name, branch_code, status}]` | 그 그룹 소속 KB |
| `GET /admin/kbs` | `[{kb_id, name, group_id, group_code, owner_email}]` | **전체** KB(+현재 그룹). 기존 `GET /kb`(멤버십 스코프)와 별개 |
| `PUT /groups/{group_id}/kbs/{kb_id}` | `{kb_id, group_id}` | KB 를 이 그룹으로 지정/이동(`knowledge_bases.group_id=group_id`). group·kb 존재검증(404), 멱등 |
| `DELETE /groups/{group_id}/kbs/{kb_id}` | 204 | 해제 → `group_id=NULL`(그 KB 가 현재 이 그룹일 때만; 아니면 404). KB 검색불가 상태가 됨 |

- 멤버 추가/제거는 **기존** `POST /groups/{id}/members`(email) · `DELETE /groups/{id}/members/{user_id}` 재사용. 피커가 선택한 사용자의 email 을 POST 에 전달.
- 라우터 배치: `GET /users` 는 신규 `app/routers/users.py`(developer). group KB/members 조회·지정은 `app/routers/groups.py` 확장. `GET /admin/kbs` 는 `groups.py` 또는 신규 admin 라우터(구현 plan 에서 확정).
- 리포지토리: `SqlGroupMembersRepo` 에 `list_members(group_id)`(users JOIN) 추가, `SqlKbRepo` 에 `list_all()`·`set_group(kb_id, group_id|None)` 추가, `SqlUserRepo`(신규 or 기존 `_resolve_user_by_email` 승격) 에 `list()`.
- 스키마: `app/schemas/groups.py` 에 `GroupMemberListItem`, `GroupKbItem`; `app/schemas/users.py`(신규) `UserListItem`; `app/schemas/admin.py` or groups 에 `AdminKbItem`.

## 프론트 (Next.js App Router, 순수 CSS, `lib/api.ts` 패턴)

라우트:
- `/admin/groups` — 그룹 목록 + 생성 폼(`code`,`name`) + 삭제(소속 KB 검색불가 경고 confirm). *(app/kb/page.tsx 리스트+CreateKbForm 패턴 미러)*
- `/admin/groups/[groupId]` — 그룹 상세:
  - **멤버 패널**: `GET members` 리스트 + 사용자 피커(`GET /users` 드롭다운, 이미 멤버는 제외/비활성) → `POST members`(email) 추가, `DELETE members/{user_id}` 제거. *(ShareModal 리스트/폼 패턴 미러)*
  - **KB 패널**: `GET group kbs` 리스트 + 전체 KB 피커(`GET /admin/kbs`, 현재 그룹 표시) → `PUT .../kbs/{kb_id}` 지정(타 그룹이면 "이동" 경고 confirm), `DELETE .../kbs/{kb_id}` 해제.
- 네비: 헤더/AuthGate 에서 `user.role==='developer'` 일 때만 `/admin/groups` 링크 노출. 비-developer 의 `/admin/*` 직접 접근 → `/kb` 로 redirect(프론트 UX). **최종 강제는 백엔드 게이트**(직접 API 호출 시 403).
- `lib/api.ts`: `listGroups, createGroup, deleteGroup, getGroupMembers, addGroupMember, removeGroupMember, getGroupKbs, assignKbToGroup, unassignKbFromGroup, listAllKbsAdmin, listUsers` 추가(기존 `request<T>()` + `ApiError` 패턴). `lib/types.ts` 대응 타입.
- **ShareModal 제거**: `components/ShareModal.tsx` 및 KB 상세의 공유 버튼/호출 제거. api.ts 의 shares 함수 제거 또는 deprecate.

## 에러/엣지

- 그룹 삭제 → 소속 KB `group_id` SET NULL(검색불가) → UI confirm 경고.
- KB 재지정(1:1 이동) → 이전 그룹서 빠짐 → UI confirm 경고("이 KB 는 현재 그룹 X 에 속함. 이동?").
- 마지막 멤버 제거 허용(그룹 유지). group_id NULL 인 KB 는 소유자도 검색불가(관리자만 재지정 가능).
- 비-developer: 프론트 redirect + 백엔드 403. 토큰 만료 → 기존 401 핸들러(로그인 이동).

## 테스트

- **백엔드 pytest**(sqlite 인메모리, `db_session`/`seed_user(role='developer')`/`app_client` 패턴):
  - `GET /users` developer 게이트(비-dev 403) + 목록.
  - `GET /groups/{id}/members`·`/kbs` 목록.
  - `GET /admin/kbs` 전체+현재그룹.
  - `PUT/DELETE .../kbs/{kb_id}` 1:1 지정·이동·해제(이동 시 이전 그룹서 빠짐; 해제 시 group_id NULL).
- **프론트**: 기존 테스트 관행 확인 후 최소 — api 함수 단위 + `/admin/groups` 핵심 컴포넌트 렌더/상호작용(있으면). 없으면 수동 스모크 절차 문서화.

## 비범위

- KB-중심 그룹 셀렉터(생성/상세), 그룹 rename, 서버측 사용자 검색/페이지네이션, 대량 일괄지정, W4 RLS 하드닝, facade 시크릿게이트 활성화.
