"""W3 Community-report pipeline (GraphRAG-style) over the edgequake graph.

Single-DB principle: reads the Apache AGE graph (``eq_eq_default_graph``) and
writes ``public.community_reports`` in the SAME edgequake Postgres.

Pipeline:
  1. ``fetch_graph``            — load Node/EDGE properties for one workspace.
  2. ``build_communities``      — networkx + Louvain partition.
  3. ``generate_report``        — render a community as Entities/Relationships
                                  CSV blocks, run the ported GraphRAG community
                                  report prompt through an injectable ``llm``,
                                  parse the JSON.
  4. ``store_reports``          — CREATE TABLE IF NOT EXISTS + upsert.
  5. ``build_workspace_communities`` — orchestrate 1-4.
  6. ``global_query``           — map-reduce QA over the stored reports.

GraphRAG prompt is PORTED (Korean-localized output, ``/no_think`` prefix) from
``graphrag_root/prompts/community_report_graph.txt``. Output schema:
``{title, summary, rating, rating_explanation, findings:[{summary, explanation}]}``.

The ``llm`` argument everywhere is an injectable callable
``(system_prompt: str, user_prompt: str) -> str`` so tests can mock it with no
network. ``make_openrouter_llm`` builds the real qwen-via-OpenRouter callable.
"""

from __future__ import annotations

import json
import os
import re
from typing import Callable, Iterable

WORKSPACE_ID = "00000000-0000-0000-0000-000000000003"
DEFAULT_DSN = "host=localhost port=5432 user=edgequake password=edgequake_secret dbname=edgequake"
DEFAULT_MODEL = "qwen/qwen3.5-122b-a10b"
DEFAULT_ENV_PATH = (
    "/Users/xxx/workspace/99.projects/rag-edgequake-benchmark/docker/.env"
)
MAX_REPORT_LENGTH = 2000

#: An LLM callable: (system_prompt, user_prompt) -> completion text.
LLM = Callable[[str, str], str]

