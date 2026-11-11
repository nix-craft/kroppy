"""Read-only color and bit-depth recommendations."""
import json, os, subprocess, sys, time
import pymupdf
import pytest

from krop.analyze import analyze_file, classify_rgb, format_report


def _save(doc, path):
    doc.save(path)
    doc.close()


def test_near_black_is_not_color():
    assert classify_rgb(0.08, 0.08, 0.09) == "black"
    assert classify_rgb(1, 0, 0) == "color"
    assert classify_rgb(0.5, 0.5, 0.5) == "gray"


def test_bw_page(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Hello", fontsize=18, color=(0, 0, 0))
    page.draw_rect(pymupdf.Rect(72, 140, 200, 180), color=(0, 0, 0), width=1)
    path = str(tmp_path / "bw.pdf")
    _save(doc, path)
    data = analyze_file(path)
    assert data["summary"]["label"] == "BW"
    assert data["summary"]["target"] == "bw-1"
    assert data["pages"][0]["blocker_count"] == 0


def test_near_black_text_is_bw(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 120), "Body", fontsize=14, color=(0.08, 0.08, 0.07))
    path = str(tmp_path / "near.pdf")
    _save(doc, path)
    data = analyze_file(path)
    assert data["pages"][0]["label"] == "BW"
    assert data["summary"]["target"] == "bw-1"


