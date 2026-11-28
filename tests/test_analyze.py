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

    def boom(path, cancel=None, **kwargs):
        raise MissingDependency(["pikepdf"])

    monkeypatch.setattr("krop.analyze.analyze_page", boom)
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


def test_page_visit_order_reprioritizes():
    from krop.analyze import page_visit_order
    assert page_visit_order(5, 2) == [2, 3, 1, 4, 0]
    focus = [2]
    seen = []
    pending = set(range(5))
    while pending:
        nxt = next(i for i in page_visit_order(5, focus[0]) if i in pending)
        pending.remove(nxt)
        seen.append(nxt)
        if nxt == 2:
            focus[0] = 0
    assert seen[0] == 2
    assert seen[1] == 0


def test_image_detail_gray_target_and_uses(tmp_path):
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
    page.insert_image(pymupdf.Rect(72, 320, 220, 420), pixmap=pix)
    path = str(tmp_path / "twice.pdf")
    _save(doc, path)
    data = analyze_file(path)
    page = data["pages"][0]
    assert page["image_count"] == 1
    im = page["images"][0]
    assert im["uses"] == 2
    assert im["target"] == "gray-2"
    assert im["error_metric"] == "mae"
    assert im["error_mae"] == 0
    assert "xref %d" % im["xref"] in im["detail"]
    assert "ICC N=1" in im["colorspace_text"] or "Gray" in im["colorspace_text"]
    assert im["structure"] == "1ch 8bpp"
    assert "1ch 8bpp" in im["detail"] and "dpi" in im["detail"]
    assert im["chosen_target"] == im["recommended_target"] == "gray-2"
    assert any(c["id"] == "1bpp" and c["over"] for c in im["candidates"])
    gids = [c["id"] for c in im["candidates"]]
    for required in ("rgb-24", "jpeg-85", "gray-jpeg-60", "gray-8", "gray-4", "gray-2", "1bpp", "keep"):
        assert required in gids
    assert any("mae" in (c.get("label") or "") or c.get("error_mae") is not None for c in im["candidates"])
    assert im["rep_bytes"] < im["bytes"]
    assert page["savings_pct"] > 0
    blob = "\n".join(__import__("krop.analyze", fromlist=["analyze_lines"]).analyze_lines(data))
    assert "image xref" in blob and "mae" in blob
    assert "savings" in blob


