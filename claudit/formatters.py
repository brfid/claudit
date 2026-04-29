"""Field constants, number formatting, and table rendering."""

from typing import Dict

# Field name constants
FIELD_TOKENS_IN = 'tokensIn'
FIELD_TOKENS_OUT = 'tokensOut'
FIELD_CACHE_WRITES = 'cacheWrites'
FIELD_CACHE_READS = 'cacheReads'
FIELD_COST = 'cost'
FIELD_CACHE_SAVINGS = 'cacheSavings'
FIELD_REQUESTS = 'requests'

FIELD_NAMES = [
    FIELD_TOKENS_IN, FIELD_TOKENS_OUT, FIELD_CACHE_WRITES,
    FIELD_CACHE_READS, FIELD_COST, FIELD_CACHE_SAVINGS, FIELD_REQUESTS,
]

FLOAT_FIELDS = {FIELD_COST, FIELD_CACHE_SAVINGS}

SOURCE_MAP = {'claude-code': 'cc', 'cline': 'cline'}


def init_field_dict() -> Dict:
    """Initialize a dictionary with zeros for all field names."""
    return {field: 0.0 if field in FLOAT_FIELDS else 0 for field in FIELD_NAMES}


def format_number(num: int) -> str:
    """Format large numbers with K, M, B suffixes (whole numbers)."""
    if num >= 1_000_000_000:
        return f"{round(num / 1_000_000_000)}B"
    if num >= 1_000_000:
        return f"{round(num / 1_000_000)}M"
    if num >= 1_000:
        return f"{round(num / 1_000)}K"
    return f"{num:,}"


def format_cost(amount: float) -> str:
    """Format dollar amount: $0.52 under $1, $134 at/above."""
    if abs(amount) < 1.0:
        return f"${amount:,.2f}"
    return f"${amount:,.0f}"


def _fmt_int(x) -> str:
    return format_number(int(x))


# Column configuration: (field_name, header, width, format_func)
COLUMNS = [
    ('date', 'Date', 12, str),
    ('requests', 'Reqs', 8, _fmt_int),
    ('tokensIn', 'In (tok)', 10, _fmt_int),
    ('tokensOut', 'Out (tok)', 10, _fmt_int),
    ('cacheWrites', 'CW (tok)', 10, _fmt_int),
    ('cacheReads', 'CR (tok)', 10, _fmt_int),
    ('cacheSavings', 'Saved ($)', 10, format_cost),
    ('cost', 'Cost ($)', 10, format_cost),
]


def format_tokens(num: int, compact: bool = False) -> str:
    """Format token counts with MTok/KTok suffixes.

    compact=True rounds to whole numbers (for chart axes).
    Default shows one decimal when non-integer (for stat boxes and tables).
    """
    for threshold, divisor, suffix in [
        (1_000_000_000, 1_000_000_000, " GTok"),
        (1_000_000, 1_000_000, " MTok"),
        (1_000, 1_000, " KTok"),
    ]:
        if num >= threshold:
            value = num / divisor
            if compact or value == int(value):
                return f"{round(value)}{suffix}"
            return f"{value:.1f}{suffix}"
    return f"{num:,}"


def format_table_header() -> str:
    parts = []
    for field, header, width, _ in COLUMNS:
        if field == 'date':
            parts.append(f"{header:<{width}}")
        else:
            parts.append(f"{header:>{width}}")
    return " ".join(parts)


def format_table_row(date: str, data: Dict) -> str:
    parts = []
    for field, _, width, format_func in COLUMNS:
        if field == 'date':
            parts.append(f"{date:<{width}}")
        else:
            value = data.get(field, 0)
            formatted = format_func(value)
            parts.append(f"{formatted:>{width}}")
    return " ".join(parts)


def calculate_totals(daily_data: Dict[str, Dict]) -> Dict:
    totals = init_field_dict()
    for data in daily_data.values():
        for field in FIELD_NAMES:
            totals[field] += data.get(field, 0)
    return totals


def calculate_averages(totals: Dict, num_days: int) -> Dict:
    if num_days == 0:
        return init_field_dict()
    averages = init_field_dict()
    for field in FIELD_NAMES:
        averages[field] = totals[field] / num_days
    return averages
