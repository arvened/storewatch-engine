# storewatch-engine

Price-monitoring engine: reads public product catalogues (Shopify, WooCommerce, and any shop
with standard product markup), compares with the previous snapshot, reports price changes,
discounts, stock, new and removed products. Respects robots.txt.

This repository holds **code only** — no store lists, no prices, no reports.

## How a niche runs
- `.github/workflows/niche.yml` — daily run of one niche (copy the file for each new niche).
- Store list comes from a secret (`STORES_A`), never from a file in this repo.
- All output (reports, price history, logs, database) is written to a **private** data
  repository (`<owner>/storewatch-data`, via the `DATA_TOKEN` secret). The public log shows only
  `stores=N exit=X`; every store host is also masked.

## Tests
```
python -m unittest discover -s tests
```