# --------------------------------------------------------------------------- #
# Ported GraphRAG community-report prompt (community_report_graph.txt).
# Korean-localized output values, English JSON keys + [Data: ...] citations.
# {input_text} and {max_report_length} are .format() placeholders; the literal
# JSON braces in the schema/example are escaped as {{ }} so .format leaves them.
# --------------------------------------------------------------------------- #
COMMUNITY_REPORT_PROMPT = r"""/no_think

**언어 규칙: 모든 출력 텍스트 값(title, summary, findings 의 summary 와 explanation)은 반드시 한국어로 작성하라. 단, JSON 키 이름과 전체 구조, 그리고 [Data: Entities (...); Relationships (...)] 같은 인용 표기 형식은 영어 그대로 유지하라.**

You are an AI assistant that helps a human analyst to perform general information discovery. Information discovery is the process of identifying and assessing relevant information associated with certain entities (e.g., organizations and individuals) within a network.

# Goal
Write a comprehensive report of a community, given a list of entities that belong to the community as well as their relationships and optional associated claims. The report will be used to inform decision-makers about information associated with the community and their potential impact. The content of this report includes an overview of the community's key entities, their legal compliance, technical capabilities, reputation, and noteworthy claims.

# Report Structure

The report should include the following sections:

- TITLE: community's name that represents its key entities - title should be short but specific. When possible, include representative named entities in the title.
- SUMMARY: An executive summary of the community's overall structure, how its entities are related to each other, and significant information associated with its entities.
- IMPACT SEVERITY RATING: a float score between 0-10 that represents the severity of IMPACT posed by entities within the community.  IMPACT is the scored importance of a community.
- RATING EXPLANATION: Give a single sentence explanation of the IMPACT severity rating.
- DETAILED FINDINGS: A list of 5-10 key insights about the community. Each insight should have a short summary followed by multiple paragraphs of explanatory text grounded according to the grounding rules below. Be comprehensive.

Return output as a well-formed JSON-formatted string with the following format:
    {{
        "title": <report_title>,
        "summary": <executive_summary>,
        "rating": <impact_severity_rating>,
        "rating_explanation": <rating_explanation>,
        "findings": [
            {{
                "summary":<insight_1_summary>,
                "explanation": <insight_1_explanation>
            }},
            {{
                "summary":<insight_2_summary>,
                "explanation": <insight_2_explanation>
            }}
        ]
    }}

# Grounding Rules

Points supported by data should list their data references as follows:

"This is an example sentence supported by multiple data references [Data: <dataset name> (record ids); <dataset name> (record ids)]."

Do not list more than 5 record ids in a single reference. Instead, list the top 5 most relevant record ids and add "+more" to indicate that there are more.

For example:
"Person X is the owner of Company Y and subject to many allegations of wrongdoing [Data: Reports (1), Entities (5, 7); Relationships (23); Claims (7, 2, 34, 64, 46, +more)]."

where 1, 5, 7, 23, 2, 34, 46, and 64 represent the id (not the index) of the relevant data record.

Do not include information where the supporting evidence for it is not provided.

Limit the total report length to {max_report_length} words.

# Example Input
-----------
Text:

Entities

id,entity,description
5,VERDANT OASIS PLAZA,Verdant Oasis Plaza is the location of the Unity March
6,HARMONY ASSEMBLY,Harmony Assembly is an organization that is holding a march at Verdant Oasis Plaza

Relationships

id,source,target,description
37,VERDANT OASIS PLAZA,UNITY MARCH,Verdant Oasis Plaza is the location of the Unity March
38,VERDANT OASIS PLAZA,HARMONY ASSEMBLY,Harmony Assembly is holding a march at Verdant Oasis Plaza
43,HARMONY ASSEMBLY,UNITY MARCH,Harmony Assembly is organizing the Unity March

Output:
{{
    "title": "Verdant Oasis Plaza and Unity March",
    "summary": "The community revolves around the Verdant Oasis Plaza, which is the location of the Unity March.",
    "rating": 5.0,
    "rating_explanation": "The impact severity rating is moderate due to the potential for unrest or conflict during the Unity March.",
    "findings": [
        {{
            "summary": "Verdant Oasis Plaza as the central location",
            "explanation": "Verdant Oasis Plaza is the central entity in this community, serving as the location for the Unity March. [Data: Entities (5), Relationships (37, 38, +more)]"
        }}
    ]
}}


# Real Data

Use the following text for your answer. Do not make anything up in your answer.

Text:
{input_text}

The report should include the following sections following the same structure
(TITLE, SUMMARY, IMPACT SEVERITY RATING, RATING EXPLANATION, DETAILED FINDINGS)
and the same well-formed JSON format described above, with 5-10 findings.

Limit the total report length to {max_report_length} words.
Output:"""


# --------------------------------------------------------------------------- #
# 1. fetch_graph
# --------------------------------------------------------------------------- #
_NODE_SQL = """
SELECT
    properties::text::jsonb ->> 'node_id'      AS node_id,
    properties::text::jsonb ->> 'description'   AS description,
    properties::text::jsonb ->> 'entity_type'  AS entity_type,
    (properties::text::jsonb ->> 'importance')::float AS importance
FROM eq_eq_default_graph."Node"
WHERE properties::text::jsonb ->> 'workspace_id' = %s
  AND properties::text::jsonb ->> 'node_id' IS NOT NULL
"""

_EDGE_SQL = """
SELECT
    properties::text::jsonb ->> 'source_id'     AS source_id,
    properties::text::jsonb ->> 'target_id'     AS target_id,
    properties::text::jsonb ->> 'description'   AS description,
    properties::text::jsonb ->> 'relation_type' AS relation_type,
    (properties::text::jsonb ->> 'weight')::float AS weight
FROM eq_eq_default_graph."EDGE"
WHERE properties::text::jsonb ->> 'workspace_id' = %s
"""