def test_shared_image_is_encoded_once(tmp_path, monkeypatch):
    import numpy as np
    from krop import imagetarget
    doc = pymupdf.open()
    page = doc.new_page()
    arr = np.zeros((32, 32), np.uint8)
    arr[:, :16] = 0
    arr[:, 16:] = 255
    pix = pymupdf.Pixmap(pymupdf.csGRAY, 32, 32, arr.tobytes(), False)
    xref = page.insert_image(pymupdf.Rect(72, 72, 200, 200), pixmap=pix)
    page2 = doc.new_page()
    page2.insert_image(pymupdf.Rect(72, 72, 180, 180), xref=xref)
    path = str(tmp_path / "shared.pdf")
    _save(doc, path)
    calls = []
    real = imagetarget.recommend

    def wrapped(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(imagetarget, "recommend", wrapped)
    seen = []
    data = analyze_file(path, on_image=seen.append)
    assert calls == [1]
    assert len(seen) == 1
    assert seen[0]["pages"] == [1, 2]
    pages = data["pages"]
    assert pages[0]["images"][0]["link_pages"] == 2
    assert pages[0]["images"][0]["link_placements"] >= 2
    assert 2 in pages[0]["images"][0]["pages"]
    assert pages[1]["images"][0]["xref"] == pages[0]["images"][0]["xref"]
    assert pages[1]["images"][0]["shared"] is True
    assert "counted on p1" in pages[1]["images"][0]["detail"]
    assert pages[1]["image_bytes"] == 0
    assert data["summary"]["image_bytes"] == pages[0]["images"][0]["bytes"]
    blob = "\n".join(__import__("krop.analyze", fromlist=["analyze_lines"]).analyze_lines(data))
    assert "links 2 pages" in blob and "[1,2]" in blob


def test_identical_streams_share_one_target(tmp_path, monkeypatch):
    import pikepdf
    import zlib
    from krop import imagetarget
    path = str(tmp_path / "same-bytes.pdf")
    pdf = pikepdf.Pdf.new()
    raw = bytes([0, 255] * 32)
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
    calls = []
    real = imagetarget.recommend

    def wrapped(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(imagetarget, "recommend", wrapped)
    data = analyze_file(path)
    assert calls == [1]
    assert data["summary"]["duplicates"] == 1
    assert any(im.get("dup_of") for im in data["images"])
    assert data["summary"]["image_bytes"] == data["pages"][0]["images"][0]["bytes"]
    assert data["pages"][1]["image_bytes"] == 0
    assert data["pages"][1]["images"][0]["link_pages"] == 2


def test_chosen_target_updates_shared_rollups(tmp_path):
    import numpy as np
    from krop.analyze import apply_chosen_target
    doc = pymupdf.open()
    page = doc.new_page()
    arr = np.zeros((32, 32), np.uint8)
    arr[:, :16] = 0
    arr[:, 16:] = 255
    pix = pymupdf.Pixmap(pymupdf.csGRAY, 32, 32, arr.tobytes(), False)
    xref = page.insert_image(pymupdf.Rect(72, 72, 200, 200), pixmap=pix)
    doc.new_page().insert_image(pymupdf.Rect(72, 72, 180, 180), xref=xref)
    path = str(tmp_path / "choice.pdf")
    _save(doc, path)
    data = analyze_file(path)
    im = data["pages"][0]["images"][0]
    keep = next(c for c in im["candidates"] if c["id"] == "keep")
    apply_chosen_target(data, xref, keep)
    assert data["pages"][0]["images"][0]["chosen_target"] == "keep"
    assert data["pages"][1]["images"][0]["chosen_target"] == "keep"
    assert data["pages"][0]["images"][0]["recommended_target"] in ("keep", "1bpp")
    assert data["pages"][1]["image_bytes"] == 0
    assert data["summary"]["image_bytes"] == im["bytes"]
    blob = __import__("json").dumps(data)
    assert '"chosen_target": "keep"' in blob
    assert '"recommended_target"' in blob


def test_target_combo_updates_row(window, tmp_path, qapp):
    import numpy as np
    doc = pymupdf.open()
    page = doc.new_page()
    arr = np.zeros((40, 80), np.uint8)
    arr[:, :20] = 0
    arr[:, 20:40] = 85
    arr[:, 40:60] = 170
    arr[:, 60:] = 255
    pix = pymupdf.Pixmap(pymupdf.csGRAY, 80, 40, arr.tobytes(), False)
    page.insert_image(pymupdf.Rect(72, 72, 300, 200), pixmap=pix)
    src = str(tmp_path / "combo.pdf")
    _save(doc, src)
    w = window
    w.show()
    qapp.processEvents()
    w.actionAnalysis.setChecked(True)
    w.openFile(src, False)
    deadline = time.time() + 10
    while time.time() < deadline:
        qapp.processEvents()
        if "Document:" in w.analysisPanel.summary.text():
            break
        time.sleep(0.05)
    child = w.analysisPanel.tree.topLevelItem(0).child(0)
    combo = w.analysisPanel.tree.itemWidget(child, 2)
    assert combo is not None
    labels = [combo.itemText(i) for i in range(combo.count())]
    assert any("1ch 1bpp" in text and "over limit" in text for text in labels)
    keep = combo.findData("keep")
    assert keep >= 0
    assert combo.itemText(keep).startswith("keep:")
    assert "mae 0" in combo.itemText(keep)
    assert combo.currentData() == "gray-2"
    # gray-4 is under the threshold but was not encoded yet.
    index = combo.findData("gray-4")
    assert index >= 0
    combo.setCurrentIndex(index)
    deadline = time.time() + 10
    while time.time() < deadline:
        qapp.processEvents()
        if child.text(3) != "..." and "1ch 4bpp" in child.text(3):
            break
        time.sleep(0.05)
    assert "1ch 4bpp" in child.text(3)
    assert "mae" in child.text(3)
    assert w.analysisPanel._data["pages"][0]["images"][0]["chosen_target"] == "gray-4"
    # The choice sticks for the session.
    assert w.analysisPanel._choices


def test_shared_image_row_appears_before_later_page(window, qapp):
    w = window
    w.analysisPanel.begin(3)
    w.analysisPanel.show_shared_image({
        "xref": 9, "page": 1, "pages": [1, 3],
        "detail": "xref 9 links 2 page(s) [p1, p3]",
        "target": "keep", "target_text": "keep",
        "link_tip": "Used on p1, p3 (2 placement(s))",
    })
    qapp.processEvents()
    row = w.analysisPanel.tree.topLevelItem(2)
    assert row.text(1) == "pending"
    assert row.childCount() == 1
    assert "xref 9" in row.child(0).text(1)
    assert "p1, p3" in row.child(0).toolTip(1)


def test_keep_summary_names_the_current_encoding():
    from krop.analyze import keep_summary
    assert keep_summary({
        "filter_name": "DCT", "colorspace_text": "DeviceRGB", "bpc": 8, "bytes": 421888,
    }) == "keep: JPEG (DCT) RGB 8bpc, 412 KB"
    assert keep_summary({
        "filter_name": "CCITT", "colorspace_text": "DeviceGray", "bpc": 1, "bytes": 38 * 1024,
    }) == "keep: CCITT 1bpp, 38 KB"
    assert keep_summary({
        "filter_name": "Flate", "colorspace_text": "Indexed DeviceRGB",
        "indexed_colors": 16, "bpc": 8, "bytes": 1200,
    }) == "keep: Flate indexed 16 colors, 1.2 KB"


def test_combo_pending_label_updates_while_popup_is_open(window, qapp):
    """measuring… is replaced even if the dropdown is open, and a rebuild does not restore it."""
    from krop.qt import Qt
    panel = window.analysisPanel
    panel.show()
    qapp.processEvents()
    assert "measuring…" in panel.legend.text()
    assert "orange" in panel.legend.text()
    image = {
        "xref": 5, "page": 1, "pages": [1], "share_key": "x5",
        "detail": "xref 5", "target": "gray-2", "chosen_target": "gray-2",
        "recommended_target": "gray-2", "target_text": "1ch 2bpp",
        "filter_name": "DCT", "colorspace_text": "DeviceRGB", "bpc": 8,
        "bytes": 421888, "keep_text": "keep: JPEG (DCT) RGB 8bpc, 412 KB",
        "candidates": [
            {"id": "gray-2", "label": "1ch 2bpp", "over": False,
             "error_mae": 0.0, "est_bytes": 100, "savings_pct": 50},
            {"id": "jpeg-45", "label": "3ch 24bpp JPEG q45", "pending": True, "over": False},
            {"id": "1bpp", "label": "1ch 1bpp", "over": True,
             "error_mae": 7.2, "est_bytes": 40, "savings_pct": 90},
            {"id": "keep", "label": "keep: JPEG (DCT) RGB 8bpc, 412 KB",
             "over": False, "error_mae": 0, "est_bytes": 421888},
        ],
    }
    page = {"page": 1, "images": [image], "label": "COLOR", "target": "color",
            "image_count": 1, "blocker_count": 0, "image_bytes": 421888,
            "image_est": 100, "savings_pct": 50}
    panel.begin(1)
    panel.add_page(page)
    qapp.processEvents()
    child = panel.tree.topLevelItem(0).child(0)
    combo = panel.tree.itemWidget(child, 2)
    jpeg = combo.findData("jpeg-45")
    one = combo.findData("1bpp")
    keep = combo.findData("keep")
    assert "measuring…" in combo.itemText(jpeg)
    assert combo.itemText(jpeg).count("…") == 1
    limit = panel.error_threshold()
    shown = str(int(limit)) if abs(limit - round(limit)) < 1e-6 else ("%g" % limit)
    assert ("mae 7.2 > %s (over limit)" % shown) in combo.itemText(one)
    assert combo.itemText(keep).startswith("keep: JPEG (DCT) RGB 8bpc, 412 KB")
    assert "mae 0" in combo.itemText(keep)
    try:
        tip_role = Qt.ItemDataRole.ToolTipRole
    except AttributeError:
        tip_role = Qt.ToolTipRole
    assert "measuring…" in combo.itemData(jpeg, tip_role)
    assert "over limit" in combo.itemData(one, tip_role)
    combo.showPopup()
    qapp.processEvents()
    image["candidates"][1] = {
        "id": "jpeg-45", "label": "3ch 24bpp JPEG q45", "pending": False, "over": True,
        "error_mae": 7.2, "error_max": 40, "est_bytes": 5000, "savings_pct": 80, "codec": "DCT",
    }
    panel._refresh_image_texts(panel.tree.topLevelItem(0), image)
    qapp.processEvents()
    text = combo.itemText(jpeg)
    assert "measuring" not in text
    assert "over limit" in text
    model = combo.model()
    shown_in_popup = model.data(model.index(jpeg, 0))
    assert "measuring" not in shown_in_popup
    assert "over limit" in shown_in_popup
    combo.hidePopup()
    # Rebuilding the row uses the filled candidate, not the old ellipsis.
    panel._fill_row(panel.tree.topLevelItem(0), page)
    qapp.processEvents()
    combo = panel.tree.itemWidget(panel.tree.topLevelItem(0).child(0), 2)
    assert "measuring" not in combo.itemText(combo.findData("jpeg-45"))


def test_heavy_ladder_is_pending_until_filled(tmp_path):
    """JPEG and palette sizes are computed after the cheap default, off the UI pass."""
    import numpy as np
    from krop.imagetarget import choose_smallest, fill_pending_file
    doc = pymupdf.open()
    page = doc.new_page()
    arr = np.zeros((16, 16, 3), np.uint8)
    arr[:, :8] = (180, 40, 40)
    arr[:, 8:] = (40, 40, 180)
    pix = pymupdf.Pixmap(pymupdf.csRGB, 16, 16, arr.tobytes(), False)
    xref = page.insert_image(pymupdf.Rect(72, 72, 180, 180), pixmap=pix)
    path = str(tmp_path / "defer.pdf")
    _save(doc, path)
    quick = analyze_file(path, defer_heavy=True)["pages"][0]["images"][0]
    ids = [c["id"] for c in quick["candidates"]]
    assert "jpeg-85" in ids and "indexed-16" in ids and "gray-jpeg-60" in ids
    assert any(c["id"] == "jpeg-85" and c.get("pending") for c in quick["candidates"])
    filled = fill_pending_file(
        path, xref, quick["candidates"], quick["bytes"], quick.get("filter_name") or "",
        4.0, quick["width"], quick["height"])
    assert all(not c.get("pending") for c in filled)
    jpeg = next(c for c in filled if c["id"] == "jpeg-45")
    assert jpeg["est_bytes"] and jpeg["error_mae"] is not None and jpeg["codec"] == "DCT"
    best = choose_smallest(filled, quick["bytes"])
    full = analyze_file(path)["pages"][0]["images"][0]
    assert best["id"] == full["target"]
    assert full["chosen_target"] == full["recommended_target"] == full["target"]


def test_indexed_target_for_two_colors(tmp_path):
    import numpy as np
    doc = pymupdf.open()
    page = doc.new_page()
    arr = np.zeros((24, 48, 3), np.uint8)
    arr[:, :24] = (210, 20, 20)
    arr[:, 24:] = (20, 30, 200)
    pix = pymupdf.Pixmap(pymupdf.csRGB, 48, 24, arr.tobytes(), False)
    page.insert_image(page.rect, pixmap=pix)
    path = str(tmp_path / "two-color.pdf")
    _save(doc, path)
    data = analyze_file(path)
    im = data["pages"][0]["images"][0]
    assert data["summary"]["label"] == "COLOR"
    assert im["structure"] == "3ch 24bpp"
    ids = [c["id"] for c in im["candidates"]]
    for required in ("jpeg-95", "jpeg-85", "jpeg-75", "jpeg-60", "jpeg-45",
                     "indexed-256", "indexed-128", "indexed-64", "indexed-32",
                     "indexed-16", "indexed-8", "indexed-4",
                     "gray-8", "gray-4", "gray-2",
                     "gray-jpeg-85", "gray-jpeg-60", "1bpp", "keep"):
        assert required in ids
    under = [c for c in im["candidates"]
             if c["id"] != "keep" and not c["over"] and c.get("est_bytes") is not None]
    smallest = min(under, key=lambda c: (c["est_bytes"], c["id"]))
    assert im["target"] == im["recommended_target"] == im["chosen_target"] == smallest["id"]
    assert im["error_mae"] <= 4
    assert "rgb-24" not in ids or im["filter_name"] not in ("Flate", "LZW")
    assert all(
        (c.get("refused") or str(c.get("note") or "").startswith("refused") or c.get("error_mae") is not None)
        and c.get("est_bytes") is not None
        for c in im["candidates"] if c["id"] != "keep")
    blob = __import__("json").dumps(data)
    assert '"chosen_target"' in blob and '"candidates"' in blob


def test_error_threshold_changes_target(tmp_path):
    import numpy as np
    doc = pymupdf.open()
    page = doc.new_page()
    arr = np.arange(256, dtype=np.uint8).reshape(16, 16)
    pix = pymupdf.Pixmap(pymupdf.csGRAY, 16, 16, arr.tobytes(), False)
    page.insert_image(pymupdf.Rect(72, 72, 200, 200), pixmap=pix)
    path = str(tmp_path / "ramp.pdf")
    _save(doc, path)
    tight = analyze_file(path, threshold=0)["pages"][0]["images"][0]
    loose = analyze_file(path, threshold=80)["pages"][0]["images"][0]
    # At zero tolerance only an exact encoding passes. Re-encoding the
    # original 8-bit samples is not smaller, so the target stays "keep".
    assert tight["target"] in ("keep", "gray-8")
    assert tight["error_mae"] == 0
    # desky-scan-tools gates on max error. 1-bit Otsu of a ramp exceeds 80;
    # 2-bit gray (steps of 85, max error 42) is the smallest encoding that passes.
    assert loose["target"] == "gray-2"
    assert loose["error_max"] <= 80


def test_panel_lists_each_image(window, tmp_path, qapp):
    import numpy as np
    doc = pymupdf.open()
    page = doc.new_page()
    arr = np.zeros((20, 20), np.uint8)
    arr[:10] = 0
    arr[10:] = 255
    pix = pymupdf.Pixmap(pymupdf.csGRAY, 20, 20, arr.tobytes(), False)
    page.insert_image(pymupdf.Rect(72, 72, 200, 200), pixmap=pix)
    src = str(tmp_path / "one.pdf")
    _save(doc, src)
    w = window
    w.show()
    qapp.processEvents()
    w.actionAnalysis.setChecked(True)
    w.openFile(src, False)
    deadline = time.time() + 10
    while time.time() < deadline:
        qapp.processEvents()
        if "Document:" in w.analysisPanel.summary.text():
            break
        time.sleep(0.05)
    assert "Document:" in w.analysisPanel.summary.text()
    row = w.analysisPanel.tree.topLevelItem(0)
    assert row.text(3) == "1"
    assert row.childCount() >= 1
    detail = row.child(0).text(1)
    assert "xref" in detail and "1ch" in detail
    combo = w.analysisPanel.tree.itemWidget(row.child(0), 2)
    assert combo is not None
    assert combo.currentData() in ("1bpp", "keep", "gray-2", "gray-4", "gray-8")


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

    from krop.analyze import analyze_page as real_page
    gate = threading.Event()
    started = threading.Event()

    def slow(path, index, **kwargs):
        started.set()
        if not gate.wait(5):
            return None
        return real_page(path, index, **kwargs)

    monkeypatch.setattr("krop.analyze.analyze_page", slow)
    w = window
    w.show()
    w.resize(1000, 800)
    qapp.processEvents()
    w.actionAnalysis.setChecked(True)
    w.analysisPanel._scheduler.idle_delay = 0
    w.analysisPanel.tree.setFixedHeight(160)
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
    deadline = time.time() + 5
    while time.time() < deadline and not started.is_set():
        qapp.processEvents()
        time.sleep(0.02)
    assert started.is_set()
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
    deadline = time.time() + 10
    while time.time() < deadline and page2.childCount() == 0:
        qapp.processEvents()
        time.sleep(0.05)
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


def _false_comparisons(text):
    import re
    bad = []
    for left, right in re.findall(r"([0-9]+(?:\.[0-9]+)?) > ([0-9]+(?:\.[0-9]+)?)", text):
        if float(left) <= float(right):
            bad.append((left, right))
    return bad


def test_over_limit_label_names_the_test_that_failed():
    from krop.analyzepanel import _candidate_label
    # Mean is under the limit; only the percentile tail fails. Do not print "mae 3 > 4".
    cand = {
        "id": "jpeg-85", "label": "3ch 24bpp JPEG q85", "over": True,
        "error_mae": 3.0, "error_p995": 40.0, "error_max": 212,
    }
    text = _candidate_label(cand, limit=4)
    assert "p99.5 40.0 > 16" in text
    assert "mae 3.0 > 4" not in text
    assert _false_comparisons(text) == []
    both = {
        "id": "jpeg-45", "label": "3ch 24bpp JPEG q45", "over": True,
        "error_mae": 7.2, "error_p995": 30.0, "error_max": 200,
    }
    text = _candidate_label(both, limit=4)
    assert "mae 7.2 > 4" in text
    assert "p99.5" in text
    assert _false_comparisons(text) == []
    fine = {
        "id": "gray-4", "label": "1ch 4bpp", "over": False,
        "error_mae": 1.0, "error_p995": 4.0, "error_max": 200,
    }
    assert "over limit" not in _candidate_label(fine, limit=4)
    assert _false_comparisons(_candidate_label(fine, limit=4)) == []


def test_target_combo_popup_scrolls(qapp):
    from krop.analyzepanel import _scrollable_combo
    from krop.qt import QComboBox, QListView
    try:
        from PyQt6.QtCore import QPoint, QPointF, Qt
        from PyQt6.QtGui import QWheelEvent
        from PyQt6.QtWidgets import QWidget
    except ImportError:
        from PyQt5.QtCore import QPoint, QPointF, Qt
        from PyQt5.QtGui import QWheelEvent
        from PyQt5.QtWidgets import QWidget
    host = QWidget()
    host.resize(400, 80)
    combo = QComboBox(host)
    _scrollable_combo(combo)
    for i in range(40):
        combo.addItem("choice %d" % i, i)
    host.show()
    qapp.processEvents()
    assert combo.maxVisibleItems() == 15
    assert isinstance(combo.view(), QListView)
    assert "combobox-popup: 0" in combo.styleSheet()
    combo.showPopup()
    qapp.processEvents()
    view = combo.view()
    bar = view.verticalScrollBar()
    assert bar.maximum() > 0
    before = bar.value()
    bar.setValue(bar.maximum())
    qapp.processEvents()
    assert bar.value() > before
    center = view.viewport().rect().center()
    assert view.indexAt(center).row() > 10
    # Wheel moves the same scrollbar.
    bar.setValue(0)
    local = QPointF(center)
    global_pos = QPointF(view.viewport().mapToGlobal(QPoint(center.x(), center.y())))
    buttons = Qt.MouseButton.NoButton if hasattr(Qt, "MouseButton") else Qt.NoButton
    mods = Qt.KeyboardModifier.NoModifier if hasattr(Qt, "KeyboardModifier") else Qt.NoModifier
    phase = Qt.ScrollPhase.NoScrollPhase if hasattr(Qt, "ScrollPhase") else Qt.ScrollUpdate
    event = QWheelEvent(local, global_pos, QPoint(0, 0), QPoint(0, -120), buttons, mods, phase, False)
    qapp.sendEvent(view.viewport(), event)
    qapp.processEvents()
    assert bar.value() > 0
    combo.hidePopup()
