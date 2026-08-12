"""Native MAF SkillsProvider discovery, isolation, and loading tests."""

import asyncio
import re

from agent_framework import SkillsSourceContext

from src.agents.data_insight_agent import DataInsightAgent
from src.agents.metadata_agent import MetadataAgent
from src.agents.ontology_agent import OntologyAgent
from src.config import AzureOpenAIConfig
from src.ontology import OntologyService
from src.skills_provider import (
    begin_skill_usage_tracking,
    configured_skill_resource_exists,
    list_skill_metadata,
    read_configured_skill,
    reset_skill_usage_tracking,
    skill_resource_was_read,
    skill_was_loaded,
)


def test_agent_scoped_native_skills() -> None:
    async def run_test() -> None:
        metadata_agent = MetadataAgent()
        data_agent = DataInsightAgent(metadata_agent=metadata_agent)
        ontology_service = OntologyService(enable_reasoner=False).load()
        ontology_agent = OntologyAgent(ontology_service)

        try:
            discovered = await list_skill_metadata(metadata_agent.agent)
            assert {item["name"] for item in discovered} == {
                "analytics-spec",
                "metadata-mapping",
                "sql-planning",
            }
            assert len(metadata_agent.verifier_agent.context_providers) == 0
            assert metadata_agent.agent.client.model == AzureOpenAIConfig.SMALL_GPT_DEPLOYMENT
            assert metadata_agent.verifier_agent.client.model == AzureOpenAIConfig.SMALL_GPT_DEPLOYMENT
            assert ontology_agent.agent.client.model == AzureOpenAIConfig.GPT_DEPLOYMENT
            assert data_agent.agent.client.model == AzureOpenAIConfig.GPT_DEPLOYMENT

            # Ontology Skill routing runs on the tool-free router; the tool-using agent
            # is only the recovery stage and must not re-run routing.
            assert len(ontology_agent.agent.context_providers) == 0

            for agent, expected_names in (
                (metadata_agent.agent, {"metadata-mapping"}),
                (ontology_agent.router_agent, {"analytics-spec"}),
                (data_agent.agent, {"analytics-spec", "sql-planning"}),
            ):
                assert len(agent.context_providers) == 1
                provider = agent.context_providers[0]
                skills, _, tools = await provider._create_context(
                    SkillsSourceContext(
                        agent=agent,
                        session=agent.create_session(),
                    )
                )
                assert {skill.frontmatter.name for skill in skills} == expected_names

                load_skill = next(tool for tool in tools if tool.name == "load_skill")
                assert load_skill.approval_mode == "never_require"
                usage_token = begin_skill_usage_tracking()
                for expected_name in expected_names:
                    result = await asyncio.create_task(
                        load_skill.invoke(arguments={"skill_name": expected_name})
                    )
                    assert result[0].type == "text"
                    assert expected_name in result[0].text
                    assert skill_was_loaded(expected_name)

                    if expected_name == "sql-planning":
                        assert "Use no default baseline" in result[0].text
                        assert "Avoid `SELECT *`" in result[0].text
                        assert "Aggregate before applying ranking windows" in result[0].text
                        # The ambiguity policy lives in the always-present system prompt; the
                        # Skill only defers to it and adds what the prompt does not cover.
                        assert "Apply that policy" in result[0].text
                        assert "operational-choice ambiguity" not in result[0].text
                        assert "transaction-linked address role" in result[0].text
                        assert "runner_up" not in result[0].text
                        assert "average_order_value" not in result[0].text

                    if expected_name != "analytics-spec":
                        continue

                    assert "FROM ai_data_insight.silver.salesorderheader" not in result[0].text
                    assert "references/highest-spending-customer.sql" in result[0].text

                    read_resource = next(
                        tool for tool in tools if tool.name == "read_skill_resource"
                    )
                    resource_result = await asyncio.create_task(
                        read_resource.invoke(
                            arguments={
                                "skill_name": expected_name,
                                "resource_name": "references/highest-spending-customer.sql",
                            }
                        )
                    )
                    assert resource_result[0].type == "text"
                    assert "FROM ai_data_insight.silver.salesorderheader" in resource_result[0].text
                    assert "LIMIT 1" in resource_result[0].text
                    assert skill_resource_was_read(
                        expected_name,
                        "references/highest-spending-customer.sql",
                    )

                run_script = next(tool for tool in tools if tool.name == "run_skill_script")
                assert run_script.approval_mode == "always_require"
                reset_skill_usage_tracking(usage_token)
        finally:
            ontology_service.close()

    asyncio.run(run_test())


