# Writes a small 3-page PDF with text and margins, for the CI smoke test.
import sys, fitz
doc = fitz.open()
for i in range(3):
    p = doc.new_page(width=612, height=792)
    p.insert_text((150, 200), "krop smoke test page %d" % (i + 1), fontsize=24)
    p.draw_rect(fitz.Rect(140, 170, 470, 620), color=(0, 0, 0), width=2)
    p.insert_text((300, 700), str(i + 1), fontsize=14)
doc.save(sys.argv[1])
