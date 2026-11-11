# -*- coding: utf-8 -*-
"""Read-only PDF analysis: smallest safe color and bit depth.

Samples images instead of inspecting every pixel, and flags shadings,
patterns, transparency, and spot colors without guessing their color.
"""
import hashlib

# Near-gray if max-min channel is under this fraction of full scale.
CHROMA_MAX = 0.03
# Near-black / near-white snap, as a fraction of full luminance.
BLACK_MAX = 0.10
WHITE_MIN = 0.90
# Image sample budget. Large scans are strided down to about this many pixels.
SAMPLE_PIXELS = 200000

_SPOT = ("spot", "separation", "devicen", "indexed", "pattern", "icc")
_RISKY = ("shading", "pattern", "transparency", "spot", "separation", "devicen", "indexed", "icc")


def _unit(v):
    v = float(v)
    if v > 1.0001:
        return v / 255.0
    if v < 0:
        return 0.0
    return v


def _luma(r, g, b):
    return 0.299 * r + 0.587 * g + 0.114 * b


def classify_rgb(r, g, b):
    """Return black, white, gray, or color. Components are 0-1 or 0-255."""
    r, g, b = _unit(r), _unit(g), _unit(b)
    if max(r, g, b) - min(r, g, b) > CHROMA_MAX:
        return "color"
    y = _luma(r, g, b)
    if y <= BLACK_MAX:
        return "black"
    if y >= WHITE_MIN:
        return "white"
    return "gray"


def classify_gray(g):
    g = _unit(g)
    if g <= BLACK_MAX:
        return "black"
    if g >= WHITE_MIN:
        return "white"
    return "gray"


def bpp_for_levels(levels):
    levels = int(levels or 0)
    if levels <= 2:
        return 1
    if levels <= 4:
        return 2
    if levels <= 16:
        return 4
    return 8


def bpp_for_gray_value(g):
    """Bits needed to keep one gray (0-1 or 0-255) without rounding it away."""
    kind = classify_gray(g)
    if kind in ("black", "white"):
        return 1
    v = int(round(_unit(g) * 255))
    if min(abs(v - x) for x in (0, 85, 170, 255)) <= 8:
        return 2
    if min(abs(v - i * 17) for i in range(16)) <= 8:
        return 4
    return 8


def _blocker(page, kind, text):
    return {"page": page, "kind": kind, "text": text}


def _cs_kind(resources, name):
    n = str(name)
    if n in ("/DeviceGray", "/G", "/DeviceGray "):
        return "gray"
    if n in ("/DeviceRGB", "/RGB", "/DeviceRGB "):
        return "rgb"
    if n in ("/DeviceCMYK", "/CMYK"):
        return "cmyk"
    if n == "/Pattern":
        return "pattern"
    try:
        import pikepdf
        csd = resources.get("/ColorSpace") if resources is not None else None
        if csd is None or name not in csd:
            return "other"
        cs = csd[name]
        if isinstance(cs, pikepdf.Array) and len(cs):
            t = str(cs[0])
            if t in ("/Separation", "/DeviceN"):
                return "spot"
            if t == "/Indexed":
                return "indexed"
            if t == "/ICCBased":
                try:
                    return "gray" if int(cs[1].get("/N", 3)) == 1 else "icc"
                except Exception:
                    return "icc"
            if t == "/Pattern":
                return "pattern"
            if t in ("/DeviceGray", "/CalGray"):
                return "gray"
            if t in ("/DeviceRGB", "/CalRGB", "/Lab"):
                return "rgb"
            if t == "/DeviceCMYK":
                return "cmyk"
    except Exception:
        return "other"
    return "other"


def _nums(ops, n):
    try:
        return [_unit(ops[i]) for i in range(n)]
    except Exception:
        return None


def _classify_nums(nums, kind):
    if kind == "gray" and len(nums) >= 1:
        return classify_gray(nums[0])
    if kind == "rgb" and len(nums) >= 3:
        return classify_rgb(nums[0], nums[1], nums[2])
    if kind == "cmyk" and len(nums) >= 4:
        c, m, y, k = nums[:4]
        r = (1 - c) * (1 - k)
        g = (1 - m) * (1 - k)
        b = (1 - y) * (1 - k)
        return classify_rgb(r, g, b)
    return None


