"""PDFium 렌더링 프로세스. 한 페이지를 제한된 크기의 PNG로 만든다."""
import sys
from pathlib import Path


def render(source, target, page_number, max_pixels):
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(str(source)) as document:
        if page_number < 1 or page_number > len(document):
            raise ValueError("page_out_of_range")
        page = document[page_number - 1]
        try:
            width, height = page.get_size()
            scale = 200 / 72
            if width <= 0 or height <= 0 or width * height * scale * scale > max_pixels:
                raise ValueError("render_pixel_limit_exceeded")
            bitmap = page.render(scale=scale)
            try:
                bitmap.to_pil().save(target, format="PNG")
            finally:
                bitmap.close()
        finally:
            page.close()


if __name__ == "__main__":
    render(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