def test_red_text_is_an_accent(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Hello", fontsize=14, color=(0, 0, 0))
    page.insert_text((72, 140), "Refrain", fontsize=14, color=(0.9, 0.05, 0.05))
    path = str(tmp_path / "red.pdf")
    _save(doc, path)
    data = analyze_file(path)
    page = data["pages"][0]
    assert page["label"] == "MOSTLY-BW-WITH-COLOR-ACCENTS"
    assert page["target"] == "color"
    blob = " ".join(b["text"] for b in page["blockers"])
    assert 'red text span "Refrain"' in blob
    assert "p1" in blob


def test_gray_four_levels(tmp_path):
    import numpy as np
    doc = pymupdf.open()
    page = doc.new_page()
    arr = np.zeros((40, 80), np.uint8)
    arr[:, :20] = 0
    arr[:, 20:40] = 85
    arr[:, 40:60] = 170
    arr[:, 60:] = 255
    pix = pymupdf.Pixmap(pymupdf.csGRAY, 80, 40, arr.tobytes(), False)
    page.insert_image(pymupdf.Rect(72, 72, 400, 300), pixmap=pix)
    path = str(tmp_path / "gray.pdf")
    _save(doc, path)
    data = analyze_file(path)
    page = data["pages"][0]
    assert page["label"] == "GRAY"
    assert page["target"] == "gray-2"
    assert page["images"][0]["levels"] == 4
    assert page["images"][0]["suggested_bpp"] == 2
    assert page["images"][0]["photo"] is False


def test_color_photo(tmp_path):
    import numpy as np
    doc = pymupdf.open()
    page = doc.new_page()
    arr = np.zeros((60, 80, 3), np.uint8)
    arr[:, :40] = (220, 30, 40)
    arr[:, 40:] = (20, 40, 200)
    pix = pymupdf.Pixmap(pymupdf.csRGB, 80, 60, arr.tobytes(), False)
    page.insert_image(page.rect, pixmap=pix)
    path = str(tmp_path / "photo.pdf")
    _save(doc, path)
    data = analyze_file(path)
    assert data["summary"]["label"] == "COLOR"
    assert data["summary"]["target"] == "color"
    assert any("color image xref" in b["text"] for b in data["summary"]["blockers"])


def test_shading_is_flagged_not_bw(tmp_path):
    import pikepdf
    path = str(tmp_path / "shade.pdf")
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page()
    fn = pikepdf.Dictionary(FunctionType=2, Domain=[0, 1], C0=[0, 0, 0], C1=[1, 0, 0], N=1)
    sh = pikepdf.Dictionary(ShadingType=2, ColorSpace=pikepdf.Name.DeviceRGB,
                            Coords=[0, 0, 100, 0], Function=fn)
    page.Resources = pikepdf.Dictionary(Shading=pikepdf.Dictionary(Sh1=sh))
    page.Contents = pikepdf.Stream(pdf, b"/Sh1 sh\n")
    pdf.save(path)
    data = analyze_file(path)
    assert data["pages"][0]["label"] == "COLOR"
    assert any(b["kind"] == "shading" for b in data["pages"][0]["blockers"])
    assert any("shading pattern" in b["text"] for b in data["pages"][0]["blockers"])


def test_transparency_is_flagged(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.draw_rect(pymupdf.Rect(72, 72, 200, 200), color=(0, 0, 0),
                   fill=(0, 0, 0), fill_opacity=0.4, width=1)
    path = str(tmp_path / "trans.pdf")
    _save(doc, path)
    data = analyze_file(path)
    page = data["pages"][0]
    assert any(b["kind"] == "transparency" for b in page["blockers"])
    assert page["label"] in ("BW", "GRAY")
    blob = " ".join([b["text"] for b in page["blockers"]] + page["notes"])
    assert "by render" in blob or "render is neutral" in blob
    assert "transparency" in blob
    assert page["target"] != "color"


def test_spot_color_is_flagged(tmp_path):
    import pikepdf
    path = str(tmp_path / "spot.pdf")
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page()
    fn = pikepdf.Dictionary(FunctionType=2, Domain=[0, 1], C0=[0, 0, 0, 0], C1=[0, 0.2, 0.8, 0], N=1)
    sep = pikepdf.Array([pikepdf.Name.Separation, pikepdf.Name("/Gold"),
                         pikepdf.Name.DeviceCMYK, fn])
    page.Resources = pikepdf.Dictionary(ColorSpace=pikepdf.Dictionary(Sp0=sep))
    page.Contents = pikepdf.Stream(pdf, b"/Sp0 cs 0.5 scn\n")
    pdf.save(path)
    data = analyze_file(path)
    assert any("spot color" in b["text"] for b in data["pages"][0]["blockers"])
    assert data["pages"][0]["target"] == "color"


def test_ccitt_copier_page(tmp_path):
    import pikepdf
    from PIL import Image
    path = str(tmp_path / "ccitt.pdf")
    im = Image.new("1", (80, 40), 1)
    tif = str(tmp_path / "c.tif")
    im.save(tif, "TIFF", compression="group4")
    t = Image.open(tif)
    offs, cnts = t.tag_v2[273], t.tag_v2[279]
    offs = offs if isinstance(offs, tuple) else (offs,)
    cnts = cnts if isinstance(cnts, tuple) else (cnts,)
    raw = open(tif, "rb").read()[offs[0]:offs[0] + cnts[0]]
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(80, 40))
    st = pikepdf.Stream(pdf, raw)
    st.Type = pikepdf.Name.XObject
    st.Subtype = pikepdf.Name.Image
    st.Width, st.Height = 80, 40
    st.ColorSpace = pikepdf.Name.DeviceGray
    st.BitsPerComponent = 1
    st.Filter = pikepdf.Name.CCITTFaxDecode
    st.DecodeParms = pikepdf.Dictionary(K=-1, Columns=80, Rows=40)
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=st))
    page.Contents = pikepdf.Stream(pdf, b"q 80 0 0 40 0 0 cm /Im0 Do Q\n")
    pdf.save(path)
    data = analyze_file(path)
    im0 = data["pages"][0]["images"][0]
    assert im0["copier_ccitt"] is True
    assert im0["suggested_bpp"] == 1
    assert data["summary"]["label"] == "BW"
    assert "CCITT" in data["pages"][0]["image_summary"]


