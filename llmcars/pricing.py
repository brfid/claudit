"""Model pricing tables, family registry, and cost calculation.

Canonical registry of model families. Display layers (short-name, colors)
and pricing both read from the same list so adding a new family is a
one-line change here.

`get_model_pricing()` returns ``Optional[Dict]``. Three outcomes:

- `dict` — we have rates, callers can compute cost.
- `None` — family is recognized but rates are not configured (e.g. a
  placeholder for a model whose public pricing we haven't vetted).
- Default fallback pricing — only when the model string is entirely
  unrecognized.

The `None`-for-unpriced semantics matter for `recalc_ledger_costs`: we
must not silently reprice a GPT-class entry with Sonnet rates. Callers
check for `None` and leave the stored cost alone.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

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

# Source: openai.com/pricing — published GPT-5 family rates (USD/MTok).
# These are published Azure/OpenAI direct rates; AWS Bedrock may charge a
# small premium. Update here if Bedrock's rate card diverges materially.
GPT5_PRICING = {
    'input': 1.25, 'output': 10.00, 'cache_write': 1.25, 'cache_read': 0.125,
}
GPT5_MINI_PRICING = {
    'input': 0.25, 'output': 2.00, 'cache_write': 0.25, 'cache_read': 0.025,
}
GPT5_NANO_PRICING = {
    'input': 0.05, 'output': 0.40, 'cache_write': 0.05, 'cache_read': 0.005,
}


@dataclass(frozen=True)
class ModelFamily:
    """One model family: identifying tokens + display metadata + rates.

    `tokens` match against the normalized model ID; first family whose
    token appears wins. Keep more-specific tokens earlier in the registry.
    """
    key: str                      # short name used in display (e.g. "opus")
    tokens: tuple                 # substrings that identify this family
    color: str                    # hex color for OPS model-mix widget
    pricing: Optional[Dict[str, float]]  # None = family known, rates TBD


# Ordered — first match wins. Keep longer/more-specific tokens first.
FAMILIES: List[ModelFamily] = [
    # Anthropic
    ModelFamily("opus",   ("opus",),               "#FF9900", OPUS_PRICING),
    ModelFamily("sonnet", ("sonnet",),             "#9999CC", SONNET_PRICING),
    ModelFamily("haiku",  ("haiku",),              "#CC6699", HAIKU_PRICING),
    # OpenAI (most specific first so gpt-5-nano doesn't match bare gpt-5)
    ModelFamily("gpt-5-nano", ("gpt-5-nano",),     "#66CC99", GPT5_NANO_PRICING),
    ModelFamily("gpt-5-mini", ("gpt-5-mini",),     "#66CCCC", GPT5_MINI_PRICING),
    ModelFamily("gpt-5",  ("gpt-5", "gpt5"),       "#33AA66", GPT5_PRICING),
    ModelFamily("gpt-4",  ("gpt-4", "gpt4"),       "#2E8B57", None),
    ModelFamily("gpt",    ("gpt",),                "#50B080", None),
    # Amazon Bedrock (Nova)
    ModelFamily("nova",   ("nova",),               "#FFCC33", None),
    # Google
    ModelFamily("gemini", ("gemini",),             "#4285F4", None),
    # Meta
    ModelFamily("llama",  ("llama",),              "#6A5ACD", None),
    # Mistral
    ModelFamily("mistral", ("mistral",),           "#FF7F50", None),
]

# Fast lookup: family key → ModelFamily. Used by display helpers.
FAMILIES_BY_KEY: Dict[str, ModelFamily] = {f.key: f for f in FAMILIES}

# Exact model-ID overrides. Use when a specific version diverges from the
# family default or when you want to pin pricing even if the ID contains
# a misleading substring.
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # Anthropic exact IDs
    'claude-opus-4-6': OPUS_PRICING,
    'claude-opus-4-7': OPUS_PRICING,
    'claude-sonnet-4-5-20250929': SONNET_PRICING,
    'claude-sonnet-4-6': SONNET_PRICING,
    'claude-haiku-4-5-20251001': HAIKU_PRICING,
}

# Only used when a model string is completely unrecognizable (no family
# match and no exact-ID hit). Kept as Sonnet so legacy un-attributed
# Anthropic entries don't misprice wildly. Explicit fallback, not silent.
DEFAULT_PRICING = SONNET_PRICING


def family_for_model(model: Optional[str]) -> Optional[ModelFamily]:
    """Find the ModelFamily whose tokens match ``model``, or None."""
    if not model:
        return None
    lowered = model.lower()
    for fam in FAMILIES:
        if any(tok in lowered for tok in fam.tokens):
            return fam
    return None


def get_model_pricing(model: Optional[str]) -> Optional[Dict[str, float]]:
    """Look up pricing for a model.

    Returns:
      - Exact pricing dict when the model ID is in ``MODEL_PRICING`` or
        matches a family with configured rates.
      - ``None`` when the family is recognized but rates aren't set
        (placeholder entries). Callers must treat this as "do not
        compute / do not overwrite existing cost".
      - ``DEFAULT_PRICING`` only when no family matches at all.
    """
    if not model:
        return DEFAULT_PRICING
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    fam = family_for_model(model)
    if fam is None:
        return DEFAULT_PRICING
    return fam.pricing  # may be None


def has_priced_model(model: Optional[str]) -> bool:
    """True iff we can compute a real cost for this model."""
    return get_model_pricing(model) is not None


def _infer_pricing_by_family(model: Optional[str]) -> Optional[Dict[str, float]]:
    """Compat shim: return family pricing (or None) without exact-ID lookup.

    Historically a private helper; kept as a named symbol because a few
    tests import it directly. Prefer ``family_for_model`` in new code.
    """
    fam = family_for_model(model)
    return None if fam is None else fam.pricing


def calculate_cost(tokens_in: int, tokens_out: int, cache_writes: int,
                   cache_reads: int, model: Optional[str] = None
                   ) -> Optional[float]:
    """Calculate USD cost from token counts. ``None`` when rates are absent."""
    pricing = get_model_pricing(model)
    if pricing is None:
        return None
    return (
        tokens_in * pricing['input']
        + tokens_out * pricing['output']
        + cache_writes * pricing['cache_write']
        + cache_reads * pricing['cache_read']
    ) / 1_000_000


def calculate_cache_savings(cache_reads: int,
                            model: Optional[str] = None) -> float:
    """Calculate cost savings from prompt caching. 0.0 when rates absent."""
    if cache_reads == 0:
        return 0.0
    pricing = get_model_pricing(model)
    if pricing is None:
        return 0.0
    savings_per_million = pricing['input'] - pricing['cache_read']
    return (cache_reads * savings_per_million) / 1_000_000
