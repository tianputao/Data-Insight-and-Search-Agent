---
name: analytics-spec
description: Standard analytical SQL templates and KPI definitions for Databricks, including highest customer spending in a period and product-category sales distribution in a period. Use this skill to improve stability, cardinality control, and deterministic query structure.
---

# analytics-spec

- Defines standard analytical patterns, KPI formulas, and benchmark thresholds for the databricks UC and delta lake data platform. 
- All content below must also match the Chinese questions.

## When and how to Use

- Before DataInsightAgent writes a query, load this skill to apply **Standard SQL Patterns**.
- When user asks about **Which customer had the highest spending in a given period**, **View sales by product category in a given period** or the same in Chinese, ***MANDATORILY*** use the use the corresponding SQL query statements as is in **Standard SQL Patterns** without generate new SQL and without changing the SQL statements.
- To ensure consistent KPI definitions across all analytical outputs.

## Core KPIs

| KPI | Formula | Unit | Threshold |
|-----|---------|------|-----------|
| Average Range | `AVG(ev_range_km)` | km | Target ≥ 400 km |
| Battery Health Index | `AVG(soc_pct) / MAX(soc_pct)` | ratio 0–1 | Healthy ≥ 0.85 |
| Mean Charge Cycles | `AVG(chg_cyc)` | cycles | Alert > 1 000 |
| Fleet Fault Rate | `COUNT(DISTINCT vin WHERE fault_code IS NOT NULL) / COUNT(DISTINCT vin)` | % | Alert > 5 % |
| Trip Efficiency | `SUM(odm_km) / COUNT(trp_dur_s / 3600.0)` | km/h | — |

## Standard SQL Patterns

*** !!!When a question’s semantics match the sample questions below, directly and mandatorily use the corresponding SQL query statements, and do not generate new SQL statment and do not change the SQL statement blow. This applies regardless of whether the question is in Chinese or English.!!! ***

- Which customer had the highest spending in a given period/哪个客户在某年的消费是最高的:

```sql
-- Parameter convention:
-- {{start_date}}: inclusive, format YYYY-MM-DD
-- {{end_date}}: exclusive, format YYYY-MM-DD
WITH params AS (
  SELECT
    DATE '{{start_date}}' AS start_date,
    DATE '{{end_date}}'   AS end_date
),
customer_period AS (
  SELECT
    fs.CustomerIDKey,
    fs.CustomerID,
    SUM(fs.TotalDue) AS TotalDue_period
  FROM ai_data_insight.gold.factsales fs
  CROSS JOIN params p
  WHERE fs.OrderDate >= p.start_date
    AND fs.OrderDate < p.end_date
  GROUP BY fs.CustomerIDKey, fs.CustomerID
),
ranked AS (
  SELECT
    c.CustomerIDKey,
    c.CustomerID,
    c.TotalDue_period,
    ROW_NUMBER() OVER (ORDER BY c.TotalDue_period DESC) AS rn
  FROM customer_period c
)
SELECT
  r.CustomerIDKey,
  r.CustomerID,
  r.TotalDue_period,
  dc.FirstName,
  dc.MiddleName,
  dc.LastName,
  dc.CompanyName
FROM ranked r
LEFT JOIN ai_data_insight.gold.dimcustomer dc
  ON r.CustomerIDKey = dc.CustomerIDKey
WHERE r.rn = 1
LIMIT 1
```

- View sales by product category in a given period/按产品类别看某年的销量:

```sql
-- Parameter convention:
-- {{start_date}}: inclusive, format YYYY-MM-DD
-- {{end_date}}: exclusive, format YYYY-MM-DD
WITH params AS (
  SELECT
    DATE '{{start_date}}' AS start_date,
    DATE '{{end_date}}'   AS end_date
),
category_sales AS (
  SELECT
    spc.Name AS ProductCategoryName,
    SUM(fs.OrderQty) AS TotalOrderQty
  FROM ai_data_insight.gold.factsales fs
  JOIN ai_data_insight.silver.salesproduct sp
    ON fs.ProductID = sp.ProductID
  JOIN ai_data_insight.silver.salesproductcategory spc
    ON sp.ProductCategoryID = spc.ProductCategoryID
  CROSS JOIN params p
  WHERE fs.OrderDate >= p.start_date
    AND fs.OrderDate < p.end_date
  GROUP BY spc.Name
),
summary AS (
  SELECT SUM(TotalOrderQty) AS TotalOrderQtyAll
  FROM category_sales
)
SELECT
  cs.ProductCategoryName,
  cs.TotalOrderQty,
  ROUND(
    CASE WHEN s.TotalOrderQtyAll = 0 THEN 0
         ELSE cs.TotalOrderQty / s.TotalOrderQtyAll * 100
    END
  , 2) AS OrderQtyPctOfTotal
FROM category_sales cs
CROSS JOIN summary s
ORDER BY cs.TotalOrderQty DESC
LIMIT 500
```


### Time Parameter Mapping Rules
- Always convert natural-language time into:
  - `start_date` (inclusive)
  - `end_date` (exclusive)
- Mapping examples:
  - `2023年` → `start_date='2023-01-01'`, `end_date='2024-01-01'`
  - `2024年` → `start_date='2024-01-01'`, `end_date='2025-01-01'`
  - `2024年Q1` → `start_date='2024-01-01'`, `end_date='2024-04-01'`
  - `2024年3月` → `start_date='2024-03-01'`, `end_date='2024-04-01'`
  - `最近30天` → `start_date=date_sub(current_date(), 30)`, `end_date=current_date()`
- If user provides exact range (e.g. `2024-01-15 到 2024-02-20`):
  - use as-is with exclusive end boundary when possible.

### SQL Generation Constraints
- Never hardcode a fixed year in the query body.
- Prefer half-open interval: `OrderDate >= start_date AND OrderDate < end_date`.
- Keep date literals in ISO format `YYYY-MM-DD`.


## When to Fetch Full Page

Fetch when:
- User asks for **Which customer had the highest spending in a given period**, **View sales by product category in a given period**
- DataInsightAgent needs guidance on which **aggregation logic** to apply.

## Why Use This

- **Consistency** — all agents use the same SQL to improve query accuracy and stability.
- **Governance** — thresholds and formulas are centrally maintained.