def fetch_graph(workspace_id: str, dsn: str = DEFAULT_DSN):
    """Read entities + relations for one workspace from the AGE graph.

    AGE stores ``properties`` as ``ag_catalog.agtype`` (NOT jsonb), so we cast
    ``properties::text::jsonb`` before extracting. The single physical graph is
    shared across tenants; we ALWAYS scope by ``workspace_id``.

    :returns: ``(entities, relations)`` where ``entities`` is a list of
        ``{node_id, description, entity_type, importance}`` and ``relations`` is
        a list of ``{source_id, target_id, description, relation_type, weight}``.
    """
    import psycopg  # local import so unit tests need no DB driver

    entities: list[dict] = []
    relations: list[dict] = []
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(_NODE_SQL, (workspace_id,))
        for node_id, description, entity_type, importance in cur.fetchall():
            entities.append(
                {
                    "node_id": node_id,
                    "description": description or "",
                    "entity_type": entity_type or "",
                    "importance": importance if importance is not None else 0.0,
                }
            )
        cur.execute(_EDGE_SQL, (workspace_id,))
        for source_id, target_id, description, relation_type, weight in cur.fetchall():
            relations.append(
                {
                    "source_id": source_id,
                    "target_id": target_id,
                    "description": description or "",
                    "relation_type": relation_type or "",
                    "weight": weight if weight is not None else 0.0,
                }
            )
    return entities, relations


# --------------------------------------------------------------------------- #
# 2. build_communities
# --------------------------------------------------------------------------- #
def build_communities(
    entities: list[dict],
    relations: list[dict],
    resolution: float = 1.0,
) -> list[set[str]]:
    """Build a networkx graph and run Louvain community detection.

    Nodes are entity ``node_id`` strings; edges join by the ``source_id`` /
    ``target_id`` strings (== ``node_id``). Edge ``weight`` is used (uniform 0.5
    in the test doc, so partitioning is driven by topology). Isolated entities
    each become their own singleton community.

    :returns: a list of communities, each a ``set`` of entity ``node_id``.
    """
    import networkx as nx

    g = nx.Graph()
    valid = {e["node_id"] for e in entities if e.get("node_id")}
    g.add_nodes_from(valid)

    for r in relations:
        s, t = r.get("source_id"), r.get("target_id")
        if not s or not t:
            continue
        # endpoints must be real entities of this workspace
        if s not in valid or t not in valid:
            continue
        w = r.get("weight") or 1.0
        if g.has_edge(s, t):
            g[s][t]["weight"] += w
        else:
            g.add_edge(s, t, weight=w)

    if g.number_of_nodes() == 0:
        return []

    # python-louvain (community.best_partition) — the standard Louvain primitive.
    import community as community_louvain

    partition = community_louvain.best_partition(
        g, weight="weight", resolution=resolution, random_state=42
    )

    buckets: dict[int, set[str]] = {}
    for node, comm_id in partition.items():
        buckets.setdefault(comm_id, set()).add(node)

    # deterministic ordering: largest community first, then by min member name
    communities = sorted(
        buckets.values(), key=lambda s: (-len(s), min(s))
    )
    return communities


# --------------------------------------------------------------------------- #
# 3. generate_report
# --------------------------------------------------------------------------- #
def _csv_escape(value: str) -> str:
    """Minimal CSV field escaping (quote if it contains , " or newline)."""
    value = (value or "").replace("\n", " ").strip()
    if any(c in value for c in (",", '"')):
        return '"' + value.replace('"', '""') + '"'
    return value


def render_community_text(
    community: set[str],
    entities: list[dict],
    relations: list[dict],
) -> tuple[str, dict, dict]:
    """Render a community as Entities + Relationships CSV blocks.

    Per-community INTEGER ids (1..N) are assigned so the model's
    ``[Data: Entities (n)]`` citations resolve to a record, not a node_id string.

    :returns: ``(input_text, entity_id_map, member_entities)`` where
        ``entity_id_map`` maps ``node_id -> integer id`` and ``member_entities``
        maps ``node_id -> entity dict``.
    """
    by_id = {e["node_id"]: e for e in entities if e.get("node_id")}
    members = [n for n in sorted(community) if n in by_id]

    entity_id_map: dict[str, int] = {n: i + 1 for i, n in enumerate(members)}
    member_entities = {n: by_id[n] for n in members}

    ent_lines = ["id,entity,description"]
    for n in members:
        e = by_id[n]
        ent_lines.append(
            f"{entity_id_map[n]},{_csv_escape(n)},{_csv_escape(e.get('description', ''))}"
        )

    rel_lines = ["id,source,target,description"]
    rid = 0
    member_set = set(members)
    for r in relations:
        s, t = r.get("source_id"), r.get("target_id")
        if s in member_set and t in member_set:
            rid += 1
            rel_lines.append(
                f"{rid},{_csv_escape(s)},{_csv_escape(t)},{_csv_escape(r.get('description', ''))}"
            )

    input_text = (
        "Entities\n\n"
        + "\n".join(ent_lines)
        + "\n\nRelationships\n\n"
        + "\n".join(rel_lines)
    )
    return input_text, entity_id_map, member_entities