def _walk_content(pdf, owner, resources, page_no, out, seen, depth=0):
    """Collect vector color uses and risky constructs from one content stream."""
    if depth > 12 or owner is None:
        return
    try:
        import pikepdf
        instructions = pikepdf.parse_content_stream(owner)
    except Exception as exc:
        out["blockers"].append(_blocker(page_no, "stream",
            "content stream could not be parsed (%s) p%d" % (exc, page_no)))
        return
    # Color operators only set state. A color counts when a path or visible text is painted.
    state = {"fill_cs": "gray", "stroke_cs": "gray", "fill": "black", "stroke": "black", "tr": 0}
    stack = []

    def paint(which):
        cls = state.get(which)
        if cls:
            out["vectors"].append(cls)

    def note_text():
        # Span color comes from get_text('dict'). Render mode 3 is not a painted color.
        mode = int(state.get("tr") or 0)
        if mode == 3:
            out["invisible_text"] = out.get("invisible_text", 0) + 1

    for ins in instructions:
        if isinstance(ins, pikepdf.ContentStreamInlineImage):
            _inline_image(ins, page_no, out)
            continue
        op = str(ins.operator)
        ops = list(ins.operands)
        if op == "q":
            stack.append(dict(state))
        elif op == "Q" and stack:
            state = stack.pop()
        elif op in ("g", "G"):
            nums = _nums(ops, 1)
            if nums:
                state["fill" if op == "g" else "stroke"] = classify_gray(nums[0])
        elif op in ("rg", "RG"):
            nums = _nums(ops, 3)
            if nums:
                state["fill" if op == "rg" else "stroke"] = classify_rgb(*nums)
        elif op in ("k", "K"):
            nums = _nums(ops, 4)
            if nums:
                state["fill" if op == "k" else "stroke"] = _classify_nums(nums, "cmyk")
        elif op == "cs" and ops:
            state["fill_cs"] = _cs_kind(resources, ops[0])
            if state["fill_cs"] in _SPOT or state["fill_cs"] == "spot":
                _flag_cs(state["fill_cs"], page_no, out)
        elif op == "CS" and ops:
            state["stroke_cs"] = _cs_kind(resources, ops[0])
            if state["stroke_cs"] in _SPOT or state["stroke_cs"] == "spot":
                _flag_cs(state["stroke_cs"], page_no, out)
        elif op in ("sc", "scn", "SC", "SCN"):
            which = "fill" if op in ("sc", "scn") else "stroke"
            kind = state[which + "_cs"]
            if kind in ("spot", "indexed", "pattern", "icc", "other"):
                _flag_cs(kind, page_no, out)
            else:
                try:
                    nums = [_unit(v) for v in ops]
                except Exception:
                    nums = []
                got = _classify_nums(nums, kind)
                if got:
                    state[which] = got
                elif kind == "pattern":
                    _flag_cs("pattern", page_no, out)
        elif op == "sh":
            out["blockers"].append(_blocker(page_no, "shading",
                "shading pattern p%d" % page_no))
        elif op == "gs" and ops:
            _extgstate(resources, ops[0], page_no, out)
        elif op == "Do" and ops and depth < 12:
            _do_xobject(pdf, resources, ops[0], page_no, out, seen, depth)
        elif op == "Tr" and ops:
            try:
                state["tr"] = int(ops[0])
            except Exception:
                pass
        elif op in ("S", "s"):
            paint("stroke")
        elif op in ("f", "F", "f*"):
            paint("fill")
        elif op in ("B", "B*", "b", "b*"):
            paint("fill")
            paint("stroke")
        elif op in ("Tj", "TJ", "'", '"'):
            note_text()


def _flag_cs(kind, page_no, out):
    label = {
        "spot": "spot color",
        "indexed": "indexed color",
        "pattern": "tiling pattern",
        "icc": "ICC color",
        "other": "unknown colorspace",
    }.get(kind, kind)
    text = "%s p%d" % (label, page_no)
    if not any(b["text"] == text for b in out["blockers"]):
        out["blockers"].append(_blocker(page_no, "spot" if kind == "spot" else kind, text))


