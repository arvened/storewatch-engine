"""Platform adapters. Each module exposes NAME and fetch(client, store, max_pages)."""

from . import shopify, structured, woocommerce

# Detection order: cheapest and most precise first.
ADAPTERS = {m.NAME: m for m in (shopify, woocommerce, structured)}
DETECT_ORDER = [shopify.NAME, woocommerce.NAME, structured.NAME]