def test_invisible_text_is_ignored(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Hello", fontsize=14, color=(0, 0, 0))
    page.insert_text((72, 140), "OCR layer", fontsize=14, color=(1, 0, 0), render_mode=3)
    path = str(tmp_path / "ocr.pdf")
    _save(doc, path)
    data = analyze_file(path)
    assert data["pages"][0]["label"] == "BW"
    notes = [t for t in data["pages"][0]["text"] if t.get("render") == 3 or t.get("cls") == "ignored"]
    assert notes
    assert all(t["cls"] == "ignored" for t in notes)


def test_form_xobject_color_is_seen(tmp_path):
    import pikepdf
    path = str(tmp_path / "form.pdf")
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page()
    form = pikepdf.Stream(pdf, b"1 0 0 RG 0 0 m 10 10 l S\n")
    form.Type = pikepdf.Name.XObject
    form.Subtype = pikepdf.Name.Form
    form.BBox = pikepdf.Array([0, 0, 10, 10])
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Fm0=form))
    page.Contents = pikepdf.Stream(pdf, b"/Fm0 Do\n")
    pdf.save(path)
    data = analyze_file(path)
    assert data["pages"][0]["label"] == "COLOR"
    assert any("color drawing" in b["text"] for b in data["pages"][0]["blockers"])


def test_black_transparency_group_is_bw_by_render(tmp_path):
    import pikepdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Hello", fontsize=18, color=(0, 0, 0))
    path = str(tmp_path / "group.pdf")
    _save(doc, path)
    pdf = pikepdf.open(path, allow_overwriting_input=True)
    pdf.pages[0].obj["/Group"] = pikepdf.Dictionary(
        Type=pikepdf.Name.Group, S=pikepdf.Name.Transparency, CS=pikepdf.Name.DeviceGray)
    pdf.save(path)
    data = analyze_file(path)
    page = data["pages"][0]
    assert page["label"] == "BW"
    assert page["target"] == "bw-1"
    blob = " ".join(page["notes"])
    assert "BW by render (transparency group present)" in blob
    assert any("warning" in b["text"] for b in page["blockers"])


def test_gray_alternate_spot_is_not_color(tmp_path):
    import pikepdf
    path = str(tmp_path / "grayspot.pdf")
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page()
    fn = pikepdf.Dictionary(FunctionType=2, Domain=[0, 1], C0=[0], C1=[1], N=1)
    sep = pikepdf.Array([pikepdf.Name.Separation, pikepdf.Name("/StaffGray"),
                         pikepdf.Name.DeviceGray, fn])
    page.Resources = pikepdf.Dictionary(ColorSpace=pikepdf.Dictionary(Sp0=sep))
    page.Contents = pikepdf.Stream(pdf, b"/Sp0 cs 0.5 scn 100 100 80 40 re f\n")
    pdf.save(path)
    data = analyze_file(path)
    page = data["pages"][0]
    assert page["label"] != "COLOR"
    assert page["target"] != "color"
    blob = " ".join(b["text"] for b in page["blockers"]) + " " + " ".join(page["notes"])
    assert "StaffGray" in blob
    assert "maps to gray" in blob


def test_icc_gray_is_not_color(tmp_path):
    import pikepdf
    path = str(tmp_path / "icc.pdf")
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page()
    icc = pikepdf.Stream(pdf, b"\0")
    icc.N = 1
    cs = pikepdf.Array([pikepdf.Name.ICCBased, icc])
    page.Resources = pikepdf.Dictionary(ColorSpace=pikepdf.Dictionary(Cs1=cs))
    page.Contents = pikepdf.Stream(pdf, b"/Cs1 cs 0.4 scn 80 80 120 60 re f\n")
    pdf.save(path)
    data = analyze_file(path)
    page = data["pages"][0]
    assert page["label"] == "GRAY"
    assert page["target"].startswith("gray-")
    blob = " ".join(b["text"] for b in page["blockers"]) + " " + " ".join(page["notes"])
    assert "ICC gray" in blob
    assert page["label"] != "COLOR"