def _extgstate(resources, name, page_no, out):
    try:
        gs = resources.get("/ExtGState") if resources is not None else None
        if gs is None or name not in gs:
            return
        spec = gs[name]
        ca = float(spec.get("/ca", 1))
        CA = float(spec.get("/CA", 1))
        if ca < 0.999 or CA < 0.999:
            text = "transparency group p%d" % page_no
            if not any(b["text"] == text for b in out["blockers"]):
                out["blockers"].append(_blocker(page_no, "transparency", text))
        sm = spec.get("/SMask")
        if sm is not None and str(sm) not in ("/None", "None"):
            text = "soft mask p%d" % page_no
            if not any(b["text"] == text for b in out["blockers"]):
                out["blockers"].append(_blocker(page_no, "transparency", text))
        bm = str(spec.get("/BM", "/Normal"))
        if bm not in ("/Normal",):
            text = "blend mode %s p%d" % (bm, page_no)
            if not any(b["text"] == text for b in out["blockers"]):
                out["blockers"].append(_blocker(page_no, "transparency", text))
    except Exception:
        return


def _do_xobject(pdf, resources, name, page_no, out, seen, depth):
    try:
        import pikepdf
        xos = resources.get("/XObject") if resources is not None else None
        if xos is None or name not in xos:
            return
        xo = xos[name]
        if not isinstance(xo, pikepdf.Stream):
            return
        if xo.objgen in seen:
            return
        seen.add(xo.objgen)
        if str(xo.get("/Subtype")) == "/Form":
            fres = xo.get("/Resources", resources)
            _walk_content(pdf, xo, fres, page_no, out, seen, depth + 1)
    except Exception:
        return


def _inline_image(ins, page_no, out):
    try:
        d = ins.iimage.obj
        bpc = int(d.get("/BitsPerComponent", 8) or 8)
        cs = str(d.get("/ColorSpace", "/DeviceGray"))
    except Exception:
        out["blockers"].append(_blocker(page_no, "image",
            "inline image could not be read p%d" % page_no))
        return
    if bpc <= 1 and "RGB" not in cs and "CMYK" not in cs:
        out["images_inline"] += 1
        return
    if any(k in cs for k in ("RGB", "CMYK", "Separation", "DeviceN")):
        out["blockers"].append(_blocker(page_no, "image",
            "color inline image p%d" % page_no))
    else:
        out["gray_bpp"] = max(out["gray_bpp"], bpp_for_levels(1 << min(bpc, 8)))


def _page_group(page_obj, page_no, out):
    try:
        grp = page_obj.get("/Group")
        if grp is not None and str(grp.get("/S")) == "/Transparency":
            text = "transparency group p%d" % page_no
            if not any(b["text"] == text for b in out["blockers"]):
                out["blockers"].append(_blocker(page_no, "transparency", text))
    except Exception:
        return


def _annotations(page, page_no, blockers):
    try:
        annot = page.first_annot
    except Exception:
        return
    while annot:
        try:
            typ = annot.type
            tname = typ[1] if isinstance(typ, (tuple, list)) and len(typ) > 1 else str(typ)
            colors = annot.colors or {}
            bits = []
            for part in ("stroke", "fill"):
                col = colors.get(part)
                if not col:
                    continue
                if len(col) >= 3:
                    kind = classify_rgb(col[0], col[1], col[2])
                elif len(col) == 1:
                    kind = classify_gray(col[0])
                else:
                    kind = "color"
                if kind == "color":
                    bits.append(part)
            if bits or str(tname).lower() in ("highlight", "underline", "squiggly", "strikeout"):
                if bits or str(tname).lower() == "highlight":
                    blockers.append(_blocker(page_no, "annot",
                        "%s annotation p%d" % (tname, page_no)))
        except Exception:
            pass
        try:
            annot = annot.next
        except Exception:
            break


