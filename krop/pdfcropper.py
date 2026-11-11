# -*- coding: iso-8859-1 -*-

"""
Cropping functionality for krop.

Copyright (C) 2010-2025 Armin Straub, http://arminstraub.com
"""

"""
This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 3 of the License, or
(at your option) any later version.
"""

import copy
import sys

from krop.config import PYQT6


class PdfEncryptedError(Exception):
    pass


class AbstractPdfFile:
    """Abstract class for loading a PDF document used in a corresponding
    PdfCropper class"""
    def loadFromStream(self, stream):
        pass
    def loadFromFile(self, filename):
        self.loadFromStream(open(filename, "rb"))

class PyPdfFile(AbstractPdfFile):
    """Implementation of PdfFile using the new pypdf"""
    def __init__(self):
        self.reader = None
    def loadFromStream(self, stream):
        self.reader = self.PdfReader(stream)
        if self.reader.is_encrypted:
            try:
                if not self.reader.decrypt(''):
                    raise PdfEncryptedError
            except:
                raise PdfEncryptedError
    def getPage(self, nr):
        return self.reader.pages[nr]

class PyPdfOldFile(PyPdfFile):
    """Implementation of PdfFile using PyPDF2 (<2) which uses camelCase rather
    than snake_case"""
    def loadFromStream(self, stream):
        self.reader = self.PdfReader(stream, strict=False)
        if self.reader.isEncrypted:
            try:
                if not self.reader.decrypt(''):
                    raise PdfEncryptedError
            except:
                raise PdfEncryptedError
    def getPage(self, nr):
        return self.reader.getPage(nr)

class PyMuPdfFile(AbstractPdfFile):
    """Implementation of PdfFile using PyMuPDF"""
    def __init__(self):
        self.reader = None
    def loadFromStream(self, stream):
        self.reader = self.pymupdf.open(stream)
        if self.reader.is_encrypted:
            raise PdfEncryptedError
    def getPage(self, nr):
        return self.reader[nr]

class PikePdfFile(AbstractPdfFile):
    """Implementation of PdfFile using pikepdf"""
    def __init__(self):
        self.reader = None
    def loadFromStream(self, stream):
        self.reader = self.Pdf.open(stream)
        if self.reader.is_encrypted:
            raise PdfEncryptedError
    def getPage(self, nr):
        return self.reader.pages[nr]


class AbstractPdfCropper:
    """Abstract class for writing a PDF documents composed of cropped pages
    from PdfFile instances"""
    def writeToStream(self, stream):
        pass
    def writeToFile(self, filename):
        stream = open(filename, "wb")
        self.writeToStream(stream)
        stream.close()
    def addPageCropped(self, pdffile, pagenumber, croplist, rotate=0):
        pass
    def copyDocumentRoot(self, pdffile):
        pass

class SemiAbstractPdfCropper(AbstractPdfCropper):
    """An extension of AbstractPdfCropper that implements the basic logic
    breaking it down to certain functions on pages"""
    def addPageCropped(self, pdffile, pagenumber, croplist, alwaysinclude, rotate=0):
        page = pdffile.getPage(pagenumber)
        if not croplist and alwaysinclude:
            self.doAddPage(page, rotate)
        else:
            box = self.pageGetCropBox(page)
            for crop in croplist:
                new_page = copy.copy(page)
                new_box = computeCropBoxCoords(box, crop)
                self.pageRememberFullBox(new_page)
                self.pageSetCropBox(new_page, new_box)
                self.doAddPage(new_page, rotate)
    def doAddPage(self, page, rotate):
        pass
    def pageGetCropBox(self, page):
        pass
    def pageSetCropBox(self, page, box):
        pass
    def pageRememberFullBox(self, page):
        """Store the uncropped page (MediaBox) in the private /KropFullBox key
        (unless already there) so that krop can reopen the crop for editing."""
        pass