def test_governed_skill_resources_are_validated_from_skill_files() -> None:
    assert configured_skill_resource_exists(
        "OntologyAgent",
        "analytics-spec",
        "references/highest-spending-customer.sql",
    )
    assert not configured_skill_resource_exists(
        "OntologyAgent",
        "analytics-spec",
        "../metadata-mapping/SKILL.md",
    )
    assert not configured_skill_resource_exists(
        "OntologyAgent",
        "sql-planning",
        "SKILL.md",
    )


def test_metadata_mapping_is_physical_adapter_not_ontology_copy() -> None:
    skill = read_configured_skill("MetadataAgent", "metadata-mapping")
    compact_skill = " ".join(skill.split())
    verified_columns = {
        "salesaddress": {
            "AddressID", "AddressLine1", "AddressLine2", "City",
            "StateProvince", "CountryRegion", "PostalCode",
        },
        "salescustomer": {
            "CustomerID", "Title", "FirstName", "MiddleName", "LastName",
            "Suffix", "CompanyName", "EmailAddress", "Phone",
        },
        "salescustomeraddress": {"CustomerID", "AddressID", "AddressType"},
        "salesorderdetail": {
            "SalesOrderID", "OrderQty", "ProductID", "UnitPrice",
            "UnitPriceDIscount", "LineTotal",
        },
        "salesorderheader": {
            "SalesOrderID", "RevisionNumber", "DueDate", "ShipDate", "Status",
            "OnlineOrderFlag", "SalesOrderNumber", "PurchaseOrderNumber",
            "AccountNumber", "CustomerID", "ShipToAddressID", "BillToAddressID",
            "ShipMethod", "SubTotal", "TaxAmt", "Freight", "TotalDue", "OrderDate",
        },
        "salesproduct": {
            "ProductID", "Name", "ProductNumber", "Color", "StandardCost",
            "ListPrice", "Size", "Weight", "ProductCategoryID",
        },
        "salesproductcategory": {
            "ProductCategoryID", "ParentProductCategoryID", "Name",
        },
    }
    physical_references = set(
        re.findall(r"\b(sales[a-z]+)\.([A-Za-z][A-Za-z0-9]*)\b", skill)
    )
    unknown_references = sorted(
        f"{table}.{column}"
        for table, column in physical_references
        if table not in verified_columns or column not in verified_columns[table]
    )
    assert unknown_references == []

    for required_mapping in (
        "name suffix / 姓名后缀 | `salescustomer.Suffix`",
        "product name / 产品名、商品名 | `salesproduct.Name`",
        "status code / 状态代码、订单状态 | `salesorderheader.Status`",
        "unit price discount / 单价折扣率、折扣字段 | `salesorderdetail.UnitPriceDIscount`",
    ):
        assert required_mapping in skill

    assert "lexical-to-physical adapter" in skill
    assert "not a business ontology or an analytics specification" in skill
    assert "does not define a derived order class" in skill
    assert "do not choose an aggregation" in skill
    # Address-role selection is ontology-only; this Skill must not carry a region-role rule.
    assert "address role" not in compact_skill
    assert "all verified role candidates explicitly" in compact_skill

    forbidden_ontology_copies = {
        "placedOrder", "placedBy", "hasOrderLine", "partOfOrder",
        "refersToProduct", "soldIn", "inCategory", "containsProduct",
        "hasParentCategory", "hasShipToAddress", "hasBillToAddress",
        "hasAddressLink", "linksAddress", "HighValueOrder",
        "DiscountedOrderLine", "BusinessCustomer", "IndividualCustomer",
    }
    assert all(term not in skill for term in forbidden_ontology_copies)
    assert "Defined Business Concepts" not in skill
    assert ">= 500" not in skill
    assert "SUM(" not in skill
    assert "COUNT(" not in skill
    assert "CompanyName` is present and non-empty" not in skill
    assert "CompanyName` is null or empty" not in skill

    obsolete_vehicle_terms = {
        "veh_mfg", "odm_km", "soc_pct", "batt_temp_c", "chg_cyc",
        "trp_dur_s", "ev_range_km", "fault_code", "ts_utc",
    }
    assert obsolete_vehicle_terms.isdisjoint(skill.casefold().split())
