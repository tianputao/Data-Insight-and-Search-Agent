---
name: metadata-mapping
description: Maps common English and Chinese AdventureWorks terms to verified Unity Catalog tables, columns, grains, and physical join keys. Load for ontology-disabled metadata discovery and ambiguous business-to-column translation.
---

# metadata-mapping

This Skill is a lexical-to-physical adapter. It helps MetadataAgent find candidate Unity Catalog
objects when Ontology is disabled; it is not a business ontology or an analytics specification.

## Boundary

- Translate user terms into candidate `table.column` names, then call `search_tables` and
  `get_table_details`. Only returned Unity Catalog metadata is executable.
- Prefix the relative names below only with the catalog and schema exposed by the current runtime.
- Do not define official thresholds, derived classes, customer classifications, category rollups,
  relationship inference, metric formulas, default aggregations, or comparison methods.
- If a request depends on an official business definition, hierarchy, or inferred semantic path
  while Ontology is disabled, report that the definition is unavailable and ask the user to define
  it. Do not reconstruct it from this Skill.
- Unity Catalog is authoritative for physical availability, spelling, type, keys, and joinability.

## Business Domain Context

- Catalog and schema availability comes only from the runtime Databricks configuration and Unity Catalog tool results.
- Never assume that `gold`, `silver`, or any other schema exists. Search only the schemas advertised as configured for the current agent run.
- Do not prefer one schema over another unless the current runtime context explicitly defines that preference.
- The available AdventureWorks objects cover customer, address, order, order-line, product, and
  product-category records.

## Verified Table Grain

| Relative table | One row represents | Primary or identifying columns |
|---|---|---|
| `salescustomer` | One customer record | `CustomerID` |
| `salescustomeraddress` | One customer-address-role record | `CustomerID`, `AddressID`, `AddressType` |
| `salesaddress` | One postal-address record | `AddressID` |
| `salesorderheader` | One sales-order header | `SalesOrderID` |
| `salesorderdetail` | One product line in an order | `SalesOrderID`, `ProductID` |
| `salesproduct` | One product record | `ProductID` |
| `salesproductcategory` | One product-category record | `ProductCategoryID` |

## Customer Field Aliases

| User terms | Verified physical column | UC type | Physical note |
|---|---|---|---|
| customer id / 客户编号、客户ID | `salescustomer.CustomerID` | `INT` | Customer-table identifier. |
| company name / 公司名、企业名称 | `salescustomer.CompanyName` | `STRING` | Company-name field; may be empty. |
| email address / 电子邮箱、邮箱 | `salescustomer.EmailAddress` | `STRING` | Email field. |
| first name / 名 | `salescustomer.FirstName` | `STRING` | Given-name component. |
| middle name / 中间名 | `salescustomer.MiddleName` | `STRING` | Optional middle-name component. |
| last name / 姓 | `salescustomer.LastName` | `STRING` | Family-name component. |
| name suffix / 姓名后缀 | `salescustomer.Suffix` | `STRING` | The physical column is not `NameSuffix`. |
| phone / 电话、手机号 | `salescustomer.Phone` | `STRING` | Phone field. |

There is no verified `CustomerName` column. A request for customer/person name requires the
available name-component columns. Keep person-name fields and `CompanyName` as separate candidates.

## Address Field Aliases

| User terms | Verified physical column | UC type | Physical note |
|---|---|---|---|
| address id / 地址编号、地址ID | `salesaddress.AddressID` | `INT` | Address-table identifier. |
| address line 1 / 地址行1 | `salesaddress.AddressLine1` | `STRING` | First street-address line. |
| address line 2 / 地址行2 | `salesaddress.AddressLine2` | `STRING` | Optional second address line. |
| city / 城市 | `salesaddress.City` | `STRING` | City field. |
| state, province, region / 州、省、地区 | `salesaddress.StateProvince` | `STRING` | Address attribute; address role remains a separate choice. |
| country, country region / 国家、国家地区 | `salesaddress.CountryRegion` | `STRING` | Country/region field. |
| postal code, ZIP / 邮编、邮政编码 | `salesaddress.PostalCode` | `STRING` | Postal-code field. |

## Sales Order Field Aliases

