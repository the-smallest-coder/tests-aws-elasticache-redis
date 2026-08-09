"""Small numeric-to-string formatters shared by cards.py and report_compare.py.

Deliberately pandas-free: cards.py's header_pills() must stay importable
without pandas installed (see cards.py), so this module must not become a
transitive pandas dependency the way importing from helpers.py would.
"""


def format_gib(byte_value):
    """Bytes as a trimmed GiB string (e.g. '6.38'), or None if unparseable."""
    try:
        gib = float(byte_value) / (1024 ** 3)
    except (TypeError, ValueError):
        return None
    decimals = 3 if gib < 1 else 2
    return f"{gib:,.{decimals}f}".rstrip('0').rstrip('.')


def format_usd_hour(value):
    """Price as a trimmed, '$'-prefixed string (e.g. '$0.158'), or None if unparseable."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return f"${price:,.3f}".rstrip('0').rstrip('.')