def _image_records(doc, page, page_no):
    import numpy as np
    import pymupdf
    info_by_xref = {}
    try:
        for info in page.get_image_info(xrefs=True):
            info_by_xref[int(info.get("xref") or 0)] = info
    except Exception:
        info_by_xref = {}
    records = []
    try:
        images = page.get_images(full=True)
    except Exception:
        images = []
    page_area = max(float(page.rect.width * page.rect.height), 1.0)
    for im in images:
        xref = int(im[0])
        smask = int(im[1] or 0)
        w, h = int(im[2] or 0), int(im[3] or 0)
        bpc = int(im[4] or 0)
        cs = str(im[5] or "")
        name = str(im[7] or "")
        filt = str(im[8] or "")
        try:
            raw = doc.xref_stream_raw(xref) or b""
        except Exception:
            raw = b""
        try:
            filt_key = doc.xref_get_key(xref, "Filter")[1] or filt
        except Exception:
            filt_key = filt
        filt_s = str(filt_key)
        info = info_by_xref.get(xref, {})
        bbox = info.get("bbox")
        ppi = None
        area_frac = 1.0
        if bbox and len(bbox) == 4 and w and h:
            bw = abs(float(bbox[2]) - float(bbox[0]))
            bh = abs(float(bbox[3]) - float(bbox[1]))
            if bw > 1 and bh > 1:
                ppi = round(w / (bw / 72.0), 1)
                area_frac = (bw * bh) / page_area
        rec = {
            "xref": xref, "name": name, "width": w, "height": h, "bpc": bpc,
            "colorspace": cs, "filter": filt_s, "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest() if raw else "",
            "ppi": ppi, "area_frac": round(area_frac, 4), "smask": smask,
            "pages": [page_no], "dup_of": None, "kind": "gray",
            "levels": None, "photo": False, "suggested_bpp": None,
            "est_bytes": len(raw), "copier_ccitt": "CCITT" in filt_s.upper(),
        }
        if rec["copier_ccitt"] or (bpc <= 1 and "RGB" not in cs.upper() and "CMYK" not in cs.upper()):
            rec.update(kind="bilevel", levels=2, photo=False, suggested_bpp=1,
                       est_bytes=len(raw) or max(1, (w * h) // 8))
            records.append(rec)
            continue
        try:
            pix = pymupdf.Pixmap(doc, xref)
            if pix.alpha:
                pix = pymupdf.Pixmap(pix, 0)
            if pix.colorspace and pix.colorspace.n == 4:
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            n = pix.n
            arr = np.frombuffer(pix.samples, np.uint8)
            arr = arr.reshape(pix.h, pix.stride)[:, :pix.w * n].reshape(pix.h, pix.w, n)
            step = 1
            if arr.shape[0] * arr.shape[1] > SAMPLE_PIXELS:
                step = int((arr.shape[0] * arr.shape[1] / SAMPLE_PIXELS) ** 0.5) or 1
            sample = arr[::step, ::step]
            if n >= 3:
                mx = sample[..., :3].max(axis=2).astype(int)
                mn = sample[..., :3].min(axis=2).astype(int)
                chroma = (mx - mn) / 255.0
                grayish = float(np.percentile(chroma, 99)) <= CHROMA_MAX
                if grayish:
                    y = (0.299 * sample[..., 0] + 0.587 * sample[..., 1] + 0.114 * sample[..., 2])
                    levels = int(np.unique(y.astype(np.uint8)).size)
                    rec.update(kind="gray", levels=levels, suggested_bpp=bpp_for_levels(levels))
                else:
                    rec.update(kind="color", levels=None, suggested_bpp=None, photo=True)
            else:
                levels = int(np.unique(sample[..., 0]).size)
                rec.update(kind="gray", levels=levels, suggested_bpp=bpp_for_levels(levels))
            if rec["kind"] == "gray":
                rec["photo"] = (rec["levels"] or 0) > 16
                bpp = rec["suggested_bpp"] or 8
                rec["est_bytes"] = max(1, w * h * bpp // 8) if w and h else len(raw)
                if bpc and bpc <= bpp:
                    rec["est_bytes"] = len(raw) or rec["est_bytes"]
            else:
                rec["est_bytes"] = len(raw)
        except Exception as exc:
            rec.update(kind="undecodable", est_bytes=len(raw))
            rec["error"] = str(exc)[:120]
        records.append(rec)
    return records


def _span_class(color):
    """Classify a get_text('dict') span color (0xRRGGBB) or a 0-1 sequence."""
    if isinstance(color, int):
        r = (color >> 16) & 255
        g = (color >> 8) & 255
        b = color & 255
        cls = classify_rgb(r, g, b)
        bpp = 1 if cls in ("black", "white") else (8 if cls == "color" else bpp_for_gray_value(r))
        return cls, bpp, (r / 255.0, g / 255.0, b / 255.0)
    if isinstance(color, (int, float)):
        return classify_gray(color), bpp_for_gray_value(color), (color, color, color)
    if isinstance(color, (tuple, list)) and len(color) >= 3:
        cls = classify_rgb(color[0], color[1], color[2])
        bpp = 1 if cls in ("black", "white") else (8 if cls == "color" else bpp_for_gray_value(color[0]))
        return cls, bpp, tuple(color[:3])
    if isinstance(color, (tuple, list)) and len(color) == 1:
        return classify_gray(color[0]), bpp_for_gray_value(color[0]), (color[0], color[0], color[0])
    return "gray", 8, None


def _text_items(page, page_no):
    """Span colors come from get_text('dict'). Render mode 3 is noted and ignored."""
    items = []
    try:
        data = page.get_text("dict")
    except Exception:
        data = {}
    for block in (data or {}).get("blocks") or []:
        for line in block.get("lines") or []:
            for span in line.get("spans") or []:
                text = span.get("text") or ""
                cls, bpp, rgb = _span_class(span.get("color", 0))
                items.append({"text": text[:40], "render": 0, "cls": cls, "bpp": bpp, "rgb": rgb})
    try:
        trace = page.get_texttrace() or []
    except Exception:
        trace = []
    # Pair in order. Invisible/OCR runs (render mode 3) do not count as color.
    for item, block in zip(items, trace):
        render = int(block.get("type", 0) or 0)
        item["render"] = render
        if render == 3:
            item["cls"] = "ignored"
            item["note"] = "invisible"
    if len(trace) > len(items):
        for block in trace[len(items):]:
            render = int(block.get("type", 0) or 0)
            if render != 3:
                continue
            try:
                chars = block.get("chars") or []
                text = "".join(chr(c[0]) for c in chars if c and isinstance(c[0], int))
            except Exception:
                text = ""
            items.append({"text": text[:40], "render": 3, "cls": "ignored", "bpp": 1, "note": "invisible"})
    return items


def _draw_classes(page):
    classes = []
    opacities = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return classes, opacities
    for d in drawings or []:
        for key in ("color", "fill"):
            col = d.get(key)
            if not col:
                continue
            if len(col) >= 3:
                classes.append(classify_rgb(col[0], col[1], col[2]))
            elif len(col) == 1:
                classes.append(classify_gray(col[0]))
        for key in ("fill_opacity", "stroke_opacity"):
            if d.get(key) is not None and float(d[key]) < 0.999:
                opacities.append(float(d[key]))
    return classes, opacities


def _page_decision(page_no, images, texts, vectors, blockers):
    """Pick BW / GRAY / MOSTLY-BW-WITH-COLOR-ACCENTS / COLOR for one page."""
    if any(im.get("smask") for im in images):
        text = "soft mask p%d" % page_no
        if not any(b["text"] == text for b in blockers):
            blockers.append(_blocker(page_no, "transparency", text))
    risky = [b for b in blockers if b["kind"] in _RISKY or b["kind"] in ("shading", "transparency", "spot", "pattern", "indexed", "icc")]
    color_images = [im for im in images if im.get("kind") == "color"]
    large_color = [im for im in color_images if im.get("area_frac", 1) >= 0.05]
    small_color = [im for im in color_images if im.get("area_frac", 1) < 0.05]
    undecodable = [im for im in images if im.get("kind") == "undecodable"]
    gray_bpp = 1
    saw_gray = False
    for im in images:
        if im.get("kind") == "gray":
            saw_gray = True
            gray_bpp = max(gray_bpp, int(im.get("suggested_bpp") or 8))
        elif im.get("kind") == "bilevel":
            gray_bpp = max(gray_bpp, 1)
    for t in texts:
        if t["cls"] == "ignored":
            continue
        if t["cls"] == "gray":
            saw_gray = True
            gray_bpp = max(gray_bpp, int(t["bpp"]))
        elif t["cls"] in ("black", "white"):
            gray_bpp = max(gray_bpp, 1)
    for v in vectors:
        if v == "gray":
            saw_gray = True
            gray_bpp = max(gray_bpp, 8)
        elif v in ("black", "white"):
            gray_bpp = max(gray_bpp, 1)
    color_text = [t for t in texts if t["cls"] == "color"]
    color_vec = [v for v in vectors if v == "color"]
    has_bw_body = (
        any(t["cls"] in ("black", "white", "gray") for t in texts if t["cls"] != "ignored")
        or any(im.get("kind") in ("bilevel", "gray") for im in images)
        or any(v in ("black", "white", "gray") for v in vectors))
    if risky or undecodable or large_color or (color_vec and not has_bw_body and not color_text and not small_color):
        label, target = "COLOR", "color"
    elif color_text or small_color or color_vec:
        label, target = "MOSTLY-BW-WITH-COLOR-ACCENTS", "color"
    elif saw_gray and gray_bpp > 1:
        label, target = "GRAY", "gray-%d" % gray_bpp
    else:
        label, target = "BW", "bw-1"
        gray_bpp = 1
    # Color text/vector still listed even when the label is already COLOR.
    for t in color_text:
        snippet = (t.get("text") or "").replace("\n", " ")[:24]
        blockers.append(_blocker(page_no, "text",
            'red text span "%s" p%d' % (snippet, page_no) if _is_red(t) else
            'color text span "%s" p%d' % (snippet, page_no)))
    for im in color_images:
        blockers.append(_blocker(page_no, "image",
            "color image xref %d p%d" % (im["xref"], page_no)))
    for im in undecodable:
        blockers.append(_blocker(page_no, "image",
            "undecodable image xref %d p%d" % (im["xref"], page_no)))
    if color_vec and not any(b["kind"] == "vector" for b in blockers):
        blockers.append(_blocker(page_no, "vector", "color drawing p%d" % page_no))
    parts = []
    if images:
        bits = []
        for im in images:
            if im.get("dup_of"):
                bits.append("dup of xref %d" % im["dup_of"])
            elif im.get("copier_ccitt"):
                bits.append("CCITT 1-bit %dx%d" % (im["width"], im["height"]))
            elif im.get("kind") == "bilevel":
                bits.append("1-bit %dx%d" % (im["width"], im["height"]))
            elif im.get("kind") == "gray":
                bits.append("gray %d-level -> %d bpp" % (im.get("levels") or 0, im.get("suggested_bpp") or 8))
            elif im.get("kind") == "color":
                bits.append("color %dx%d" % (im["width"], im["height"]))
            else:
                bits.append(im.get("kind") or "image")
        parts.append(", ".join(bits))
    else:
        parts.append("no images")
    return {
        "page": page_no,
        "label": label,
        "target": target,
        "suggested_bpp": None if target == "color" else gray_bpp,
        "image_summary": "; ".join(parts),
        "images": images,
        "text": texts,
        "blockers": blockers,
        "blocker_count": len(blockers),
    }


def _is_red(item):
    # Used only for the example wording when the span is a saturated red.
    return False


def _mark_red_text(texts, page):
    """Saturated red spans use the 'red text span' wording."""
    for item in texts:
        rgb = item.get("rgb")
        if not rgb or len(rgb) < 3:
            continue
        r, g, b = _unit(rgb[0]), _unit(rgb[1]), _unit(rgb[2])
        item["red"] = r > 0.5 and g < 0.35 and b < 0.35 and (r - max(g, b)) > 0.3


def _page_decision_fixed(page_no, images, texts, vectors, blockers):
    page = _page_decision(page_no, images, texts, vectors, blockers)
    # Rewrite text blockers so saturated red uses the requested wording.
    rewritten = []
    seen = set()
    color_i = 0
    color_texts = [t for t in texts if t["cls"] == "color"]
    for b in page["blockers"]:
        if b["kind"] == "text" and color_i < len(color_texts):
            t = color_texts[color_i]
            color_i += 1
            snippet = (t.get("text") or "").replace("\n", " ")[:24]
            if t.get("red"):
                b = _blocker(page_no, "text", 'red text span "%s" p%d' % (snippet, page_no))
            else:
                b = _blocker(page_no, "text", 'color text span "%s" p%d' % (snippet, page_no))
        if b["text"] in seen:
            continue
        seen.add(b["text"])
        rewritten.append(b)
    page["blockers"] = rewritten
    page["blocker_count"] = len(rewritten)
    return page


def _document(pages, image_table):
    labels = [p["label"] for p in pages] or ["BW"]
    if "COLOR" in labels:
        label = "COLOR"
    elif "MOSTLY-BW-WITH-COLOR-ACCENTS" in labels:
        label = "MOSTLY-BW-WITH-COLOR-ACCENTS"
    elif "GRAY" in labels:
        label = "GRAY"
    else:
        label = "BW"
    if label == "BW":
        target, bpp = "bw-1", 1
    elif label == "GRAY":
        bpp = max([p["suggested_bpp"] or 1 for p in pages] or [1])
        target = "gray-%d" % bpp
    else:
        target, bpp = "color", None
    blockers = []
    seen = set()
    for p in pages:
        for b in p["blockers"]:
            if b["text"] not in seen:
                seen.add(b["text"])
                blockers.append(b)
    dups = sum(1 for im in image_table if im.get("dup_of"))
    est = 0
    counted = set()
    for im in image_table:
        key = im.get("dup_of") or im["xref"]
        if key in counted:
            continue
        counted.add(key)
        est += int(im.get("est_bytes") or 0)
    return {
        "pages": len(pages),
        "label": label,
        "target": target,
        "suggested_bpp": bpp,
        "blockers": blockers,
        "blocker_count": len(blockers),
        "images": len(image_table),
        "duplicates": dups,
        "est_bytes": est,
    }


def analyze_file(path, cancel=None):
    """Analyze path. cancel is an optional threading.Event checked between pages."""
    import pikepdf
    import pymupdf
    doc = pymupdf.open(path)
    pdf = pikepdf.open(path)
    image_table = []
    seen_hash = {}
    pages = []
    try:
        for index, page in enumerate(doc):
            if cancel is not None and cancel.is_set():
                return None
            page_no = index + 1
            images = _image_records(doc, page, page_no)
            owned = []
            for im in images:
                previous = seen_hash.get(im["xref"])
                if previous is not None:
                    if page_no not in previous["pages"]:
                        previous["pages"].append(page_no)
                    owned.append(previous)
                    continue
                prev_hash = seen_hash.get("sha:" + im["sha256"]) if im["sha256"] else None
                if prev_hash and prev_hash != im["xref"]:
                    im["dup_of"] = prev_hash
                    im["est_bytes"] = 0
                elif im["sha256"]:
                    seen_hash["sha:" + im["sha256"]] = im["xref"]
                seen_hash[im["xref"]] = im
                image_table.append(im)
                owned.append(im)
            images = owned
            texts = _text_items(page, page_no)
            _mark_red_text(texts, page)
            vectors, opacities = _draw_classes(page)
            stream = {"blockers": [], "vectors": [], "images_inline": 0, "gray_bpp": 1}
            try:
                pk_page = pdf.pages[index]
                res = pk_page.get("/Resources", None)
                if "/Group" in pk_page.obj:
                    _page_group(pk_page.obj, page_no, stream)
                _walk_content(pdf, pk_page.obj, res if res is not None else pk_page.resources, page_no, stream, set())
            except Exception as exc:
                stream["blockers"].append(_blocker(page_no, "stream",
                    "content stream could not be parsed (%s) p%d" % (exc, page_no)))
            if opacities:
                text = "transparency group p%d" % page_no
                if not any(b["text"] == text for b in stream["blockers"]):
                    stream["blockers"].append(_blocker(page_no, "transparency", text))
            _annotations(page, page_no, stream["blockers"])
            pages.append(_page_decision_fixed(page_no, images, texts, vectors + [v for v in stream["vectors"] if v], stream["blockers"]))
    finally:
        doc.close()
        pdf.close()
    summary = _document(pages, image_table)
    return {"summary": summary, "pages": pages, "images": image_table}


def analyze_lines(data):
    """Short ANALYZE log lines, without a timestamp."""
    if not data:
        return []
    s = data["summary"]
    lines = ["document %s target %s pages %d images %d duplicates %d blockers %d" % (
        s["label"], s["target"], s["pages"], s["images"], s["duplicates"], s["blocker_count"])]
    for p in data["pages"]:
        lines.append("p%d %s target %s %s blockers %d" % (
            p["page"], p["label"], p["target"], p["image_summary"], p["blocker_count"]))
        for b in p["blockers"]:
            lines.append("blocker %s" % b["text"])
    return lines


def format_report(data):
    """Human summary for --analyze-only."""
    if not data:
        return "analysis cancelled"
    s = data["summary"]
    bpp = "" if s["suggested_bpp"] is None else "  bpp %s" % s["suggested_bpp"]
    lines = ["document: %s  target %s%s  pages %d  images %d (%d duplicates)  est %d B" % (
        s["label"], s["target"], bpp, s["pages"], s["images"], s["duplicates"], s["est_bytes"])]
    if s["blockers"]:
        lines.append("blockers:")
        for b in s["blockers"]:
            lines.append("  - %s" % b["text"])
    lines.append("")
    for p in data["pages"]:
        pb = "" if p["suggested_bpp"] is None else "  bpp %s" % p["suggested_bpp"]
        lines.append("page %d: %s  target %s%s  %s  blockers %d" % (
            p["page"], p["label"], p["target"], pb, p["image_summary"], p["blocker_count"]))
        for b in p["blockers"]:
            lines.append("  - %s" % b["text"])
    return "\n".join(lines)
