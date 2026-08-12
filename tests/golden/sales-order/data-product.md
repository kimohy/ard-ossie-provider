# Sales Order

- Product ID: `prd_0198f6c2-8ac7-7f31-a48e-1c3d82e9a631`
- Product key: `sales-order`
- Version: `v12`

## Overview

- **Description:** Order analytics product.
- **Purpose:** Analyze customer purchase orders.

## Data source

- **Domain:** Sales
- **Data type:** Transactional
- **Storage location:** BigQuery analytics.sales
- **Source system:** ERP
- **Source name:** Order Management

## Tags

- **Tag:** Orders

## Access and security

- **Access:** Approved analysts
- **Security classification:** Internal

## Ownership

- **Owner:** Sales Data Office
- **Contact:** sales-data@example.com
- **Consumer:** Sales analysts

## Freshness and SLA

- **Refresh schedule:** Daily at 02:00 UTC
- **Freshness:** Within 24 hours
- **SLA:** 99.9% monthly availability

## AI readiness and quality

- **AI readiness:** Documented semantics and stable identifiers
- **Quality:** Primary keys are validated

## Constraints and notes

- **Constraint:** Tax is excluded from net revenue
- **Related link:** https://example.com/orders

## Datasets

| Dataset | Table ID | Table version | Source |
|---|---|---:|---|
| orders | `tbl_0198f6ca-2a11-78d1-8672-67d49e69f14c` | v7 | `analytics.sales.orders` |
| customers | `tbl_0198f6ca-2a11-78d1-8672-67d49e69f14d` | v3 | `analytics.sales.customers` |