def _extract_json(text: str) -> dict:
    """Parse a JSON object from an LLM completion, tolerating fences/preamble.

    Handles ``/no_think`` echoes, ```json fences, and leading/trailing prose by
    grabbing the outermost balanced ``{...}``.
    """
    text = text.strip()
    # strip code fences
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # fall back to outermost balanced object
    start = text.find("{")
    if start == -1:
        raise ValueError(f"no JSON object found in LLM output: {text[:200]!r}")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError(f"unbalanced JSON in LLM output: {text[:200]!r}")


def generate_report(
    community: set[str],
    entities: list[dict],
    relations: list[dict],
    *,
    llm: LLM,
    max_report_length: int = MAX_REPORT_LENGTH,
) -> dict:
    """Generate one community report dict via the ported GraphRAG prompt.

    :param llm: injectable ``(system_prompt, user_prompt) -> str``.
    :returns: ``{title, summary, findings:[{summary,explanation}], rank,
        entity_ids}``. ``rank`` comes from GraphRAG ``rating``;
        ``rating_explanation`` is folded into the summary (no column for it).
    """
    input_text, _entity_id_map, member_entities = render_community_text(
        community, entities, relations
    )
    prompt = COMMUNITY_REPORT_PROMPT.format(
        input_text=input_text, max_report_length=max_report_length
    )
    # System message carries the role; user message carries the rendered prompt.
    raw = llm("You are a careful GraphRAG community-report writer.", prompt)
    parsed = _extract_json(raw)

    findings = parsed.get("findings") or []
    # normalize findings to [{summary, explanation}]
    norm_findings = [
        {
            "summary": f.get("summary", "") if isinstance(f, dict) else str(f),
            "explanation": f.get("explanation", "") if isinstance(f, dict) else "",
        }
        for f in findings
    ]

    rating = parsed.get("rating")
    try:
        rank = float(rating) if rating is not None else None
    except (TypeError, ValueError):
        rank = None

    summary = parsed.get("summary", "") or ""
    rating_explanation = parsed.get("rating_explanation")
    if rating_explanation:
        # fold the unmapped rating_explanation into the summary
        summary = f"{summary}\n\n[중요도 근거] {rating_explanation}".strip()

    return {
        "title": parsed.get("title", "") or "",
        "summary": summary,
        "findings": norm_findings,
        "rank": rank,
        "entity_ids": sorted(member_entities.keys()),
    }


# --------------------------------------------------------------------------- #
# 4. store_reports
# --------------------------------------------------------------------------- #
_DDL = """
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
CREATE INDEX IF NOT EXISTS idx_community_reports_ws
    ON public.community_reports(workspace_id);
CREATE INDEX IF NOT EXISTS idx_community_reports_ws_level
    ON public.community_reports(workspace_id, level);
"""

_UPSERT_SQL = """
INSERT INTO public.community_reports
    (workspace_id, level, community_id, title, summary, findings, rank, entity_ids)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (workspace_id, level, community_id) DO UPDATE SET
    title = EXCLUDED.title,
    summary = EXCLUDED.summary,
    findings = EXCLUDED.findings,
    rank = EXCLUDED.rank,
    entity_ids = EXCLUDED.entity_ids,
    created_at = now()
"""


def store_reports(
    reports: list[dict],
    workspace_id: str,
    dsn: str = DEFAULT_DSN,
    *,
    level: int = 0,
) -> int:
    """Create the table if needed and upsert reports. Returns rows written.

    Each report dict needs: ``community_id, title, summary, findings, rank,
    entity_ids``. ``findings`` is serialized to jsonb.
    """
    import psycopg

    written = 0
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(_DDL)
        for rep in reports:
            cur.execute(
                _UPSERT_SQL,
                (
                    workspace_id,
                    level,
                    rep["community_id"],
                    rep.get("title", "") or "",
                    rep.get("summary", ""),
                    json.dumps(rep.get("findings", []), ensure_ascii=False),
                    rep.get("rank"),
                    rep.get("entity_ids", []),
                ),
            )
            written += 1
        conn.commit()
    return written


