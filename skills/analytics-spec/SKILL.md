---
name: analytics-spec
description: Silver-schema SQL template for finding the single customer with the highest total spending in a specified time period. Load only for semantically equivalent highest-spending-customer questions in English or Chinese.
---

# analytics-spec

- Routes one analytical intent to a governed SQL resource: the single customer with the highest total spending in a specified time period.
- Applies equally to semantically equivalent English and Chinese questions.

## Match This Skill

- Match only when all conditions hold:
  - metric: total customer spending;
  - grain: customer;
  - cardinality: exactly one highest-spending customer;
  - time: an explicit year, quarter, month, date range, or relative period.
- Matching examples include **Which customer had the highest spending in 2023?**, **Who was the top-spending customer last quarter?**, **哪个客户在2023年的消费最高？**, and **上季度消费总额最高的客户是谁？**
- Do not load this skill for product/category analysis, trends, distributions, lowest-spending customers, Top-N customer lists, customer rankings, or unrelated KPIs.

## Resource Index

| Intent | Resource | Use |
|---|---|---|
| Single highest-spending customer in a period | `references/highest-spending-customer.sql` | Read after this Skill matches; use its SQL structure without redesigning it. |

## Required Workflow

1. Call `read_skill_resource` with:
   - `skill_name`: `analytics-spec`
   - `resource_name`: `references/highest-spending-customer.sql`
2. Follow the time-mapping comments in the resource and derive `start_date` (inclusive) and `end_date` (exclusive).
3. Substitute only the resource's `{{start_date}}` and `{{end_date}}` placeholders.
4. Do not redesign the query, change its tables or joins, expand it to Top-N, or alter its Top-1 ordering.
5. Execute the resulting SQL with `execute_sql`.
