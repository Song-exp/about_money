"""공백·제어 문자 차이만 허용하며 인용을 원문의 위치로 되돌린다."""
from app.services.document_layout import compact


def find_quote(text, quote, start=0):
    if not isinstance(quote, str) or not compact(quote):
        return None
    index = text.find(quote, start)
    if index >= 0:
        return index, index + len(quote), "exact"
    positions = [i for i, c in enumerate(text) if not c.isspace() and ord(c) >= 32]
    normalized = "".join(text[i] for i in positions)
    needle = compact(quote)
    offset = next((n for n, p in enumerate(positions) if p >= start), len(positions))
    index = normalized.find(needle, offset)
    if index < 0:
        return None
    return positions[index], positions[index + len(needle) - 1] + 1, "whitespace"


def canonical_quote(text, quote):
    match = find_quote(text, quote)
    return text[match[0]:match[1]] if match else quote