# --------------------------------------------------------------------------- #
# 5. build_workspace_communities (orchestration)
# --------------------------------------------------------------------------- #
def build_workspace_communities(
    workspace_id: str = WORKSPACE_ID,
    *,
    llm: LLM,
    dsn: str = DEFAULT_DSN,
    resolution: float = 1.0,
    level: int = 0,
    min_community_size: int = 1,
) -> dict:
    """Orchestrate fetch -> partition -> report -> store for a workspace.

    :returns: counts ``{entities, relations, communities, reports_written}``.
    """
    entities, relations = fetch_graph(workspace_id, dsn)
    communities = build_communities(entities, relations, resolution=resolution)

    reports: list[dict] = []
    for community_id, community in enumerate(communities):
        if len(community) < min_community_size:
            continue
        rep = generate_report(community, entities, relations, llm=llm)
        rep["community_id"] = community_id
        reports.append(rep)

    written = store_reports(reports, workspace_id, dsn, level=level)
    return {
        "entities": len(entities),
        "relations": len(relations),
        "communities": len(communities),
        "reports_written": written,
    }


# --------------------------------------------------------------------------- #
# 6. global_query (map-reduce QA over reports)
# --------------------------------------------------------------------------- #
_SELECT_REPORTS_SQL = """
SELECT community_id, title, summary, findings, rank, entity_ids
FROM public.community_reports
WHERE workspace_id = %s AND level = %s
ORDER BY rank DESC NULLS LAST, community_id ASC
"""

_MAP_SYSTEM = (
    "You extract, from a single community report, the points relevant to the "
    "user's question. Answer in Korean. If the report is irrelevant, reply with "
    "exactly: NONE."
)
_MAP_PROMPT = """질문: {question}

아래는 하나의 커뮤니티 리포트입니다. 이 리포트에서 질문과 관련된 핵심 정보만 간결히 추출하세요.
관련 내용이 없으면 정확히 NONE 이라고만 답하세요.

리포트 제목: {title}
요약: {summary}
주요 발견:
{findings}
"""

_REDUCE_SYSTEM = (
    "You synthesize multiple partial answers (each from one community report) "
    "into a single coherent answer. Answer in Korean. Cite community ids like "
    "[Community: 0, 3]."
)
_REDUCE_PROMPT = """질문: {question}

아래는 여러 커뮤니티 리포트에서 각각 추출한 부분 답변입니다. 이들을 종합하여
질문에 대한 하나의 일관된 최종 답변을 작성하세요.

{partials}
"""


def _select_top_reports(
    cur, workspace_id: str, level: int, question: str, top_k: int
) -> list[dict]:
    """Select candidate reports by rank, then re-rank by keyword overlap."""
    cur.execute(_SELECT_REPORTS_SQL, (workspace_id, level))
    rows = cur.fetchall()
    reports = []
    for community_id, title, summary, findings, rank, entity_ids in rows:
        reports.append(
            {
                "community_id": community_id,
                "title": title,
                "summary": summary or "",
                "findings": findings or [],
                "rank": rank if rank is not None else 0.0,
                "entity_ids": entity_ids or [],
            }
        )
    return _rank_reports(reports, question, top_k)


def _rank_reports(reports: list[dict], question: str, top_k: int) -> list[dict]:
    """Rank reports by keyword overlap with the question, tie-broken by rank."""
    q_terms = {t for t in re.split(r"\W+", question.lower()) if len(t) > 1}

    def score(rep: dict) -> tuple[float, float]:
        hay = (rep.get("title", "") + " " + rep.get("summary", "")).lower()
        for f in rep.get("findings", []):
            if isinstance(f, dict):
                hay += " " + f.get("summary", "") + " " + f.get("explanation", "")
        overlap = sum(1 for term in q_terms if term in hay)
        return (float(overlap), float(rep.get("rank") or 0.0))

    return sorted(reports, key=score, reverse=True)[:top_k]


def _format_findings(findings) -> str:
    lines = []
    for f in findings or []:
        if isinstance(f, dict):
            lines.append(f"- {f.get('summary', '')}: {f.get('explanation', '')}")
        else:
            lines.append(f"- {f}")
    return "\n".join(lines)