def test_gray_drawing_is_explained(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.draw_rect(pymupdf.Rect(72, 72, 200, 140), color=None, fill=(0.45, 0.45, 0.45))
    path = str(tmp_path / "gdraw.pdf")
    _save(doc, path)
    data = analyze_file(path)
    page = data["pages"][0]
    assert page["label"] == "GRAY"
    assert page["images"] == []
    blob = " ".join(page["notes"])
    assert "gray drawing" in blob
    assert "bpp" in blob


def test_missing_dependency_message(monkeypatch):
    import builtins
    from krop.analyze import MissingDependency, require_analysis_deps
    real = builtins.__import__

    def fake(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name in ("numpy", "pikepdf"):
            raise ModuleNotFoundError("No module named '%s'" % name, name=name)
        return real(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(MissingDependency) as exc:
        require_analysis_deps()
    assert "numpy" in exc.value.packages
    assert "pikepdf" in exc.value.packages
    assert "pip install" in str(exc.value)
    assert "numpy" in str(exc.value)


def test_dock_names_missing_package(window, tmp_path, qapp, monkeypatch):
    from krop.analyze import MissingDependency
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 80), "Hi", fontsize=12, color=(0, 0, 0))
    src = str(tmp_path / "one.pdf")
    _save(doc, src)

    def boom(path, cancel=None):
        raise MissingDependency(["pikepdf"])

    monkeypatch.setattr("krop.analyze.analyze_file", boom)
    w = window
    w.show()
    qapp.processEvents()
    w.actionAnalysis.setChecked(True)
    w.openFile(src, False)
    deadline = time.time() + 10
    while time.time() < deadline:
        qapp.processEvents()
        if "pikepdf" in w.analysisPanel.summary.text():
            break
        time.sleep(0.05)
    summary = w.analysisPanel.summary.text()
    assert "Missing package: pikepdf" in summary
    assert "pip install pikepdf" in summary
    assert summary != "Analysis failed."
    text = open(w.logPath).read()
    assert "ANALYZE" in text
    assert "Traceback" in text
    assert "MissingDependency" in text or "pikepdf" in text


def test_heavy_shared_form_is_fast(tmp_path):
    import pikepdf
    pdf = pikepdf.Pdf.new()
    parts = ["0 G\n"]
    for i in range(2500):
        y = 10 + (i % 700)
        parts.append("%d %d m %d %d l S\n" % (20, y, 400, y))
    form = pikepdf.Stream(pdf, "".join(parts).encode())
    form.Type = pikepdf.Name.XObject
    form.Subtype = pikepdf.Name.Form
    form.BBox = pikepdf.Array([0, 0, 500, 800])
    for n in range(20):
        page = pdf.add_blank_page()
        page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Fm0=form))
        extra = "".join("%d %d m %d %d l S\n" % (i % 400, i % 500, 10, 10) for i in range(200))
        page.Contents = pikepdf.Stream(pdf, ("0 G\n" + extra + "q /Fm0 Do Q\n").encode())
    path = str(tmp_path / "heavy.pdf")
    pdf.save(path)
    t0 = time.perf_counter()
    data = analyze_file(path)
    elapsed = time.perf_counter() - t0
    assert data["summary"]["label"] == "BW"
    assert elapsed < 5.0, elapsed


def test_needs_check_when_render_fails(tmp_path, monkeypatch):
    import pikepdf
    monkeypatch.setattr("krop.analyze._render_measure", lambda page, annots=False: None)
    path = str(tmp_path / "shade.pdf")
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page()
    fn = pikepdf.Dictionary(FunctionType=2, Domain=[0, 1], C0=[0, 0, 0], C1=[1, 0, 0], N=1)
    sh = pikepdf.Dictionary(ShadingType=2, ColorSpace=pikepdf.Name.DeviceRGB,
                            Coords=[0, 0, 100, 0], Function=fn)
    page.Resources = pikepdf.Dictionary(Shading=pikepdf.Dictionary(Sh1=sh))
    page.Contents = pikepdf.Stream(pdf, b"/Sh1 sh\n")
    pdf.save(path)
    data = analyze_file(path)
    assert data["pages"][0]["label"] == "NEEDS-CHECK"
    assert data["pages"][0]["target"] == "color"


