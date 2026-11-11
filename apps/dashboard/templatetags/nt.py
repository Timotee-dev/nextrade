from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def pct(value, places=2):
    if value is None or value == "":
        return "-"
    return f"{float(value) * 100:.{int(places)}f}%"


@register.filter
def num(value, places=2):
    if value is None or value == "":
        return "-"
    try:
        return f"{float(value):,.{int(places)}f}"
    except (TypeError, ValueError):
        return value


@register.filter
def signed(value, places=2):
    if value is None:
        return "-"
    return f"{float(value):+,.{int(places)}f}"


@register.filter
def get(d, key):
    return d.get(key) if hasattr(d, "get") else None
