# model_router.py

Selects the cheapest capable model for a given task based on capability requirements, then provides a fallback chain if the primary fails. Integrates with pricing_table.py for cost-aware selection and stats_collector.py for success-rate tracking.

## Key Exports
- `FallbackChain`: Class
- `ModelRouter`: Class
- `ProviderConfig`: Class
- `ProviderHealth`: Class
- `build_default_chain()`: Function
