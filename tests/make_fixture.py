"""Regenerate tests/data/scanlike.pdf: a synthetic 4-page "copier scan".

Each page is a single raster image (like a scan) with a solid black platen
shadow along the left and top edges, a white gap, a grey text block, a small
page number near the bottom, and a couple of dust dots in the margin.
No real content.  Run: python tests/make_fixture.py
"""
import os, pymupdf

W, H = 612, 792            # points
DPI = 100
# content geometry in points (left, top, right, bottom)
BLOCK = (110, 120, 500, 640)
NUM_Y = 700                # baseline of page number


def page_image(n):
    src = pymupdf.open()
    p = src.new_page(width=W, height=H)
    p.draw_rect(pymupdf.Rect(0, 0, 18, H), color=None, fill=(0, 0, 0))       # left shadow
    p.draw_rect(pymupdf.Rect(0, 0, W, 14), color=None, fill=(0, 0, 0))       # top shadow
    x0, y0, x1, y1 = BLOCK
    y = y0
    while y < y1:
        p.draw_rect(pymupdf.Rect(x0, y, x1 - (0 if (y // 24) % 3 else 60), y + 10), color=None, fill=(0.1, 0.1, 0.1))
        y += 24
    p.insert_text((W / 2 - 6, NUM_Y), str(n), fontsize=14)
    p.draw_circle((60, 400), 0.8, color=None, fill=(0, 0, 0))               # dust
    p.draw_circle((560, 740), 0.8, color=None, fill=(0, 0, 0))
    pix = p.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
    return pix.tobytes("png")


def main(path):
    doc = pymupdf.open()
    for n in range(1, 5):
        pg = doc.new_page(width=W, height=H)
        pg.insert_image(pg.rect, stream=page_image(n))
    doc.save(path, deflate=True, garbage=4)


if __name__ == "__main__":
    main(os.path.join(os.path.dirname(__file__), "data", "scanlike.pdf"))
