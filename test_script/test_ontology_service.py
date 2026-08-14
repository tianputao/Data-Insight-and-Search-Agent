"""Read-only Owlready2 ontology service tests."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from src.ontology import OntologyService


@pytest.fixture(scope="module")
def ontology_service():
    service = OntologyService(enable_reasoner=False).load()
    yield service
    service.close()


def test_loads_repository_ontology_recursively(ontology_service: OntologyService) -> None:
    health = ontology_service.health()

    assert health["available"] is True
    assert health["file_count"] >= 1
    assert health["ontology_count"] >= 1
    assert health["entity_count"] >= 400
    assert health["load_errors"] == []


@pytest.mark.parametrize(
    ("query", "entity_types", "expected_name"),
    [
        ("Customer", ["class"], "Customer"),
        ("客户", ["class"], "Customer"),
        ("Sales Ordr", ["class"], "SalesOrder"),
        ("sales_order", ["class"], "SalesOrder"),
        ("订单明细行", ["class"], "SalesOrderLine"),
    ],
)
def test_resolves_exact_multilingual_and_fuzzy_names(
    ontology_service: OntologyService,
    query: str,
    entity_types: list[str],
    expected_name: str,
) -> None:
    result = ontology_service.search_entities(query, entity_types=entity_types)

    assert result["status"] == "ok"
    assert result["matches"][0]["name"] == expected_name
    assert result["matches"][0]["evidence"]["source"] == "ontology"
    assert result["confidence"] >= 0.62


def test_returns_ranked_candidates_for_ambiguous_label(
    ontology_service: OntologyService,
) -> None:
    result = ontology_service.search_entities("Bikes")

    assert result["status"] == "ambiguous"
    assert len(result["matches"]) >= 2
    assert {match["type"] for match in result["matches"][:2]} == {
        "class",
        "individual",
    }


def test_describes_class_restrictions_and_properties(
    ontology_service: OntologyService,
) -> None:
    result = ontology_service.describe_entity("SalesOrder", entity_types=["class"])

    assert result["status"] == "ok"
    assert result["data"]["name"] == "SalesOrder"
    assert any(
        restriction["property"]["name"] == "hasShipToAddress"
        and restriction["restriction"] == "exactly"
        for restriction in result["data"]["restrictions"]
    )
    assert any(prop["name"] == "totalDue" for prop in result["data"]["properties"])


def test_expands_bounded_neighbors_and_finds_multihop_path(
    ontology_service: OntologyService,
) -> None:
    neighborhood = ontology_service.expand_neighbors("Customer", depth=2)
    paths = ontology_service.find_paths("Customer", "Product", max_depth=4)

    assert neighborhood["status"] == "ok"
    assert len(neighborhood["data"]["nodes"]) <= ontology_service.max_nodes
    assert paths["status"] == "ok"
    semantic_paths = [path["semantic_path"] for path in paths["data"]["paths"]]
    assert any(
        path[0] == "Customer"
        and "SalesOrder" in path
        and "SalesOrderLine" in path
        and path[-1] == "Product"
        for path in semantic_paths
    )


def test_finds_related_entity_by_descendant_type(
    ontology_service: OntologyService,
) -> None:
    result = ontology_service.find_related_by_type(
        "Customer",
        "Product",
        max_depth=4,
    )

    assert result["status"] == "ok"
    assert any(
        item["entity"]["name"] == "Product"
        for item in result["data"]["related"]
    )


def test_preserves_mapping_and_lineage_provenance(
    ontology_service: OntologyService,
) -> None:
    mapping = ontology_service.get_schema_mapping("SalesOrder")
    joins = ontology_service.get_join_paths("Customer", "Product", max_depth=4)
    lineage = ontology_service.get_lineage("SalesOrder")

    assert mapping["status"] == "partial"
    assert mapping["data"]["requires_metadata_resolution"] is True
    assert mapping["data"]["physical_mappings"] == {"tables": [], "columns": []}
    assert joins["status"] == "partial"
    assert joins["data"]["join_paths"]
    assert all(
        path["physical_joins"] == []
        and path["requires_metadata_resolution"] is True
        for path in joins["data"]["join_paths"]
    )
    assert lineage["status"] == "partial"
    assert lineage["data"]["has_explicit_lineage"] is False
    assert lineage["data"]["semantic_dependencies"]


def test_builds_json_safe_business_context(ontology_service: OntologyService) -> None:
    result = ontology_service.get_business_context(
        "Analyze 2023 sales order value by product category",
        root_entity="SalesOrder",
    )

    assert result["status"] in {"ok", "partial"}
    assert result["data"]["root_entity"] == "SalesOrder"
    assert result["data"]["requires_metadata_resolution"] is True
    assert result["data"]["semantic_properties"]
    assert result["data"]["filters"] == []
    json.dumps(result, ensure_ascii=False)


def test_long_question_matches_datatype_property_and_uses_its_domain(
    ontology_service: OntologyService,
) -> None:
    question = "Show the grand total due for each sales order in 2023"
    property_matches = ontology_service.search_entities(
        question,
        entity_types=["data_property"],
    )
    context = ontology_service.get_business_context(question)

    assert property_matches["status"] == "ok"
    assert property_matches["matches"][0]["name"] == "totalDue"
    assert property_matches["matches"][0]["strategy"] in {
        "substring",
        "candidate_token_coverage",
        "comment_or_label_context",
    }
    assert context["data"]["root_entity"] == "SalesOrder"
    assert context["data"]["semantic_properties"][0]["name"] == "totalDue"
    assert context["data"]["semantic_properties"][0]["relevance"] >= 0.8
    assert any(
        label["value"] == "应付总额"
        for label in context["data"]["semantic_properties"][0]["labels"]
    )
    assert any(
        "Grand total due" in comment["value"]
        for comment in context["data"]["semantic_properties"][0]["comments"]
    )


def test_business_context_is_property_first_and_leaves_roles_to_the_model(
    ontology_service: OntologyService,
) -> None:
    result = ontology_service.get_business_context(
        "哪个客户在2023年的消费是最高的"
    )
    data = result["data"]
    properties = data["semantic_properties"]
    properties_by_name = {item["name"]: item for item in properties}

    assert data["root_entity"] == "Customer"
    # Question-matched properties rank first, but every involved class keeps its full schema.
    assert properties[0]["name"] == "totalDue"
    assert {"companyName", "customerId", "firstName"} <= set(properties_by_name)
    assert all("role" not in item for item in properties)
    assert properties_by_name["totalDue"]["domain"]
    assert properties_by_name["totalDue"]["range"]
    assert any(
        path["semantic_path"] == ["Customer", "placedOrder", "SalesOrder"]
        for path in data["join_paths"]
    )
    assert len(json.dumps(result, ensure_ascii=False)) < 80000


def test_business_context_uses_question_driven_paths_and_keeps_parallel_roles(
    ontology_service: OntologyService,
) -> None:
    result = ontology_service.get_business_context(
        "2023年按发货地区和按账单地区分别统计销售额，两者差异最大的前5个地区是哪些？"
    )
    data = result["data"]
    properties = data["semantic_properties"]
    paths = [path["semantic_path"] for path in data["join_paths"]]

    assert {item["entity"] for item in properties} == {"SalesOrder", "Address"}
    assert {item["name"] for item in properties} >= {
        "totalDue",
        "countryRegion",
        "stateProvince",
    }
    assert not any(item["name"] == "weight" for item in properties)
    assert ["SalesOrder", "hasShipToAddress", "Address"] in paths
    assert ["SalesOrder", "hasBillToAddress", "Address"] in paths


def test_question_driven_paths_can_reach_explicit_concepts_beyond_three_hops(
    ontology_service: OntologyService,
) -> None:
    result = ontology_service.get_business_context(
        "Analyze high-value orders by product category",
        root_entity="HighValueOrder",
    )
    paths = [path["semantic_path"] for path in result["data"]["join_paths"]]

    assert any(
        path[0] == "HighValueOrder"
        and "SalesOrder" in path
        and "SalesOrderLine" in path
        and "Product" in path
        and path[-1] == "ProductCategory"
        for path in paths
    )


def test_business_context_surfaces_recursive_hierarchy_relations(
    ontology_service: OntologyService,
) -> None:
    result = ontology_service.get_business_context(
        "2023年各顶层产品大类的销量和订单明细行小计分别是多少？"
        "请沿产品类目层级将叶子类目汇总到顶层。"
    )
    hierarchy = result["data"]["hierarchy_relations"]

    assert any(
        item["entity"] == "ProductCategory"
        and item["relation"] == "hasParentCategory"
        and item["recursive"] is True
        for item in hierarchy
    )


def test_business_context_does_not_prescribe_an_analysis_strategy(
    ontology_service: OntologyService,
) -> None:
    result = ontology_service.get_business_context(
        "为什么2023年消费最高的客户比其他客户高，分析驱动因素和根因"
    )
    data = result["data"]

    assert "analysis_blueprint" not in data
    assert data["root_entity"] == "Customer"
    assert any(
        item["name"] == "totalDue" for item in data["semantic_properties"]
    )
    assert data["join_paths"]
    assert "runner_up" not in json.dumps(data, ensure_ascii=False)
    assert "average_order_value" not in json.dumps(data, ensure_ascii=False)


def test_long_chinese_question_keeps_region_property_and_multihop_path(
    ontology_service: OntologyService,
) -> None:
    question = "哪个地区的成交量是最高的，在这个地区那个产品销量最高，并且结合数据分析原因"

    property_matches = ontology_service.search_entities(
        question,
        entity_types=["data_property"],
    )
    result = ontology_service.get_business_context(question)
    data = result["data"]

    # 地区 must resolve to the finer, multi-valued level, not the single-valued country.
    assert {match["name"] for match in property_matches["matches"]} >= {
        "stateProvince",
        "orderQty",
    }
    assert {item["name"] for item in data["semantic_properties"]} >= {
        "stateProvince",
        "orderQty",
    }
    assert "countryRegion" not in {
        match["name"] for match in property_matches["matches"]
    }
    assert any(
        path["semantic_path"][0] == "Product"
        and "SalesOrderLine" in path["semantic_path"]
        and "SalesOrder" in path["semantic_path"]
        and path["semantic_path"][-1] == "Address"
        for path in data["join_paths"]
    )


@pytest.mark.parametrize(
    ("question", "expected_root"),
    [
        ("Analyze 2023 sales by product category", "ProductCategory"),
        ("哪个客户在2023年的消费最高", "Customer"),
        ("按月看2023年的销售额趋势", "SalesOrder"),
        ("按产品类别看2023年的销量", "ProductCategory"),
    ],
)
def test_discovers_root_entities_from_natural_language_without_explicit_hint(
    ontology_service: OntologyService,
    question: str,
    expected_root: str,
) -> None:
    result = ontology_service.get_business_context(question)

    assert result["status"] in {"ok", "partial"}
    assert result["data"]["root_entity"] == expected_root
    assert result["confidence"] >= 0.72
    if "产品类别" in question:
        assert any(
            candidate["name"] == "ProductCategory"
            for candidate in result["data"]["entity_candidates"]
        )


@pytest.mark.parametrize(
    "operation",
    [
        lambda service: service.search_entities("entity_that_does_not_exist"),
        lambda service: service.describe_entity("entity_that_does_not_exist"),
        lambda service: service.expand_neighbors("entity_that_does_not_exist"),
        lambda service: service.find_paths("entity_that_does_not_exist", "Product"),
        lambda service: service.find_related_by_type("Customer", "missing_type"),
        lambda service: service.get_schema_mapping("entity_that_does_not_exist"),
        lambda service: service.get_join_paths("Customer", "entity_that_does_not_exist"),
        lambda service: service.get_lineage("entity_that_does_not_exist"),
        lambda service: service.get_semantic_candidates("unmapped_nonexistent_concept"),
        lambda service: service.get_business_context("unmapped_nonexistent_concept"),
    ],
)
def test_ordinary_misses_return_stable_envelopes(
    ontology_service: OntologyService,
    operation,
) -> None:
    result = operation(ontology_service)

    assert result["status"] in {"no_match", "ambiguous", "partial"}
    assert set(result) == {
        "status",
        "data",
        "matches",
        "evidence",
        "confidence",
        "strategies_tried",
        "warnings",
        "unresolved",
    }
    json.dumps(result, ensure_ascii=False)


@pytest.mark.skipif(shutil.which("java") is None, reason="Java is required by HermiT")
def test_real_ontology_remains_queryable_when_reasoning_is_unsupported() -> None:
    service = OntologyService(enable_reasoner=True, reasoner="hermit").load()
    try:
        health = service.health()
        result = service.search_entities("Customer", entity_types=["class"])

        assert health["available"] is True
        assert health["reasoning_status"] in {"completed", "failed"}
        if health["reasoning_status"] == "failed":
            assert health["reasoning_error"]
        assert result["status"] == "ok"
    finally:
        service.close()


@pytest.mark.skipif(shutil.which("java") is None, reason="Java is required by HermiT")
def test_hermit_completes_for_supported_ontology(tmp_path: Path) -> None:
    ontology_file = tmp_path / "supported.owl"
    ontology_file.write_text(
        """<?xml version="1.0"?>
<rdf:RDF xmlns="http://example.com/test#"
  xml:base="http://example.com/test"
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="http://example.com/test"/>
  <owl:Class rdf:about="http://example.com/test#A"/>
  <owl:Class rdf:about="http://example.com/test#B">
    <owl:equivalentClass rdf:resource="http://example.com/test#A"/>
  </owl:Class>
  <owl:NamedIndividual rdf:about="http://example.com/test#sample">
    <rdf:type rdf:resource="http://example.com/test#A"/>
  </owl:NamedIndividual>
</rdf:RDF>
""",
        encoding="utf-8",
    )
    service = OntologyService(
        directory=tmp_path,
        enable_reasoner=True,
        reasoner="hermit",
    ).load()
    try:
        health = service.health()
        description = service.describe_entity("B", entity_types=["class"])

        assert health["reasoning_status"] == "completed"
        assert description["status"] == "ok"
        assert description["data"]["instance_count"] == 1
    finally:
        service.close()