class PyPdfCropper(SemiAbstractPdfCropper):
    """Implementation of PdfCropper using pypdf"""
    def __init__(self):
        self.output = self.PdfWriter()
    def writeToStream(self, stream):
        # For certain large pdf files, PdfWriter.write() causes the error:
        #   maximum recursion depth exceeded while calling a Python object
        # This issue is present, for instance, in PyPDF2 1.23.
        # We therefore temporarily increase the recursion limit.
        old_reclimit = sys.getrecursionlimit()
        sys.setrecursionlimit(10000)
        self.output.write(stream)
        sys.setrecursionlimit(old_reclimit)
    def doAddPage(self, page, rotate):
        if rotate != 0:
            page.rotate(rotate)
        self.output.add_page(page)
    def pageGetCropBox(self, page):
        x0, y0 = page.cropbox.lower_left
        x1, y1 = page.cropbox.upper_right
        return x0, y0, x1, y1
    def pageSetCropBox(self, page, box):
        x0, y0, x1, y1 = box
        for page_box in (page.artbox, page.bleedbox, page.cropbox, page.mediabox, page.trimbox):
            page_box.lower_left = (x0, y0)
            page_box.upper_right = (x1, y1)
    def pageRememberFullBox(self, page):
        from pypdf.generic import NameObject, ArrayObject, FloatObject
        if '/KropFullBox' not in page:
            (a, b), (c, d) = page.mediabox.lower_left, page.mediabox.upper_right
            page[NameObject('/KropFullBox')] = ArrayObject(
                [FloatObject(float(v)) for v in (a, b, c, d)])
    def copyDocumentRoot(self, pdffile):
        # Copy the named destinations for links.
        # TODO: this worked for links in PyPDF2 but doesn't seem to work for pypdf
        for dest in pdffile.reader.named_destinations.values():
            self.output.add_named_destination_object(dest)

class PyPdfOldCropper(PyPdfCropper):
    """Implementation of PdfCropper using PyPDF2 (<2)"""
    def doAddPage(self, page, rotate):
        if rotate != 0:
            page.rotateClockwise(rotate)
        self.output.addPage(page)
    def pageGetCropBox(self, page):
        x0, y0 = page.cropBox.lowerLeft
        x1, y1 = page.cropBox.upperRight
        return x0, y0, x1, y1
    def pageSetCropBox(self, page, box):
        x0, y0, x1, y1 = box
        for page_box in (page.artBox, page.bleedBox, page.cropBox, page.mediaBox, page.trimBox):
            page_box.lowerLeft = (x0, y0)
            page_box.upperRight = (x1, y1)
    def pageRememberFullBox(self, page):
        from PyPDF2.generic import NameObject, ArrayObject, FloatObject
        if '/KropFullBox' not in page:
            (a, b), (c, d) = page.mediaBox.lowerLeft, page.mediaBox.upperRight
            page[NameObject('/KropFullBox')] = ArrayObject(
                [FloatObject(float(v)) for v in (a, b, c, d)])
    def copyDocumentRoot(self, pdffile):
        # Copy the named destinations for links.
        for dest in pdffile.reader.namedDestinations.values():
            self.output.addNamedDestinationObject(dest)