def test_duplicate_images(tmp_path):
    import pikepdf
    path = str(tmp_path / "dup.pdf")
    pdf = pikepdf.Pdf.new()
    raw = bytes([0, 85, 170, 255] * 16)
    import zlib
    encoded = zlib.compress(raw)

    def add(page, stream):
        page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=stream))
        page.Contents = pikepdf.Stream(pdf, b"q 8 0 0 8 0 0 cm /Im0 Do Q\n")

    page = pdf.add_blank_page(page_size=(8, 8))
    st = pikepdf.Stream(pdf, encoded)
    st.Type = pikepdf.Name.XObject
    st.Subtype = pikepdf.Name.Image
    st.Width = st.Height = 8
    st.ColorSpace = pikepdf.Name.DeviceGray
    st.BitsPerComponent = 8
    st.Filter = pikepdf.Name.FlateDecode
    add(page, st)
    page2 = pdf.add_blank_page(page_size=(8, 8))
    st2 = pikepdf.Stream(pdf, encoded)
    st2.Type = pikepdf.Name.XObject
    st2.Subtype = pikepdf.Name.Image
    st2.Width = st2.Height = 8
    st2.ColorSpace = pikepdf.Name.DeviceGray
    st2.BitsPerComponent = 8
    st2.Filter = pikepdf.Name.FlateDecode
    add(page2, st2)
    pdf.save(path)
    data = analyze_file(path)
    assert data["summary"]["duplicates"] == 1
    assert any(im.get("dup_of") for im in data["images"])


def test_analyze_only_cli(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Hello", fontsize=18, color=(0, 0, 0))
    src = str(tmp_path / "bw.pdf")
    _save(doc, src)
    js = str(tmp_path / "out.json")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
               KROP_LOG="off")
    r = subprocess.run([sys.executable, "-m", "krop", "--analyze-only", "--analyze-json", js,
                        "--log", "off", src], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "document: BW" in r.stdout
    assert "target bw-1" in r.stdout
    data = json.load(open(js))
    assert data["summary"]["target"] == "bw-1"
    assert not os.path.exists(os.path.splitext(src)[0] + ".kroppy.log")


def test_panel_fills_and_jumps(window, tmp_path, qapp):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "One", fontsize=16, color=(0, 0, 0))
    doc.new_page().insert_text((72, 100), "Refrain", fontsize=16, color=(0.8, 0, 0))
    src = str(tmp_path / "two.pdf")
    _save(doc, src)
    w = window
    w.show()
    qapp.processEvents()
    w.actionAnalysis.setChecked(True)
    qapp.processEvents()
    assert w.analysisPanel.isVisible()
    w.openFile(src, False)
    deadline = time.time() + 10
    while time.time() < deadline:
        qapp.processEvents()
        if "Document:" in w.analysisPanel.summary.text():
            break
        time.sleep(0.05)
    summary = w.analysisPanel.summary.text()
    assert "MOSTLY-BW-WITH-COLOR-ACCENTS" in summary or "COLOR" in summary
    assert w.analysisPanel.tree.topLevelItemCount() == 2
    item = w.analysisPanel.tree.topLevelItem(1)
    w.analysisPanel._clicked(item, 0)
    qapp.processEvents()
    assert w.viewer.currentPageIndex == 1
    text = open(w.logPath).read()
    assert "ANALYZE" in text


