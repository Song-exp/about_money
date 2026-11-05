"""Deduplicate only equal native/OCR text at the same physical page region."""
import math

from app.services.document_layout import compact


def reconcile_page(content, number, native, readings):
    kept, decisions = [], []
    if not native or not readings:
        return readings, decisions
    try:
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(content) as document:
            page = document[number - 1]
            try:
                width, height = page.get_size()
                left, bottom, right, top = page.get_bbox()
                # OCR and native coordinates cannot be compared without the transform.
                if page.get_rotation() or left or bottom or abs(right - width) > 0.1 or abs(top - height) > 0.1:
                    return readings, [{"result": "preserved", "reason": "unsupported_page_transform"}]
                textpage = page.get_textpage()
                try:
                    for reading in readings:
                        box, dpi = reading.get("bbox"), reading.get("render_dpi")
                        if (not isinstance(box, (list, tuple)) or len(box) != 4 or not isinstance(dpi, (int, float))
                                or not math.isfinite(dpi) or dpi <= 0
                                or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)
                                or reading.get("page") != number):
                            kept.append(reading)
                            continue
                        x0, y0, x1, y1 = [v * 72 / dpi for v in box]
                        if not 0 <= x0 < x1 <= width + 1 or not 0 <= y0 < y1 <= height + 1:
                            kept.append(reading)
                            continue
                        native_text = textpage.get_text_bounded(max(0, x0 - 1), max(0, height - y1 - 1),
                                                               min(width, x1 + 1), min(height, height - y0 + 1))
                        value = compact(reading["text"])
                        # No edit distance or number/punctuation removal: disagreement stays visible.
                        if value and value == compact(native_text) and value in compact(native["text"]):
                            decisions.append({"result": "duplicate", "reason": "same_region_same_text",
                                              "native_location": native["location"],
                                              "native_region_text": native_text, "reading": dict(reading)})
                        else:
                            kept.append(reading)
                            if compact(native_text):
                                decisions.append({"result": "preserved", "reason": "native_ocr_region_disagreement",
                                                  "native_location": native["location"],
                                                  "native_region_text": native_text, "reading": dict(reading)})
                finally:
                    textpage.close()
            finally:
                page.close()
    except Exception as exc:
        # Optional reconciliation failure must never discard a reading.
        return readings, [{"result": "preserved", "reason": "reconciliation_failed", "error": type(exc).__name__}]
    return kept, decisions