class PyMuPdfCropper(SemiAbstractPdfCropper):
    """Implementation of PdfCropper using PyMuPDF"""
    def __init__(self):
        self.output = self.pymupdf.open()
        # Stashed in copyDocumentRoot (called before pages are added) and
        # applied in writeTo* after pages exist. insert_pdf copies page content
        # / links / annots but never the document outline or page labels.
        self._src_toc = None
        self._src_page_labels = None
        # source 0-based page -> first output 0-based page
        self._src_page_to_out = {}
    def writeToStream(self, stream):
        self._apply_document_root()
        stream.write(self.output.tobytes())
    def writeToFile(self, filename):
        # Don't hand PyMuPDF an open file object: it saves via the object's
        # filename and on Windows can't replace a file we hold open.
        self._apply_document_root()
        self.output.save(filename)
    def addPageCropped(self, pdffile, pagenumber, croplist, alwaysinclude, rotate=0):
        def addPage():
            # https://pymupdf.readthedocs.io/en/latest/the-basics.html
            r = pdffile.reader[pagenumber].rotation + rotate
            # final=False keeps PyMuPDF's graft map for this source between
            # calls, so objects shared by several source pages (e.g. one image
            # XObject drawn on many pages) are copied once and stay shared,
            # instead of being duplicated per page.
            self.output.insert_pdf(pdffile.reader, from_page=pagenumber, to_page=pagenumber, rotate=r, final=False)
            # First output page for this source page wins (multi-crop → TOC
            # points at the first crop).
            if pagenumber not in self._src_page_to_out:
                self._src_page_to_out[pagenumber] = self.output.page_count - 1
        if not croplist and alwaysinclude:
            addPage()
            self._log_boxes("included", self.output[-1], pagenumber)
        else:
            for crop in croplist:
                addPage()
                new_page = self.output[-1]
                self._log_boxes("before", new_page, pagenumber)
                # Fractions are of the visible page. page.cropbox is not clipped
                # to the MediaBox, and set_cropbox rejects anything outside it.
                box = visible_mu_box(new_page)
                new_box = clamp_mu_box(new_page, computeCropBoxCoords(box, crop, pdf_coords=False))
                try:
                    self.pageSetCropBox(new_page, new_box)
                except ValueError as err:
                    if "not in MediaBox" not in str(err):
                        raise
                    _page_log("set_cropbox refused %s; writing the clamped box" % err)
                    self._write_clamped_box(new_page, new_box)
                self.pageShrinkMediaBox(new_page)
                self._log_boxes("after", new_page, pagenumber)
                # if rotate != 0:
                #     self.pageRotateClockwise(new_page, rotate)
    def pageGetCropBox(self, page):
        return page.cropbox
    def pageSetCropBox(self, page, box):
        page.set_cropbox(box)
        try:
            page.set_artbox(page.cropbox)
            page.set_bleedbox(page.cropbox)
            # careful: mediabox in MuPDF is an exception and uses PDF coordinates
            # page.set_mediabox(page.cropbox)
            page.set_trimbox(page.cropbox)
        except:
            # these functions did not exist prior to v1.19.4
            pass
    def pageShrinkMediaBox(self, page):
        """Local patch: like krop 0.6 (and the other backends), also set the
        MediaBox to the crop so that tools ignoring the CropBox see the cropped
        page; remember the full page in /KropFullBox for crop recovery.
        We copy the CropBox as written in the PDF (PDF coordinates), since
        MuPDF's page.cropbox is in MuPDF (top-left origin) coordinates."""
        doc = page.parent
        xref = page.xref
        if doc.xref_get_key(xref, "KropFullBox")[0] == "null":
            kind, mb = doc.xref_get_key(xref, "MediaBox")
            if kind != "array":     # inherited from the page tree
                m = page.mediabox   # PDF coordinates
                mb = "[%g %g %g %g]" % (m.x0, m.y0, m.x1, m.y1)
            doc.xref_set_key(xref, "KropFullBox", mb)
        kind, cb = doc.xref_get_key(xref, "CropBox")
        if kind == "array":
            for key in ("MediaBox", "CropBox", "TrimBox", "ArtBox", "BleedBox"):
                doc.xref_set_key(xref, key, cb)
    def _write_clamped_box(self, page, mu_box):
        """Write a CropBox PyMuPDF's set_cropbox would not accept."""
        mb = _mediabox_floats(page)
        x0, y0, x1, y1 = _pdf_from_mu(mb, mu_box)
        page.parent.xref_set_key(page.xref, "CropBox", "[%g %g %g %g]" % (x0, y0, x1, y1))
    def _log_boxes(self, phase, page, src_index):
        doc = page.parent
        xref = page.xref
        _page_log("src %d %s MediaBox %s CropBox %s rot %s" % (
            src_index + 1, phase,
            doc.xref_get_key(xref, "MediaBox")[1],
            doc.xref_get_key(xref, "CropBox")[1],
            page.rotation))
    def copyDocumentRoot(self, pdffile):
        # Called before pages are added; stash catalog extras for write time.
        try:
            self._src_toc = pdffile.reader.get_toc(simple=True)
        except Exception:
            self._src_toc = None
        try:
            labels = pdffile.reader.get_page_labels()
            self._src_page_labels = labels or None
        except Exception:
            self._src_page_labels = None

    def _apply_document_root(self):
        """Copy outline (and page labels when remappable) onto the output."""
        if self._src_toc:
            # source 1-based page number -> output 1-based
            src1_to_out1 = {
                src0 + 1: out0 + 1
                for src0, out0 in self._src_page_to_out.items()
            }
            new_toc = []
            for entry in self._src_toc:
                # [level, title, page] ; page is 1-based in the source
                level, title, page = entry[0], entry[1], entry[2]
                if not isinstance(page, int) or page not in src1_to_out1:
                    continue
                level = int(level)
                if new_toc:
                    # set_toc requires levels to rise by at most one
                    level = min(level, new_toc[-1][0] + 1)
                level = max(level, 1)
                new_toc.append([level, title, src1_to_out1[page]])
            if new_toc:
                self.output.set_toc(new_toc)
        if self._src_page_labels and self._src_page_to_out:
            # Remap startpage for labels whose start page was included; drop
            # the rest. Only rewrite when at least one rule survives.
            new_labels = []
            for rule in self._src_page_labels:
                start = int(rule.get("startpage", 0))
                if start not in self._src_page_to_out:
                    continue
                new_rule = dict(rule)
                new_rule["startpage"] = self._src_page_to_out[start]
                new_labels.append(new_rule)
            if new_labels:
                # rules must be sorted by startpage
                new_labels.sort(key=lambda r: r["startpage"])
                try:
                    self.output.set_page_labels(new_labels)
                except Exception:
                    pass


