"""Unit tests for kb_pipeline.community (W3 community-report pipeline).

All LLM calls are MOCKED — no network, no DB. Covers:
  * build_communities on a synthetic two-cluster graph,
  * report rendering + JSON parse (incl. fenced / prose-wrapped output),
  * global_query map-reduce.
"""

import json

import pytest

from kb_pipeline.community import (
    build_communities,
    generate_report,
    render_community_text,
    global_query,
    _extract_json,
    _rank_reports,
)


# --------------------------------------------------------------------------- #
# build_communities
# --------------------------------------------------------------------------- #
def _two_cluster_graph():
    """Two dense triangles (A,B,C) and (X,Y,Z) joined by a single thin bridge."""
    entities = [
        {"node_id": n, "description": f"desc {n}", "entity_type": "CONCEPT", "importance": 0.5}
        for n in ["A", "B", "C", "X", "Y", "Z"]
    ]
    rel = lambda s, t, w=0.5: {
        "source_id": s, "target_id": t, "description": f"{s}->{t}",
        "relation_type": "REL", "weight": w,
    }
    relations = [
        rel("A", "B"), rel("B", "C"), rel("A", "C"),       # cluster 1
        rel("X", "Y"), rel("Y", "Z"), rel("X", "Z"),       # cluster 2
        rel("C", "X", 0.1),                                 # weak bridge
    ]
    return entities, relations


def test_build_communities_finds_two_clusters():
    entities, relations = _two_cluster_graph()
    comms = build_communities(entities, relations, resolution=1.0)

    assert len(comms) == 2
    # every node assigned exactly once
    all_nodes = set().union(*comms)
    assert all_nodes == {"A", "B", "C", "X", "Y", "Z"}
    assert sum(len(c) for c in comms) == 6

    # the two triangles stay together
    by_node = {n: i for i, c in enumerate(comms) for n in c}
    assert by_node["A"] == by_node["B"] == by_node["C"]
    assert by_node["X"] == by_node["Y"] == by_node["Z"]
    assert by_node["A"] != by_node["X"]


def test_build_communities_isolated_nodes_are_singletons():
    entities = [
        {"node_id": "lonely", "description": "d", "entity_type": "CONCEPT", "importance": 0.5}
    ]
    comms = build_communities(entities, [])
    assert comms == [{"lonely"}]


def test_build_communities_ignores_dangling_edges():
    entities = [
        {"node_id": "A", "description": "d", "entity_type": "X", "importance": 0.5},
        {"node_id": "B", "description": "d", "entity_type": "X", "importance": 0.5},
    ]
    relations = [
        {"source_id": "A", "target_id": "GHOST", "description": "", "relation_type": "", "weight": 0.5},
        {"source_id": "A", "target_id": "B", "description": "", "relation_type": "", "weight": 0.5},
    ]
    comms = build_communities(entities, relations)
    assert set().union(*comms) == {"A", "B"}


def test_build_communities_empty():
    assert build_communities([], []) == []


# --------------------------------------------------------------------------- #
# render_community_text / generate_report (parse)
# --------------------------------------------------------------------------- #
def test_render_uses_integer_ids_per_community():
    entities = [
        {"node_id": "VERDANT", "description": "a plaza", "entity_type": "LOCATION", "importance": 0.5},
        {"node_id": "HARMONY", "description": "an org", "entity_type": "ORGANIZATION", "importance": 0.5},
    ]
    relations = [
        {"source_id": "VERDANT", "target_id": "HARMONY", "description": "holds march",
         "relation_type": "REL", "weight": 0.5},
    ]
    text, id_map, members = render_community_text({"VERDANT", "HARMONY"}, entities, relations)

    assert "Entities\n\nid,entity,description" in text
    assert "Relationships\n\nid,source,target,description" in text
    # integer ids 1..N (not node_id strings) for citation resolution
    assert set(id_map.values()) == {1, 2}
    assert "1,HARMONY,an org" in text  # sorted: HARMONY before VERDANT
    # relationship row keeps original source->target direction
    assert "1,VERDANT,HARMONY,holds march" in text
    assert set(members.keys()) == {"VERDANT", "HARMONY"}


def test_render_csv_escapes_commas():
    entities = [
        {"node_id": "X", "description": "has, a comma", "entity_type": "C", "importance": 0.5},
    ]
    text, _, _ = render_community_text({"X"}, entities, [])
    assert '"has, a comma"' in text


_MOCK_REPORT = {
    "title": "휴가 규정 커뮤니티",
    "summary": "이 커뮤니티는 휴가 규정 문서를 중심으로 구성됩니다.",
    "rating": 7.5,
    "rating_explanation": "조직 운영에 직접적 영향을 주므로 중요도가 높습니다.",
    "findings": [
        {"summary": "핵심 문서", "explanation": "휴가 규정이 중심입니다 [Data: Entities (1)]."},
        {"summary": "개정 이력", "explanation": "여러 개정일이 존재합니다 [Data: Relationships (1)]."},
    ],
}


