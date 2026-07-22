-- Governed template: single highest-spending customer in a specified period.
-- Replace only {{start_date}} and {{end_date}} with ISO dates.
-- start_date is inclusive; end_date is exclusive.
-- Time mapping examples:
--   2023          -> 2023-01-01 / 2024-01-01
--   2024 Q1       -> 2024-01-01 / 2024-04-01
--   March 2024    -> 2024-03-01 / 2024-04-01
--   最近30天      -> date_sub(current_date(), 30) / current_date()
-- For an exact range, preserve the requested start and use an exclusive end boundary when possible.
WITH params AS (
  SELECT
    DATE '{{start_date}}' AS start_date,
    DATE '{{end_date}}'   AS end_date
),
customer_period AS (
  SELECT
    sc.CustomerID,
    sc.FirstName,
    sc.LastName,
    sc.CompanyName,
    sc.EmailAddress,
    COUNT(DISTINCT soh.SalesOrderID) AS order_count,
    SUM(soh.TotalDue) AS total_spending_period,
    AVG(soh.TotalDue) AS avg_order_value
  FROM ai_data_insight.silver.salesorderheader soh
  INNER JOIN ai_data_insight.silver.salescustomer sc
    ON soh.CustomerID = sc.CustomerID
  CROSS JOIN params p
  WHERE soh.OrderDate >= p.start_date
    AND soh.OrderDate < p.end_date
  GROUP BY
    sc.CustomerID,
    sc.FirstName,
    sc.LastName,
    sc.CompanyName,
    sc.EmailAddress
)
SELECT
  CustomerID,
  FirstName,
  LastName,
  CompanyName,
  EmailAddress,
  order_count,
  total_spending_period,
  avg_order_value
FROM customer_period
ORDER BY total_spending_period DESC
LIMIT 1