class PikePdfCropper(SemiAbstractPdfCropper):
    """Implementation of PdfCropper using pikepdf"""
    def __init__(self):
        self.output = self.Pdf.new()
    def writeToStream(self, stream):
        self.output.save(stream)
    def doAddPage(self, page, rotate):
        if rotate != 0:
            page.rotate(rotate, relative=True)
        self.output.pages.append(page)
    def pageGetCropBox(self, page):
        try:
            # only page.MediaBox exists in pikepdf version 1.10.3 as currently in Ubuntu 20.04
            return page.cropbox
        except AttributeError:
            raise RuntimeError("Please install a more recent version of pikepdf.")
    def pageRememberFullBox(self, page):
        from pikepdf import Name, Array
        if Name.KropFullBox not in page.obj:
            page.obj[Name.KropFullBox] = Array([float(v) for v in page.mediabox])
    def pageSetCropBox(self, page, box):
        page.cropbox = box
        page.mediabox = box
        page.trimbox = box
    def copyDocumentRoot(self, pdffile):
        pass


def computeCropBoxCoords(box, crop, pdf_coords=True):
    x0, y0, x1, y1 = box
    x0, y0, x1, y1 = float(x0), float(y0), float(x1), float(y1)
    # In PDF coordinates (0,0) is the bottom-left point; otherwise, as in
    # MuPDF or Qt, this would be the top-left point.
    if not pdf_coords:
        crop = (crop[0], crop[3], crop[2], crop[1])
    x0, x1 = x0+crop[0]*(x1-x0), x1-crop[2]*(x1-x0)
    y0, y1 = y0+crop[3]*(y1-y0), y1-crop[1]*(y1-y0)
    return x0, y0, x1, y1


def _page_log(msg):
    try:
        from krop.activitylog import log
        log("PAGE", msg)
    except Exception:
        pass


def _mediabox_floats(page):
    mb = page.mediabox
    return (float(mb.x0), float(mb.y0), float(mb.x1), float(mb.y1))


def _mu_from_pdf(mb, rect):
    """PDF user space -> the y-down space Page.set_cropbox expects."""
    x0, y0, x1, y1 = rect
    return (x0, mb[3] - y1, x1, mb[3] - y0)


def _pdf_from_mu(mb, rect):
    return _mu_from_pdf(mb, rect)


def visible_mu_box(page):
    """The on-screen page, in set_cropbox coordinates.

    page.cropbox is the file's CropBox with y flipped about MediaBox.y1, and
    it is not clipped to the MediaBox. PyMuPDF draws only the intersection,
    so a CropBox that sticks out past the MediaBox is larger than the page the
    user sees. Crop fractions are fractions of that visible page.
    """
    cb = tuple(float(v) for v in (page.cropbox.x0, page.cropbox.y0, page.cropbox.x1, page.cropbox.y1))
    mb = _mediabox_floats(page)
    mu_mb = _mu_from_pdf(mb, mb)
    def inside(inner, outer):
        return (inner[0] >= outer[0] - 1e-6 and inner[1] >= outer[1] - 1e-6
                and inner[2] <= outer[2] + 1e-6 and inner[3] <= outer[3] + 1e-6
                and inner[2] > inner[0] + 1e-6 and inner[3] > inner[1] + 1e-6)
    if inside(cb, mu_mb):
        return cb
    vis = (max(cb[0], mu_mb[0]), max(cb[1], mu_mb[1]),
           min(cb[2], mu_mb[2]), min(cb[3], mu_mb[3]))
    if vis[2] - vis[0] < 1e-4 or vis[3] - vis[1] < 1e-4:
        _page_log("CropBox does not meet MediaBox; cropping the MediaBox")
        return mu_mb
    _page_log("CropBox extends outside MediaBox; cropping their intersection")
    return vis