def test_generate_report_parses_plain_json():
    def llm(system, user):
        return json.dumps(_MOCK_REPORT, ensure_ascii=False)

    entities = [
        {"node_id": "휴가규정", "description": "leave doc", "entity_type": "DOCUMENT", "importance": 0.5},
        {"node_id": "2007", "description": "revision date", "entity_type": "DATE", "importance": 0.5},
    ]
    relations = [
        {"source_id": "휴가규정", "target_id": "2007", "description": "revised",
         "relation_type": "REVISION_DATE", "weight": 0.5},
    ]
    rep = generate_report({"휴가규정", "2007"}, entities, relations, llm=llm)

    assert rep["title"] == "휴가 규정 커뮤니티"
    assert rep["rank"] == 7.5
    assert len(rep["findings"]) == 2
    assert rep["findings"][0]["summary"] == "핵심 문서"
    # rating_explanation folded into summary (no column for it)
    assert "조직 운영에 직접적 영향" in rep["summary"]
    assert rep["entity_ids"] == sorted(["휴가규정", "2007"])


def test_generate_report_handles_fenced_and_no_think_output():
    def llm(system, user):
        return "/no_think\n```json\n" + json.dumps(_MOCK_REPORT, ensure_ascii=False) + "\n```\n"

    entities = [{"node_id": "A", "description": "d", "entity_type": "C", "importance": 0.5}]
    rep = generate_report({"A"}, entities, [], llm=llm)
    assert rep["title"] == "휴가 규정 커뮤니티"
    assert rep["rank"] == 7.5


def test_extract_json_from_surrounding_prose():
    raw = 'Here is the report you asked for:\n{"title":"t","summary":"s","findings":[]}\nThanks!'
    parsed = _extract_json(raw)
    assert parsed["title"] == "t"
    assert parsed["findings"] == []


def test_extract_json_raises_when_absent():
    with pytest.raises(ValueError):
        _extract_json("no json here at all")


def test_generate_report_tolerates_non_numeric_rating():
    bad = dict(_MOCK_REPORT)
    bad["rating"] = "high"

    def llm(system, user):
        return json.dumps(bad, ensure_ascii=False)

    entities = [{"node_id": "A", "description": "d", "entity_type": "C", "importance": 0.5}]
    rep = generate_report({"A"}, entities, [], llm=llm)
    assert rep["rank"] is None


# --------------------------------------------------------------------------- #
# global_query map-reduce
# --------------------------------------------------------------------------- #
def _stored_reports():
    return [
        {
            "community_id": 0,
            "title": "휴가 규정 커뮤니티",
            "summary": "휴가 규정 문서와 개정일",
            "findings": [{"summary": "연차", "explanation": "연차 휴가 규정"}],
            "rank": 8.0,
            "entity_ids": ["휴가규정"],
        },
        {
            "community_id": 1,
            "title": "사무 비품 커뮤니티",
            "summary": "사무실 비품 관련",
            "findings": [{"summary": "비품", "explanation": "프린터 토너"}],
            "rank": 3.0,
            "entity_ids": ["비품"],
        },
    ]


def test_global_query_map_reduce_combines_relevant_reports():
    calls = {"map": 0, "reduce": 0}

    def llm(system, user):
        if "synthesize" in system:  # reduce step
            calls["reduce"] += 1
            return "최종 답변: 연차 휴가 규정이 핵심입니다 [Community: 0]."
        # map step — relevance keyed off the report's title in the prompt
        calls["map"] += 1
        if "휴가 규정 커뮤니티" in user:
            return "연차 휴가 규정 관련 정보"
        return "NONE"

    result = global_query(
        "휴가 규정에서 연차는 어떻게 되나요?",
        "ws",
        llm=llm,
        reports=_stored_reports(),
        top_k=5,
    )

    # report 0 (휴가) is relevant, report 1 returns NONE -> filtered out
    assert result["communities_used"] == [0]
    assert "최종 답변" in result["answer"]
    assert calls["reduce"] == 1
    assert calls["map"] >= 1


def test_global_query_returns_fallback_when_all_none():
    def llm(system, user):
        if "synthesize" in system:
            raise AssertionError("reduce must not run when no partials")
        return "NONE"

    result = global_query("무관한 질문 xyzzy", "ws", llm=llm, reports=_stored_reports())
    assert result["communities_used"] == []
    assert "찾지 못했" in result["answer"]


def test_rank_reports_prefers_keyword_overlap():
    ranked = _rank_reports(_stored_reports(), "휴가 연차", top_k=1)
    assert len(ranked) == 1
    assert ranked[0]["community_id"] == 0
