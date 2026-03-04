---
name: metadata-mapping
description: Maps Azure Databricks Unity Catalog technical column names to business terms and semantic definitions for the agents data domain. Enrich, suppsupplement and clarify the table information that did not provided. the question related to the data analytics, data query, schema search and data domain context, use this skill to improve accuracy and relevance of SQL generation by providing clear mappings and explanations of the underlying data. 
---

# metadata-mapping

- Provides semantic mappings between low-level Databricks column names and human-readable.
- business concepts used in the databricks data platform.
- All content below must also match the Chinese questions. 

## Column Name Mapping

| Technical Clolumn Name | Business Term | Notes |
|----------------|---------------|-------|
| vin | Vehicle Identification Number | 17-char ISO 3779 standard |
| abd | Azure Databricks | Platform shorthand |
| veh_mfg | Vehicle Manufacturer | OEM name |
| odm_km | Odometer Reading (km) | Cumulative distance |
| soc_pct | State of Charge (%) | EV battery level 0–100 |
| batt_temp_c | Battery Temperature (°C) | Cell temperature average |
| chg_cyc | Charge Cycle Count | Full equivalent cycles |
| trp_dur_s | Trip Duration (seconds) | Per-trip elapsed time |
| ev_range_km | EV Range (km) | Estimated remaining range |
| fault_code | Diagnostic Trouble Code | OBD-II / OEM code |
| ts_utc | Timestamp UTC | Event time in UTC |
| UnitPriceDiscount | discount amount for Unit price | 0.1 represents 10% |
| OnlineOrderFlag | whether it is an online order | 1 / 0 represents true / false |

## Business Domain Context

- Select useful information that is relevant to the current query data; ignore irrelevant information.
- The visible data is divided into two schemas: silver and gold.
- Give priority to using tables in the gold schema. If the required data is not available, then look for it in the silver schema.
- Leverage the metadata descriptions and explanations already configured in Databricks Unity Catalog (UC). If there is any conflict with this skill, give priority to UC.

## When to Use

- **Understanding column semantics** — "what does `UnitPriceDiscount` mean?"
- **Translating user questions** — "which column in the table can represent the customer name (buyer)?"
- **Enriching LLM context** before DataInsightAgent and MetaDataAgent generates SQL.
- **cannot be find the data directly** find data in silver schema if you can't find the data in gold schema, and use the metadata mapping to understand the data meaning and structure.

## When to Fetch Full Page

Fetch after skill discovery when:
- User question contains **vehicle performance**, **battery health**, **range**, **charging**, or **fault** analysis.
- DataInsightAgent needs to write SQL involving columns it has never seen before.
- MetaDataAgent needs to map user-friendly business terms to technical column names for accurate retrieval.

## Why Use This

- **Accuracy** — prevents the LLM from guessing column names.
- **Speed** — eliminates a round-trip to UC just to identify column semantics.
