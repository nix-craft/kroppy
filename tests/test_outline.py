"""Document outline (bookmarks/TOC) must survive PyMuPDF cropping."""
import os

import pymupdf

from krop.pdfcropper import PdfCropper, PdfFile, lib_crop


def _pdf_with_outline(path):
    doc = pymupdf.open()
    for i in range(2):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), f"Page {i + 1}")
        # a bit of ink so --trim has something, and for mediabox shrink
        page.draw_rect(pymupdf.Rect(100, 100, 500, 700), color=(0, 0, 0), width=1)
    doc.set_toc([[1, "Section One", 1], [1, "Section Two", 2]])
    doc.save(path)
    doc.close()


def test_outline_preserved_after_crop(tmp_path):
    assert lib_crop == "PyMuPDF"
    src, out = str(tmp_path / "in.pdf"), str(tmp_path / "out.pdf")
    _pdf_with_outline(src)
    assert pymupdf.open(src).get_toc() == [
        [1, "Section One", 1],
        [1, "Section Two", 2],
    ]

    pdf = PdfFile()
    pdf.loadFromFile(src)
    cropper = PdfCropper()
    cropper.copyDocumentRoot(pdf)
    # modest crop on each page (same path as the GUI: insert_pdf + MediaBox shrink)
    for nr in range(2):
        cropper.addPageCropped(pdf, nr, [(0.05, 0.05, 0.05, 0.05)], False, 0)
    cropper.writeToFile(out)

    doc = pymupdf.open(out)
    assert doc.page_count == 2
    assert doc.get_toc() == [
        [1, "Section One", 1],
        [1, "Section Two", 2],
    ]
    # MediaBox still shrunk (existing krop-next behaviour)
    assert all(p.mediabox.width < 612 * 0.95 for p in doc)
    # RecoverCrops marker still present
    assert all(
        doc.xref_get_key(p.xref, "KropFullBox")[0] == "array" for p in doc
    )
    assert os.path.getsize(out) > 0


def test_outline_remaps_when_subset_of_pages(tmp_path):
    """Only page 2 is kept → 'Section Two' must point at output page 1."""
    assert lib_crop == "PyMuPDF"
    src, out = str(tmp_path / "in.pdf"), str(tmp_path / "out.pdf")
    _pdf_with_outline(src)

    pdf = PdfFile()
    pdf.loadFromFile(src)
    cropper = PdfCropper()
    cropper.copyDocumentRoot(pdf)
    cropper.addPageCropped(pdf, 1, [(0.05, 0.05, 0.05, 0.05)], False, 0)
    cropper.writeToFile(out)

    toc = pymupdf.open(out).get_toc()
    assert toc == [[1, "Section Two", 1]]