def test_dock_follows_page_and_click_navigates(window, tmp_path, qapp, monkeypatch):
    """The highlighted row tracks the viewer, and a single click or key moves the viewer."""
    import threading
    from krop.config import PYQT6
    if PYQT6:
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        left = Qt.MouseButton.LeftButton
        key_down = Qt.Key.Key_Down
        user = Qt.ItemDataRole.UserRole
    else:
        from PyQt5.QtCore import Qt
        from PyQt5.QtTest import QTest
        left = Qt.LeftButton
        key_down = Qt.Key_Down
        user = Qt.UserRole

    doc = pymupdf.open()
    for i in range(8):
        page = doc.new_page()
        page.insert_text((72, 80), "Page %d" % (i + 1), fontsize=16, color=(0, 0, 0))
    doc[1].insert_text((72, 140), "Refrain", fontsize=16, color=(0.85, 0.05, 0.05))
    src = str(tmp_path / "pages.pdf")
    _save(doc, src)

    from krop.analyze import analyze_file as real
    gate = threading.Event()
    started = threading.Event()

    def slow(path, cancel=None):
        started.set()
        if not gate.wait(5):
            return None
        if cancel is not None and cancel.is_set():
            return None
        return real(path, cancel)

    monkeypatch.setattr("krop.analyze.analyze_file", slow)
    w = window
    w.show()
    w.resize(1000, 800)
    qapp.processEvents()
    w.actionAnalysis.setChecked(True)
    w.analysisPanel.tree.setFixedHeight(52)
    qapp.processEvents()

    nav = []
    orig = w.slotShowAnalysisPage

    def wrapped(page):
        nav.append(int(page))
        orig(page)

    w.analysisPanel.page_requested.disconnect(orig)
    w.analysisPanel.page_requested.connect(wrapped)

    def current_page():
        item = w.analysisPanel.tree.currentItem()
        if item is None:
            return None
        return item.data(0, user)

    def row_visible(item):
        rect = w.analysisPanel.tree.visualItemRect(item)
        return rect.height() > 0 and w.analysisPanel.tree.viewport().rect().intersects(rect)

    def click(item):
        tree = w.analysisPanel.tree
        tree.scrollToItem(item)
        qapp.processEvents()
        rect = tree.visualItemRect(item).intersected(tree.viewport().rect())
        assert rect.width() > 1 and rect.height() > 1, rect
        QTest.mouseClick(tree.viewport(), left, pos=rect.center())
        qapp.processEvents()

    w.openFile(src, False)
    assert started.wait(5)
    # Page changed while the table is still empty. Highlight once the row exists.
    w.viewer.currentPageIndex = 2
    qapp.processEvents()
    assert nav == []
    gate.set()
    deadline = time.time() + 10
    while time.time() < deadline:
        qapp.processEvents()
        if "Document:" in w.analysisPanel.summary.text() and current_page() == 3:
            break
        time.sleep(0.05)
    assert current_page() == 3, current_page()
    assert w.viewer.currentPageIndex == 2
    assert row_visible(w.analysisPanel.tree.currentItem())
    assert nav == []

    nav.clear()
    w.ui.actionNextPage.trigger()
    qapp.processEvents()
    assert w.viewer.currentPageIndex == 3
    assert current_page() == 4
    assert nav == []

    w.slotCurrentPageEdited("8")
    qapp.processEvents()
    assert w.viewer.currentPageIndex == 7
    assert current_page() == 8
    assert row_visible(w.analysisPanel.tree.currentItem())

    page6 = None
    for i in range(w.analysisPanel.tree.topLevelItemCount()):
        item = w.analysisPanel.tree.topLevelItem(i)
        if item.data(0, user) == 6:
            page6 = item
            break
    assert page6 is not None
    nav.clear()
    click(page6)
    assert nav == [6], nav
    assert w.viewer.currentPageIndex == 5
    assert current_page() == 6

    # Clicking the already-selected row still asks the viewer to show that page.
    nav.clear()
    click(page6)
    assert nav == [6], nav
    assert w.viewer.currentPageIndex == 5

    page2 = w.analysisPanel.tree.topLevelItem(1)
    assert page2.data(0, user) == 2
    assert page2.childCount() > 0
    nav.clear()
    click(page2.child(0))
    assert nav == [2], nav
    assert w.viewer.currentPageIndex == 1

    w.analysisPanel.tree.setFocus()
    qapp.processEvents()
    nav.clear()
    QTest.keyClick(w.analysisPanel.tree, key_down)
    qapp.processEvents()
    assert current_page() is not None
    assert w.viewer.currentPageIndex == int(current_page()) - 1
    assert len(nav) <= 1
