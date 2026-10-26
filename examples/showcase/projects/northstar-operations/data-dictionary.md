# Data dictionary
Synthetic weekly records for a fictional operating review.

- week: ordered reporting period, W01 to W06.
- orders: fulfilled orders, integer count.
- revenue_usd: illustrative recognized revenue, USD.
- cost_usd: direct cost, USD; excludes overhead.
- support_tickets: tickets opened; not unique customers.

Gross margin = (revenue - direct cost) / revenue. Ticket rate = tickets / orders. Neither is a causal measure. The sample excludes seasonality, returns and acquisition spend.
