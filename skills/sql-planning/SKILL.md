---
name: sql-planning
description: Dynamically plans ad-hoc Databricks SQL from user intent, optional OWL business semantics, and verified Unity Catalog metadata. Load for every analysis that does not match a governed SQL template.
metadata:
  tags: ontology, sql, databricks, dynamic-planning
---

# SQL Planning

Use this Skill to turn semantic evidence into a query plan at runtime. It defines a planning
process, not a metric catalog, fixed analytical formula, default comparison, or SQL template.

## Authority Boundaries

- The original user question determines the analytical objective.
- When available, ontology context is authoritative for business meaning: entities, property
   domains and ranges, labels, definitions, class restrictions, hierarchy, relationship roles, and
   semantic paths. When unavailable, do not manufacture ontology evidence.
- Unity Catalog metadata is authoritative for physical tables, columns, data types, keys, join
  directions, cardinality, and availability.
- Neither an ontology entity name nor a candidate mapping is an executable SQL identifier until
  MetadataAgent has verified it.

## Ambiguity Resolution

- Inspect the complete verified metadata evidence, including raw `get_table_details` results. A
   column present there is available even if the MetadataAgent prose summary omits it.
- A **formal-definition gap** means the request requires an official threshold, derived class,
   hierarchy, formula, inclusion rule, or semantic relationship that neither the question nor the
   ontology defines. Do not invent it; ask for that precise definition when it is necessary.
- An **operational-choice ambiguity** means multiple verified columns, roles, time fields, or
   granularities can implement an otherwise clear request. This is not a missing-metadata error.
   Rank the candidates by explicit user wording, ontology role/path evidence, grain compatibility,
   and analytical usefulness. Choose the best-supported candidate, state the assumption, and execute.
- Ask the user to choose only when the alternatives imply materially different business intent and
   neither the question, ontology, metadata, nor observed data supports a reasonable default.
- For a generic geography request, follow an explicitly named ontology address role. If no role is
   named, use the verified transaction-linked address role most aligned with the event being analyzed
   (for example, ship-to for delivered-sales geography) and disclose it. Use a verified geographic
   level that can distinguish the requested groups; if a broader level is constant and a finer level
   is available, use the finer level and state that choice.
- Reconcile semantic property names and labels with verified column names case-insensitively, but
   use the exact Unity Catalog spelling in SQL.

## Dynamic Planning Workflow

1. Identify the requested outcome from the original question. Select the relevant ontology
   measure, dimension, restriction, hierarchy, and relationship path; do not automatically use
   every suggested factor.
2. Derive the analytical grain from the selected measure's semantic domain and the grouping or
   entity requested by the user. Preserve that grain before joining one-to-many relationships so
   values are not duplicated.
3. Reconcile the ordered semantic path with verified physical joins. Request metadata recovery only
   when the complete raw metadata evidence lacks a required table, column, or join. Resolve multiple
   verified implementation candidates with the ambiguity policy above.
4. Design the SQL from the selected semantics and verified schema. The model decides at runtime
   whether the question needs one query or multiple evidence queries. Build ratios from compatible
   aggregates at the same grain, and aggregate at the target grain before applying a ranking window.
5. For trend, comparison, or explanatory questions, choose periods, baselines, mathematical
   decompositions, and candidate dimensions only when they follow from the requested metric,
   ontology relationships or restrictions, and available physical data. Use no default baseline,
   decomposition, or dimension list.
6. Execute the smallest evidence set that answers the question. Distinguish measured observations
   from explanatory hypotheses, and do not claim causality without appropriate evidence.

## Degenerate Result Diagnostics

`execute_sql` profiles every result for empty output, all-zero comparison fields, pairwise-identical
measures, constant measures, and unexpectedly low sample size. If its `<result_diagnostics>` block
sets `requires_follow_up=true`:

1. Do not finalize the answer from the degenerate ranking or comparison.
2. Execute exactly one focused follow-up with `purpose="diagnostic"`.
3. Build that query from the selected ontology entities/relations and verified physical schema; do
   not use domain-specific column names from examples or guess unverified identifiers.
4. Test the smallest source-level explanation that discriminates among: equal source roles/values,
   equality introduced by aggregation or grain, missing/null mappings, low distinct cardinality,
   and insufficient period/sample coverage.
5. Report measured diagnostics separately from hypotheses. A tied result is not a meaningful Top-N;
   state that the requested ranking has no discriminatory power when the diagnostic confirms it.

## SQL Engineering

- Use verified fully qualified `catalog.schema.table` names and explicit columns. Avoid `SELECT *`.
- Generate read-only SparkSQL/Databricks SQL and apply the configured row limit unless the user
   explicitly requests all rows.
- Preserve the selected metric's grain across one-to-many joins. Aggregate each source at the
   required grain before combining values that would otherwise be duplicated.
- Build ratios from numerator and denominator aggregates at compatible grains.
- Aggregate before applying ranking windows; keep ranking and output cardinality separate from the
   metric calculation.
- Prefer ANSI SQL constructs, using Spark-specific functions only when they are needed.
- On an execution error, use the returned error and verified metadata to make one focused repair;
   do not change the user's metric or business definition merely to make the query run.

## Prohibited Shortcuts

- Do not substitute a familiar KPI when the ontology identifies a different measure or unit.
- Do not assume that an order-level amount can be summed after a line-level join.
- Do not invent a business definition, threshold, category rollup, or relationship role that is
  absent from the ontology and the question.
- Do not reuse a comparison or decomposition merely because it worked for another metric.
- Do not generate physical identifiers from naming similarity alone.