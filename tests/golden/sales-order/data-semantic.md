# Sales Order semantics

## Metrics

### net_revenue

Revenue excluding tax.

- Expression: `SUM(orders.net_amount)`
- Synonyms: net sales
- Examples: What was net revenue last month?

## Relationships

### orders_customer

Each order belongs to one customer.

- From: `orders.customer_id`
- To: `customers.customer_id`