def clamp_mu_box(page, rect):
    """Pull a set_cropbox rectangle back inside the MediaBox.

    set_cropbox raises ValueError('CropBox not in MediaBox') unless the
    rectangle is strictly inside after it flips y. A selection that crosses
    the page edge, or a CropBox that already sticks out, produces that error.
    """
    mb = _mediabox_floats(page)
    x0, y0, x1, y1 = (float(v) for v in rect)
    if x1 - x0 < 1e-4 or y1 - y0 < 1e-4:
        _page_log("selection has no area; keeping the visible page")
        rect = visible_mu_box(page)
        x0, y0, x1, y1 = rect
    px0, py0, px1, py1 = _pdf_from_mu(mb, (x0, y0, x1, y1))
    if px1 < px0:
        px0, px1 = px1, px0
    if py1 < py0:
        py0, py1 = py1, py0
    clamped = (min(max(px0, mb[0]), mb[2]), min(max(py0, mb[1]), mb[3]),
               min(max(px1, mb[0]), mb[2]), min(max(py1, mb[1]), mb[3]))
    eps = 0.01
    cx0, cy0, cx1, cy1 = clamped
    if cx1 - cx0 < eps:
        cx1 = min(mb[2], cx0 + eps)
        cx0 = max(mb[0], cx1 - eps)
    if cy1 - cy0 < eps:
        cy1 = min(mb[3], cy0 + eps)
        cy0 = max(mb[1], cy1 - eps)
    out = _mu_from_pdf(mb, (cx0, cy0, cx1, cy1))
    if tuple(round(v, 4) for v in out) != tuple(round(v, 4) for v in (x0, y0, x1, y1)):
        _page_log("clamped crop into MediaBox")
    return out

def optimizePdfGhostscript(oldfilename, newfilename):
    import subprocess
    subprocess.check_call(('gs', '-sDEVICE=pdfwrite', '-sOutputFile=' + newfilename,
        '-dNOPAUSE', '-dBATCH', oldfilename))


# In the following, we determine which cropping library to use.
# See lib_crop_options below for a list of the supported libraries.

def import_pymupdf():
    import fitz
    # note: since 1.24.3, one can use 'import pymupdf' instead but fitz is
    # promised to always be supported as well
    PyMuPdfFile.pymupdf = fitz
    PyMuPdfCropper.pymupdf = fitz
    return PyMuPdfFile, PyMuPdfCropper

def import_pikepdf():
    from pikepdf import Pdf
    PikePdfFile.Pdf = Pdf
    PikePdfCropper.Pdf = Pdf
    return PikePdfFile, PikePdfCropper

def import_pypdf():
    from pypdf import PdfReader, PdfWriter
    PyPdfFile.PdfReader = PdfReader
    PyPdfCropper.PdfWriter = PdfWriter
    return PyPdfFile, PyPdfCropper

def import_pypdf2():
    import PyPDF2
    # PyPDF2 (<2) uses camelCase while newer versions (just like pypdf) use snake_case
    if PyPDF2.__version__.startswith("1."):
        from PyPDF2 import PdfFileReader as PdfReader, PdfFileWriter as PdfWriter
        PdfFile = PyPdfOldFile
        PdfCropper = PyPdfOldCropper
    else:
        from PyPDF2 import PdfReader, PdfWriter
        PdfFile = PyPdfFile
        PdfCropper = PyPdfCropper
    PdfFile.PdfReader = PdfReader
    PdfCropper.PdfWriter = PdfWriter
    return PdfFile, PdfCropper

# for each cropping library: [name, import function, flag to request it]
lib_crop_options = [
        ['PyMuPDF', import_pymupdf, '--use-pymupdf'],
        ['pikepdf', import_pikepdf, '--use-pikepdf'],
        ['pypdf', import_pypdf, '--use-pypdf'],
        ['PyPDF2', import_pypdf2, '--use-pypdf2'],
        ]
# lib_crop will be set to the name of the cropping library in use
lib_crop = None

# We go through all options for libraries twice:
# during the first round, we load a library if it is specifically requested,
# and during the second round, we load the first available library.
for load_only_if_requested in [True, False]:
    for lib, import_func, flag in lib_crop_options:
        if not lib_crop and (not load_only_if_requested or (flag and flag in sys.argv)):
            try:
                PdfFile, PdfCropper = import_func()
                lib_crop = lib
                print(f"Using {lib} for cropping.", file=sys.stderr)
            except ImportError:
                if load_only_if_requested:
                    print(f"{lib} was requested but failed to load.", file=sys.stderr)

# complain if no library could be imported
if not lib_crop:
    _msg = "Please install one of the supported cropping libraries first (PyMuPDF, pypdf, or pikepdf)."
    if PYQT6:
        _msg += "\n\tFor instance, on recent versions of Ubuntu, the following should do the trick:"\
            "\n\tsudo apt-get install python3-pymupdf"
    else:
        _msg += "\n\tFor instance, on versions of Ubuntu such as 22.04, one of the following should do the trick:"\
            "\n\tsudo apt install python3-fitz"\
            "\n\tsudo apt-get install python3-pypdf2"
    raise RuntimeError(_msg)