def global_query(
    question: str,
    workspace_id: str = WORKSPACE_ID,
    *,
    llm: LLM,
    dsn: str = DEFAULT_DSN,
    top_k: int = 5,
    level: int = 0,
    reports: list[dict] | None = None,
) -> dict:
    """Map-reduce global QA over stored community reports.

    Map: each selected report -> a partial answer (or NONE).
    Reduce: combine the non-NONE partials -> final answer.

    :param reports: optional pre-fetched reports (used by tests to skip the DB).
    :returns: ``{answer, communities_used}``.
    """
    if reports is None:
        import psycopg

        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            selected = _select_top_reports(cur, workspace_id, level, question, top_k)
    else:
        selected = _rank_reports(reports, question, top_k)

    partials: list[tuple[int, str]] = []
    for rep in selected:
        mapped = llm(
            _MAP_SYSTEM,
            _MAP_PROMPT.format(
                question=question,
                title=rep.get("title", ""),
                summary=rep.get("summary", ""),
                findings=_format_findings(rep.get("findings")),
            ),
        ).strip()
        if mapped and mapped.upper() != "NONE":
            partials.append((rep["community_id"], mapped))

    communities_used = [cid for cid, _ in partials]

    if not partials:
        return {
            "answer": "관련 커뮤니티 리포트에서 답을 찾지 못했습니다.",
            "communities_used": [],
        }

    partials_text = "\n\n".join(
        f"[Community {cid}]\n{text}" for cid, text in partials
    )
    answer = llm(
        _REDUCE_SYSTEM,
        _REDUCE_PROMPT.format(question=question, partials=partials_text),
    ).strip()

    return {"answer": answer, "communities_used": communities_used}


# --------------------------------------------------------------------------- #
# OpenRouter (qwen) LLM factory — the real callable.
# --------------------------------------------------------------------------- #
def _load_env_value(key: str, env_path: str) -> str | None:
    """Read KEY=VALUE from a .env file without importing it / printing it."""
    if not os.path.exists(env_path):
        return None
    with open(env_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    return None


def make_openrouter_llm(
    *,
    model: str = DEFAULT_MODEL,
    env_path: str = DEFAULT_ENV_PATH,
    temperature: float = 0.2,
    timeout: float = 120.0,
) -> LLM:
    """Build a real ``(system, user) -> str`` callable hitting OpenRouter.

    The API key (sk-or-...) and base URL are read from the docker ``.env`` and
    are NEVER printed/logged. Calls OpenRouter's OpenAI-compatible chat
    completions endpoint directly with ``model=qwen/qwen3.5-122b-a10b`` (a
    standalone job is unaffected by edgequake's internal model-name guard).
    """
    api_key = os.environ.get("OPENAI_API_KEY") or _load_env_value(
        "OPENAI_API_KEY", env_path
    )
    base_url = (
        os.environ.get("OPENAI_BASE_URL")
        or _load_env_value("OPENAI_BASE_URL", env_path)
        or "https://openrouter.ai/api/v1"
    )
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY not found in environment or .env "
            f"({env_path}); cannot reach OpenRouter."
        )

    import httpx

    url = base_url.rstrip("/") + "/chat/completions"

    def _llm(system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return data["choices"][0]["message"]["content"]

    return _llm


# --------------------------------------------------------------------------- #
# 7. CLI
# --------------------------------------------------------------------------- #
def main(argv: Iterable[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="W3 community-report pipeline (build + global query)."
    )
    parser.add_argument("--workspace-id", default=WORKSPACE_ID)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--resolution", type=float, default=1.0)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--env-path", default=DEFAULT_ENV_PATH)
    parser.add_argument(
        "--question",
        default="이 워크스페이스의 휴가 규정에서 가장 중요한 내용은 무엇인가요?",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="skip community build; only run the global query.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    llm = make_openrouter_llm(model=args.model, env_path=args.env_path)

    if not args.skip_build:
        counts = build_workspace_communities(
            args.workspace_id, llm=llm, dsn=args.dsn, resolution=args.resolution
        )
        print(f"[build] {counts}")

    result = global_query(
        args.question,
        args.workspace_id,
        llm=llm,
        dsn=args.dsn,
        top_k=args.top_k,
    )
    print(f"[query] communities_used={result['communities_used']}")
    print(f"[answer]\n{result['answer']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