| User terms | Verified physical column | UC type | Physical note |
|---|---|---|---|
| sales order id / 订单编号、订单ID | `salesorderheader.SalesOrderID` | `INT` | Order-header identifier. |
| sales order number / 订单号 | `salesorderheader.SalesOrderNumber` | `STRING` | Business-facing order number. |
| revision number / 版本号 | `salesorderheader.RevisionNumber` | `INT` | Revision field. |
| order date / 下单日期 | `salesorderheader.OrderDate` | `DATE` | Order-placement date field. |
| due date / 应交日期 | `salesorderheader.DueDate` | `TIMESTAMP` | Due date/time field. |
| ship date / 发货日期 | `salesorderheader.ShipDate` | `TIMESTAMP` | Shipping date/time field. |
| status code / 状态代码、订单状态 | `salesorderheader.Status` | `INT` | The physical column is not `StatusCode`. |
| online order flag / 线上订单标志、是否在线下单 | `salesorderheader.OnlineOrderFlag` | `BOOLEAN` | Boolean field; this Skill does not define a derived order class. |
| purchase order number / 采购单号 | `salesorderheader.PurchaseOrderNumber` | `STRING` | Purchase-order field. |
| account number / 账户号 | `salesorderheader.AccountNumber` | `STRING` | Account-number field. |
| sub total / 小计、订单小计 | `salesorderheader.SubTotal` | `DOUBLE` | Order-header amount field, USD. |
| tax amount / 税额 | `salesorderheader.TaxAmt` | `DOUBLE` | Order-header tax field, USD. |
| freight / 运费 | `salesorderheader.Freight` | `DOUBLE` | Order-header freight field, USD. |
| total due, spending / 应付总额、消费 | `salesorderheader.TotalDue` | `DOUBLE` | Order-header amount field, USD; no aggregation is prescribed here. |

## Sales Order Line Field Aliases

| User terms | Verified physical column | UC type | Physical note |
|---|---|---|---|
| order quantity, units / 数量、件数 | `salesorderdetail.OrderQty` | `INT` | Quantity on one physical order line. |
| unit price / 单价 | `salesorderdetail.UnitPrice` | `DOUBLE` | Line-level unit-price field, USD. |
| unit price discount / 单价折扣率、折扣字段 | `salesorderdetail.UnitPriceDIscount` | `DOUBLE` | Fraction field; preserve the verified `DIscount` spelling. |
| line total / 行小计、明细金额 | `salesorderdetail.LineTotal` | `DOUBLE` | Line-level amount field, USD. |

Terms such as 销量, 销售额, 营收, 成交量, and 客户总消费 imply an analytical operation or metric
definition rather than a simple field lookup. This Skill may expose candidate quantity/amount
columns, but it must not choose an aggregation, grain, inclusion rule, or formula for those terms.

## Product Field Aliases

| User terms | Verified physical column | UC type | Physical note |
|---|---|---|---|
| product id / 产品编号、产品ID | `salesproduct.ProductID` | `INT` | Product-table identifier. |
| product name / 产品名、商品名 | `salesproduct.Name` | `STRING` | Product display-name field; the physical name is generic. |
| product number / 产品货号 | `salesproduct.ProductNumber` | `STRING` | Business product-number field. |
| color / 颜色 | `salesproduct.Color` | `STRING` | Product color field. |
| list price / 标价 | `salesproduct.ListPrice` | `DOUBLE` | Product list-price field, USD. |
| standard cost / 标准成本 | `salesproduct.StandardCost` | `DOUBLE` | Product standard-cost field, USD. |
| size / 尺寸、服装尺寸、车架尺寸 | `salesproduct.Size` | `STRING` | Overloaded text field; category-specific meaning is not defined here. |
| weight / 重量 | `salesproduct.Weight` | `DOUBLE` | Product weight field; verify units before analysis. |

## Product Category Field Aliases

| User terms | Verified physical column | UC type | Physical note |
|---|---|---|---|
| category id / 类目编号、产品类别编号 | `salesproductcategory.ProductCategoryID` | `INT` | Category-table identifier. |
| parent category id / 父类目编号 | `salesproductcategory.ParentProductCategoryID` | `INT` | Nullable direct parent-key field; this Skill does not define category rollups. |
| category name / 类目名、产品类别名 | `salesproductcategory.Name` | `STRING` | Category display-name field; distinct from product `Name`. |


## Ambiguity Rules

- `salesproduct.Name` is product name; `salesproductcategory.Name` is category name.
- `CustomerID`, `SalesOrderID`, `ProductID`, and `AddressID` occur in multiple tables. Keep every
  reference table-qualified and verify whether it is an identifier or join key.
- `salesproduct.Size` is overloaded. Do not infer clothing-size or frame-size semantics without an
  explicit definition or Ontology evidence.
- Region fields do not select shipping, billing, or customer-address role by themselves. Return all
  verified role candidates and keys without choosing one; downstream planning applies ontology and
  question evidence, states any reasonable assumption, and asks only when no supported default exists.
- Amount and quantity fields exist at different physical grains. Return candidates and table grains;
  do not choose an aggregation or silently convert one grain into another.
- Official concepts such as high-value orders, customer classes, product-category rollups, and
  derived semantic classes require Ontology evidence or a user-provided definition.

## Required Workflow

1. Select only aliases relevant to the current question.
2. Call `search_tables` in configured schemas.
3. Call `get_table_details` for every selected table.
4. Return only verified columns, types, grains, and physical key candidates.
5. Report unresolved business definitions and all verified role candidates explicitly. Never fill
  them with a rule from this Skill or require clarification merely because several candidates exist.
