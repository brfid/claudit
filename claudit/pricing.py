"""Model pricing tables and cost calculation functions."""

from typing import Dict, Optional

# USD per million tokens
# Source: https://www.anthropic.com/pricing  (verify quarterly — update here if stale)
OPUS_PRICING = {
    'input': 5.00, 'output': 25.00, 'cache_write': 6.25, 'cache_read': 0.50,
}
SONNET_PRICING = {
    'input': 3.00, 'output': 15.00, 'cache_write': 3.75, 'cache_read': 0.30,
}
HAIKU_PRICING = {
    'input': 1.00, 'output': 5.00, 'cache_write': 1.25, 'cache_read': 0.10,
}

MODEL_PRICING = {
    # Opus family
    'claude-opus-4-6': OPUS_PRICING,
    'claude-opus-4-7': OPUS_PRICING,
    # Sonnet family
    'claude-sonnet-4-5-20250929': SONNET_PRICING,
    'claude-sonnet-4-6': SONNET_PRICING,
    # Haiku family
    'claude-haiku-4-5-20251001': HAIKU_PRICING,
}

DEFAULT_PRICING = SONNET_PRICING


def _infer_pricing_by_family(model: str) -> Optional[Dict[str, float]]:
    """Fallback: infer pricing from family name if exact model ID is unknown."""
    if 'opus' in model:
        return OPUS_PRICING
    if 'sonnet' in model:
        return SONNET_PRICING
    if 'haiku' in model:
        return HAIKU_PRICING
    return None


def get_model_pricing(model: Optional[str]) -> Dict[str, float]:
    """Get pricing for a specific model, falling back to family, then defaults."""
    if not model:
        return DEFAULT_PRICING
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    inferred = _infer_pricing_by_family(model)
    return inferred if inferred is not None else DEFAULT_PRICING


def calculate_cost(tokens_in: int, tokens_out: int, cache_writes: int,
                   cache_reads: int, model: Optional[str] = None) -> float:
    """Calculate cost from token counts and model pricing."""
    pricing = get_model_pricing(model)
    return (
        tokens_in * pricing['input'] +
        tokens_out * pricing['output'] +
        cache_writes * pricing['cache_write'] +
        cache_reads * pricing['cache_read']
    ) / 1_000_000


def calculate_cache_savings(cache_reads: int,
                            model: Optional[str] = None) -> float:
    """Calculate cost savings from prompt caching."""
    if cache_reads == 0:
        return 0.0
    pricing = get_model_pricing(model)
    savings_per_million = pricing['input'] - pricing['cache_read']
    return (cache_reads * savings_per_million) / 1_000_000
