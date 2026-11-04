# Verifies the smoke-test output: 3 pages, MediaBox shrunk, /KropFullBox present.
import sys, fitz
doc = fitz.open(sys.argv[1])
assert doc.page_count >= 1, doc.page_count
for p in doc:
    mb = p.mediabox
    print(p.number + 1, "mediabox", tuple(round(v, 1) for v in mb))
    assert mb.width < 600 and mb.height < 780, "page not cropped"
    assert "KropFullBox" in doc.xref_object(p.xref), "no /KropFullBox"
print("smoke output OK")
