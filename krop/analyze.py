# -*- coding: utf-8 -*-
"""Read-only PDF analysis: smallest safe color and bit depth.

Samples images instead of inspecting every pixel. Shadings, patterns,
transparency, spot colors and ICC spaces are not treated as color until a
tint transform or a modest render shows chromatic pixels. A neutral render
stays BW or GRAY, with the construct kept as a warning.
"""
import hashlib

# Near-gray if max-min channel is under this fraction of full scale.
CHROMA_MAX = 0.03
# Near-black / near-white snap, as a fraction of full luminance.
BLACK_MAX = 0.10
WHITE_MIN = 0.90
# Image sample budget. Large scans are strided down to about this many pixels.
SAMPLE_PIXELS = 200000


# Modest render used only when a construct could hide color. 72 dpi is enough
# to tell neutral pages from colored ones and is much cheaper than 120 on a
# page whose transparency group forces a full raster. Longest edge is capped.
RENDER_DPI = 72
RENDER_MAX_EDGE = 900

_PIP_NAMES = {
    "PIL": "Pillow",
    "cv2": "opencv-python",
}


class MissingDependency(Exception):
    """A package required for analysis is not installed."""

    def __init__(self, packages):
        self.packages = list(packages)
        names = ", ".join(self.packages)
        super(MissingDependency, self).__init__(
            "Missing package: %s. Install it with: pip install %s" % (names, " ".join(self.packages)))


def require_analysis_deps():
    """Import the libraries analysis needs, or raise MissingDependency."""
    missing = []
    for mod, pip_name in (("pikepdf", "pikepdf"), ("numpy", "numpy"), ("pymupdf", "pymupdf")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pip_name)
    if missing:
        raise MissingDependency(missing)


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


def _blocker(kind, text, severity):
    return {"page": 0, "kind": kind, "text": text, "severity": severity}


def _empty_contrib():
    return {
        "blockers": [],
        "color_vectors": 0,
        "gray_vectors": 0,
        "bw_vectors": 0,
        "gray_bpp": 1,
        "gray_levels": set(),
        "gray_notes": [],
        "tr3": 0,
        "placements": [],
        "_seen_blockers": set(),
    }


def _add_blocker(out, kind, text, severity):
    if text in out["_seen_blockers"]:
        return
    out["_seen_blockers"].add(text)
    out["blockers"].append(_blocker(kind, text, severity))


def _merge(dst, src):
    dst["color_vectors"] += src["color_vectors"]
    dst["gray_vectors"] += src["gray_vectors"]
    dst["bw_vectors"] += src["bw_vectors"]
    dst["tr3"] += src["tr3"]
    if src["gray_bpp"] > dst["gray_bpp"]:
        dst["gray_bpp"] = src["gray_bpp"]
    levels = dst["gray_levels"]
    for g in src["gray_levels"]:
        if len(levels) >= 24:
            break
        levels.add(g)
    for note in src["gray_notes"]:
        if len(dst["gray_notes"]) >= 6:
            break
        if note not in dst["gray_notes"]:
            dst["gray_notes"].append(note)
    dst["placements"].extend(src["placements"])
    for b in src["blockers"]:
        _add_blocker(dst, b["kind"], b["text"], b.get("severity") or "check")


def _state_from_gray(g, detail=""):
    cls = classify_gray(g)
    bpp = 1 if cls in ("black", "white") else bpp_for_gray_value(g)
    value = _unit(g)
    if cls == "gray" and not detail:
        detail = "gray %.2f" % value
    return {"cls": cls, "bpp": bpp, "value": value, "value_q": int(round(value * 255)), "detail": detail}


def _state_from_rgb(r, g, b, detail=""):
    cls = classify_rgb(r, g, b)
    if cls == "color":
        return {"cls": "color", "bpp": 8, "detail": detail or "color", "value_q": None}
    y = _luma(_unit(r), _unit(g), _unit(b))
    return _state_from_gray(y, detail)


def _state_from_cmyk(nums, detail=""):
    c, m, y, k = (_unit(nums[0]), _unit(nums[1]), _unit(nums[2]), _unit(nums[3]))
    r = (1 - c) * (1 - k)
    g = (1 - m) * (1 - k)
    b = (1 - y) * (1 - k)
    return _state_from_rgb(r, g, b, detail)


def _paint_color(out, col):
    if not col or col.get("unknown") or not col.get("cls"):
        kind = (col or {}).get("kind") or "other"
        detail = (col or {}).get("detail") or "unknown colorspace"
        _add_blocker(out, kind, "%s p{p}" % detail, "check")
        return
    cls = col["cls"]
    detail = col.get("detail") or ""
    if cls == "color":
        out["color_vectors"] += 1
        if detail.startswith("spot"):
            _add_blocker(out, "spot", "spot color p{p}", "color")
        elif detail.startswith("ICC"):
            _add_blocker(out, "icc", "ICC color p{p}", "color")
        return
    if cls == "gray":
        out["gray_vectors"] += 1
        bpp = int(col.get("bpp") or 8)
        if bpp > out["gray_bpp"]:
            out["gray_bpp"] = bpp
        q = col.get("value_q")
        if q is not None and len(out["gray_levels"]) < 24:
            out["gray_levels"].add(int(q))
        if detail and len(out["gray_notes"]) < 6 and detail not in out["gray_notes"]:
            out["gray_notes"].append(detail)
        if detail.startswith("spot"):
            _add_blocker(out, "spot", "%s p{p}" % detail, "warning")
        elif detail.startswith("ICC"):
            _add_blocker(out, "icc", "%s p{p}" % detail, "warning")
        return
    out["bw_vectors"] += 1
    if detail.startswith("spot"):
        _add_blocker(out, "spot", "%s p{p}" % detail, "warning")
    elif detail.startswith("ICC"):
        _add_blocker(out, "icc", "%s p{p}" % detail, "warning")


def _lookup_cs(resources, name):
    n = str(name)
    if n in ("/DeviceGray", "/G"):
        return {"mode": "gray"}
    if n in ("/DeviceRGB", "/RGB"):
        return {"mode": "rgb"}
    if n in ("/DeviceCMYK", "/CMYK"):
        return {"mode": "cmyk"}
    if n == "/Pattern":
        return {"mode": "pattern", "detail": "tiling pattern"}
    try:
        import pikepdf
        csd = resources.get("/ColorSpace") if resources is not None else None
        if csd is None or name not in csd:
            return {"mode": "other", "detail": "unknown colorspace"}
        cs = csd[name]
        if isinstance(cs, pikepdf.Array) and len(cs):
            t = str(cs[0])
            if t in ("/Separation", "/DeviceN"):
                return {"mode": "spot", "obj": cs, "detail": "spot color"}
            if t == "/Indexed":
                return {"mode": "indexed", "obj": cs, "detail": "indexed color"}
            if t == "/ICCBased":
                try:
                    ncomp = int(cs[1].get("/N", 3))
                except Exception:
                    ncomp = 3
                detail = "ICC gray" if ncomp == 1 else "ICC color"
                return {"mode": "icc", "obj": cs, "detail": detail}
            if t == "/Pattern":
                return {"mode": "pattern", "detail": "tiling pattern"}
            if t in ("/DeviceGray", "/CalGray"):
                return {"mode": "gray"}
            if t in ("/DeviceRGB", "/CalRGB", "/Lab"):
                return {"mode": "rgb"}
            if t == "/DeviceCMYK":
                return {"mode": "cmyk"}
    except Exception:
        return {"mode": "other", "detail": "unknown colorspace"}
    return {"mode": "other", "detail": "unknown colorspace"}


def _alt_kind(alt):
    s = str(alt)
    if s in ("/DeviceGray", "/CalGray"):
        return "gray"
    if s in ("/DeviceRGB", "/CalRGB", "/Lab"):
        return "rgb"
    if s == "/DeviceCMYK":
        return "cmyk"
    try:
        import pikepdf
        if isinstance(alt, pikepdf.Array) and len(alt):
            t = str(alt[0])
            if t == "/ICCBased":
                n = int(alt[1].get("/N", 3))
                if n == 1:
                    return "gray"
                if n == 4:
                    return "cmyk"
                return "rgb"
            if t in ("/DeviceGray", "/CalGray"):
                return "gray"
            if t in ("/DeviceRGB", "/CalRGB", "/Lab"):
                return "rgb"
            if t == "/DeviceCMYK":
                return "cmyk"
    except Exception:
        pass
    return "other"


def _eval_fn(fn, inputs):
    """Evaluate a type 2 or 3 PDF function. Other types are left unresolved."""
    if fn is None or not inputs:
        return None
    try:
        ft = int(fn.get("/FunctionType", -1))
        if ft == 2:
            nexp = float(fn.get("/N", 1))
            c0 = [float(v) for v in fn["/C0"]]
            c1 = [float(v) for v in fn["/C1"]]
            x = max(0.0, min(1.0, float(inputs[0])))
            t = x ** nexp
            return [a + (b - a) * t for a, b in zip(c0, c1)]
        if ft == 3:
            bounds = [float(v) for v in fn.get("/Bounds", [])]
            funcs = list(fn["/Functions"])
            encode = [float(v) for v in fn.get("/Encode", [])]
            x = float(inputs[0])
            k = len(bounds)
            for i, b in enumerate(bounds):
                if x < b:
                    k = i
                    break
            lo = 0.0 if k == 0 else bounds[k - 1]
            hi = 1.0 if k >= len(bounds) else bounds[k]
            e0 = encode[2 * k] if 2 * k < len(encode) else 0.0
            e1 = encode[2 * k + 1] if 2 * k + 1 < len(encode) else 1.0
            span = (hi - lo) or 1.0
            local = e0 + (e1 - e0) * ((x - lo) / span)
            if k >= len(funcs):
                return None
            return _eval_fn(funcs[k], [local])
    except Exception:
        return None
    return None


def _spot_state(cs, tints):
    try:
        kind0 = str(cs[0])
        if kind0 == "/Separation":
            spot_name = str(cs[1]).lstrip("/")
            alt, fn = cs[2], cs[3]
        elif kind0 == "/DeviceN":
            spot_name = "DeviceN"
            alt, fn = cs[2], cs[3]
        else:
            return {"unknown": True, "kind": "spot", "detail": "spot color"}
    except Exception:
        return {"unknown": True, "kind": "spot", "detail": "spot color"}
    values = _eval_fn(fn, tints)
    if not values:
        return {"unknown": True, "kind": "spot", "detail": "spot color"}
    mode = _alt_kind(alt)
    if mode == "gray":
        st = _state_from_gray(values[0])
        st["detail"] = "spot %s maps to %s" % (spot_name, st["cls"])
        return st
    if mode == "rgb" and len(values) >= 3:
        st = _state_from_rgb(*values[:3])
        st["detail"] = "spot color" if st["cls"] == "color" else "spot %s maps to %s" % (spot_name, st["cls"])
        return st
    if mode == "cmyk" and len(values) >= 4:
        st = _state_from_cmyk(values[:4])
        st["detail"] = "spot color" if st["cls"] == "color" else "spot %s maps to %s" % (spot_name, st["cls"])
        return st
    return {"unknown": True, "kind": "spot", "detail": "spot color"}


def _icc_state(cs, nums):
    try:
        n = int(cs[1].get("/N", 3))
    except Exception:
        n = 3
    if n == 1 and nums:
        st = _state_from_gray(nums[0])
        st["detail"] = "ICC gray" if st["cls"] == "gray" else "ICC gray maps to %s" % st["cls"]
        return st
    if n == 3 and len(nums) >= 3:
        st = _state_from_rgb(*nums[:3])
        if st["cls"] == "color":
            st["detail"] = "ICC color"
        elif st["cls"] == "gray":
            st["detail"] = "ICC neutral"
        else:
            st["detail"] = "ICC gray maps to %s" % st["cls"]
        return st
    if n == 4 and len(nums) >= 4:
        st = _state_from_cmyk(nums[:4])
        st["detail"] = "ICC color" if st["cls"] == "color" else "ICC neutral"
        return st
    return {"unknown": True, "kind": "icc", "detail": "ICC color"}


def _numeric_ops(ops):
    nums = []
    for v in ops:
        if hasattr(v, "name"):
            continue
        try:
            nums.append(_unit(v))
        except Exception:
            continue
    return nums


def _apply_components(mode, nums, detail=""):
    if mode == "gray" and nums:
        return _state_from_gray(nums[0], detail)
    if mode == "rgb" and len(nums) >= 3:
        return _state_from_rgb(nums[0], nums[1], nums[2], detail)
    if mode == "cmyk" and len(nums) >= 4:
        return _state_from_cmyk(nums, detail)
    return None


_IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mat_mul(m, ctm):
    a, b, c, d, e, f = (float(x) for x in m)
    A, B, C, D, E, F = ctm
    return (
        a * A + b * C, a * B + b * D,
        c * A + d * C, c * B + d * D,
        e * A + f * C + E, e * B + f * D + F,
    )


def _ctm_size(ctm):
    a, b, c, d, _e, _f = ctm
    return (a * a + b * b) ** 0.5, (c * c + d * d) ** 0.5


def _new_state():
    black = {"cls": "black", "bpp": 1, "value": 0.0, "value_q": 0, "detail": ""}
    return {
        "fill_mode": "gray", "stroke_mode": "gray",
        "fill_obj": None, "stroke_obj": None,
        "fill": black, "stroke": dict(black),
        "fill_pending": False, "stroke_pending": False,
        "tr": 0, "ctm": _IDENT,
    }


def _instructions(owner, cache):
    import pikepdf
    key = getattr(owner, "objgen", None)
    if key is None:
        return list(pikepdf.parse_content_stream(owner))
    found = cache["instr"].get(key)
    if found is None:
        found = list(pikepdf.parse_content_stream(owner))
        cache["instr"][key] = found
    return found


def _set_cs(state, which, info):
    state[which + "_mode"] = info.get("mode") or "other"
    state[which + "_obj"] = info.get("obj")
    mode = state[which + "_mode"]
    if mode in ("spot", "icc", "pattern", "indexed", "other"):
        state[which] = {
            "unknown": True, "kind": mode,
            "detail": info.get("detail") or mode,
        }
        state[which + "_pending"] = True


def _set_special(state, which, ops):
    mode = state.get(which + "_mode")
    obj = state.get(which + "_obj")
    nums = _numeric_ops(ops)
    if mode == "spot" and obj is not None and nums:
        state[which] = _spot_state(obj, nums)
    elif mode == "icc" and obj is not None and nums:
        state[which] = _icc_state(obj, nums)
    elif mode == "pattern":
        state[which] = {"unknown": True, "kind": "pattern", "detail": "tiling pattern"}
    elif mode in ("gray", "rgb", "cmyk"):
        got = _apply_components(mode, nums)
        if got:
            state[which] = got
    elif mode in ("spot", "icc", "indexed", "other"):
        state[which] = {"unknown": True, "kind": mode or "other", "detail": (state[which] or {}).get("detail") or mode}
    state[which + "_pending"] = True


def _walk_content(pdf, owner, resources, out, seen, depth, cache):
    """Collect painted colors and risky constructs. Parsed streams are cached."""
    if depth > 12 or owner is None:
        return
    try:
        import pikepdf
        instructions = _instructions(owner, cache)
    except Exception as exc:
        _add_blocker(out, "stream", "content stream could not be parsed (%s) p{p}" % exc, "check")
        return
    state = _new_state()
    stack = []
    for ins in instructions:
        if isinstance(ins, pikepdf.ContentStreamInlineImage):
            _inline_image(ins, out)
            continue
        op = str(ins.operator)
        ops = list(ins.operands)
        if op == "q":
            stack.append(dict(state))
        elif op == "Q" and stack:
            state = stack.pop()
        elif op == "cm" and len(ops) >= 6:
            try:
                state["ctm"] = _mat_mul(ops[:6], state["ctm"])
            except Exception:
                pass
        elif op in ("g", "G"):
            which = "fill" if op == "g" else "stroke"
            nums = _numeric_ops(ops)
            if nums:
                state[which] = _state_from_gray(nums[0])
                state[which + "_mode"] = "gray"
                state[which + "_pending"] = True
        elif op in ("rg", "RG"):
            which = "fill" if op == "rg" else "stroke"
            nums = _numeric_ops(ops)
            if len(nums) >= 3:
                state[which] = _state_from_rgb(nums[0], nums[1], nums[2])
                state[which + "_mode"] = "rgb"
                state[which + "_pending"] = True
        elif op in ("k", "K"):
            which = "fill" if op == "k" else "stroke"
            nums = _numeric_ops(ops)
            if len(nums) >= 4:
                state[which] = _state_from_cmyk(nums)
                state[which + "_mode"] = "cmyk"
                state[which + "_pending"] = True
        elif op == "cs" and ops:
            _set_cs(state, "fill", _lookup_cs(resources, ops[0]))
        elif op == "CS" and ops:
            _set_cs(state, "stroke", _lookup_cs(resources, ops[0]))
        elif op in ("sc", "scn"):
            _set_special(state, "fill", ops)
        elif op in ("SC", "SCN"):
            _set_special(state, "stroke", ops)
        elif op == "sh":
            _add_blocker(out, "shading", "shading pattern p{p}", "check")
        elif op == "gs" and ops:
            _extgstate(resources, ops[0], out)
        elif op == "Do" and ops and depth < 12:
            _do_xobject(pdf, resources, ops[0], state["ctm"], out, seen, depth, cache)
        elif op == "Tr" and ops:
            try:
                state["tr"] = int(ops[0])
            except Exception:
                pass
        elif op in ("S", "s"):
            _paint_color(out, state["stroke"])
            state["stroke_pending"] = False
        elif op in ("f", "F", "f*"):
            _paint_color(out, state["fill"])
            state["fill_pending"] = False
        elif op in ("B", "B*", "b", "b*"):
            _paint_color(out, state["fill"])
            _paint_color(out, state["stroke"])
            state["fill_pending"] = False
            state["stroke_pending"] = False
        elif op in ("Tj", "TJ", "'", '"'):
            if int(state.get("tr") or 0) == 3:
                out["tr3"] += 1
    if state.get("fill_pending"):
        _paint_color(out, state["fill"])
    if state.get("stroke_pending"):
        _paint_color(out, state["stroke"])


def _do_xobject(pdf, resources, name, ctm, out, seen, depth, cache):
    try:
        import pikepdf
        xos = resources.get("/XObject") if resources is not None else None
        if xos is None or name not in xos:
            return
        xo = xos[name]
        if not isinstance(xo, pikepdf.Stream):
            return
        key = xo.objgen
        subtype = str(xo.get("/Subtype"))
        if subtype == "/Image":
            w_pt, h_pt = _ctm_size(ctm)
            out["placements"].append({"name": str(name).lstrip("/"), "w_pt": w_pt, "h_pt": h_pt})
            return
        if subtype != "/Form":
            return
        if key in seen:
            return
        cached = cache["forms"].get(key)
        if cached is None:
            local = _empty_contrib()
            seen2 = set(seen)
            seen2.add(key)
            fres = xo.get("/Resources", None)
            _walk_content(pdf, xo, fres if fres is not None else resources, local, seen2, depth + 1, cache)
            cache["forms"][key] = local
            cached = local
        _merge(out, cached)
    except Exception:
        return


def _inline_image(ins, out):
    try:
        d = ins.iimage.obj
        bpc = int(d.get("/BitsPerComponent", 8) or 8)
        cs = str(d.get("/ColorSpace", "/DeviceGray"))
    except Exception:
        _add_blocker(out, "image", "inline image could not be read p{p}", "check")
        return
    if bpc <= 1 and "RGB" not in cs and "CMYK" not in cs:
        out["bw_vectors"] += 1
        return
    if any(k in cs for k in ("RGB", "CMYK", "Separation", "DeviceN")):
        _add_blocker(out, "image", "color inline image p{p}", "color")
        out["color_vectors"] += 1
    else:
        out["gray_vectors"] += 1
        bpp = bpp_for_levels(1 << min(bpc, 8))
        if bpp > out["gray_bpp"]:
            out["gray_bpp"] = bpp
        if len(out["gray_notes"]) < 6:
            note = "inline gray image"
            if note not in out["gray_notes"]:
                out["gray_notes"].append(note)


def _extgstate(resources, name, out):
    try:
        gs = resources.get("/ExtGState") if resources is not None else None
        if gs is None or name not in gs:
            return
        spec = gs[name]
        ca = float(spec.get("/ca", 1))
        CA = float(spec.get("/CA", 1))
        if ca < 0.999 or CA < 0.999:
            _add_blocker(out, "transparency", "transparency group p{p}", "check")
        sm = spec.get("/SMask")
        if sm is not None and str(sm) not in ("/None", "None"):
            _add_blocker(out, "transparency", "soft mask p{p}", "check")
        bm = str(spec.get("/BM", "/Normal"))
        if bm not in ("/Normal",):
            _add_blocker(out, "transparency", "blend mode %s p{p}" % bm, "check")
    except Exception:
        return


def _page_group(page_obj, out):
    try:
        grp = page_obj.get("/Group")
        if grp is not None and str(grp.get("/S")) == "/Transparency":
            _add_blocker(out, "transparency", "transparency group p{p}", "check")
    except Exception:
        return


def _annotations(page, out):
    try:
        annot = page.first_annot
    except Exception:
        return
    while annot:
        try:
            typ = annot.type
            tname = typ[1] if isinstance(typ, (tuple, list)) and len(typ) > 1 else str(typ)
            colors = annot.colors or {}
            colored = False
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
                    colored = True
            low = str(tname).lower()
            if colored:
                _add_blocker(out, "annot", "%s annotation p{p}" % tname, "color")
            elif low == "highlight":
                _add_blocker(out, "annot", "%s annotation p{p}" % tname, "check")
        except Exception:
            pass
        try:
            annot = annot.next
        except Exception:
            break


def _norm_name(name):
    return str(name or "").lstrip("/")


def _image_records(doc, page, page_no, placements):
    import numpy as np
    import pymupdf
    by_name = {}
    for place in placements or []:
        by_name.setdefault(_norm_name(place.get("name")), []).append(place)
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
        name = _norm_name(im[7])
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
        spots = by_name.get(name) or []
        ppi = None
        area_frac = 1.0
        if spots and w and h:
            area = 0.0
            widest = 0.0
            for place in spots:
                pw = max(0.0, float(place.get("w_pt") or 0))
                ph = max(0.0, float(place.get("h_pt") or 0))
                area += pw * ph
                if pw > widest:
                    widest = pw
            if area > 1 and widest > 1:
                area_frac = min(1.0, area / page_area)
                ppi = round(w / (widest / 72.0), 1)
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


def _text_items(page, need_trace):
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
    if not need_trace:
        return items
    try:
        trace = page.get_texttrace() or []
    except Exception:
        trace = []
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


def _mark_red_text(texts):
    for item in texts:
        rgb = item.get("rgb")
        if not rgb or len(rgb) < 3:
            continue
        r, g, b = _unit(rgb[0]), _unit(rgb[1]), _unit(rgb[2])
        item["red"] = r > 0.5 and g < 0.35 and b < 0.35 and (r - max(g, b)) > 0.3


def _image_summary(images):
    if not images:
        return "no images"
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
    return ", ".join(bits)


def _gray_notes(page_no, texts, contrib, gray_bpp):
    notes = []
    shown = 0
    gray_texts = [t for t in texts if t["cls"] == "gray"]
    for t in gray_texts:
        if shown >= 4:
            break
        snippet = (t.get("text") or "").replace("\n", " ")[:24]
        notes.append('gray text span "%s" p%d (%d bpp)' % (snippet, page_no, t["bpp"]))
        shown += 1
    extra = len(gray_texts) - shown
    if extra > 0:
        notes.append("%d more gray text spans p%d" % (extra, page_no))
    if contrib["gray_vectors"]:
        bits = contrib["gray_notes"][:3]
        tail = (": " + "; ".join(bits)) if bits else ""
        notes.append("%d gray drawing(s) p%d, needs %d bpp%s" % (
            contrib["gray_vectors"], page_no, gray_bpp, tail))
    return notes


def _stamp(blockers, page_no):
    for b in blockers:
        b["page"] = page_no
        b["text"] = b["text"].replace("{p}", str(page_no))
    # Drop duplicate texts created when a form and a page use the same wording.
    seen = set()
    kept = []
    for b in blockers:
        if b["text"] in seen:
            continue
        seen.add(b["text"])
        kept.append(b)
    return kept


def _page_decision(page_no, images, texts, contrib):
    """Pick a label. Risky constructs stay 'check' until a render resolves them."""
    for im in images:
        if im.get("smask"):
            _add_blocker(contrib, "transparency", "soft mask p{p}", "check")
        if im.get("kind") == "undecodable":
            _add_blocker(contrib, "image", "undecodable image xref %d p{p}" % im["xref"], "check")
    blockers = contrib["blockers"]
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
    if contrib["gray_vectors"]:
        saw_gray = True
        vector_bpp = max(contrib["gray_bpp"], bpp_for_levels(len(contrib["gray_levels"]) + 2))
        gray_bpp = max(gray_bpp, vector_bpp)
    if contrib["bw_vectors"]:
        gray_bpp = max(gray_bpp, 1)
    color_text = [t for t in texts if t["cls"] == "color"]
    color_images = [im for im in images if im.get("kind") == "color"]
    large_color = [im for im in color_images if im.get("area_frac", 1) >= 0.05]
    small_color = [im for im in color_images if im.get("area_frac", 1) < 0.05]
    color_vec = contrib["color_vectors"] > 0
    confirmed = bool(color_text or color_images or color_vec or any(b.get("severity") == "color" for b in blockers))
    has_bw_body = (
        any(t["cls"] in ("black", "white", "gray") for t in texts if t["cls"] != "ignored")
        or any(im.get("kind") in ("bilevel", "gray") for im in images)
        or contrib["bw_vectors"] or contrib["gray_vectors"])
    if large_color or (color_vec and not has_bw_body and not color_text and not small_color):
        label, target = "COLOR", "color"
    elif confirmed:
        label, target = "MOSTLY-BW-WITH-COLOR-ACCENTS", "color"
    elif saw_gray and gray_bpp > 1:
        label, target = "GRAY", "gray-%d" % gray_bpp
    else:
        label, target = "BW", "bw-1"
        gray_bpp = 1
    for t in color_text:
        snippet = (t.get("text") or "").replace("\n", " ")[:24]
        wording = 'red text span "%s" p{p}' if t.get("red") else 'color text span "%s" p{p}'
        _add_blocker(contrib, "text", wording % snippet, "color")
    for im in color_images:
        _add_blocker(contrib, "image", "color image xref %d p{p}" % im["xref"], "color")
    if color_vec and not any(b["kind"] == "vector" for b in blockers):
        _add_blocker(contrib, "vector", "color drawing p{p}", "color")
    checks = [b for b in blockers if b.get("severity") == "check"]
    if confirmed:
        for b in checks:
            b["severity"] = "warning"
            if not b["text"].endswith("(warning)"):
                b["text"] += " (warning)"
        checks = []
    notes = _gray_notes(page_no, texts, contrib, gray_bpp) if label == "GRAY" else []
    blockers = _stamp(blockers, page_no)
    return {
        "page": page_no,
        "label": label,
        "target": target,
        "suggested_bpp": None if target == "color" else gray_bpp,
        "gray_bpp": gray_bpp,
        "image_summary": _image_summary(images),
        "images": images,
        "text": texts,
        "blockers": blockers,
        "notes": notes,
        "blocker_count": len(blockers) + len(notes),
        "needs_render": bool(checks) and not confirmed,
    }


def _check_name(blocker):
    kind = blocker.get("kind")
    text = blocker.get("text") or ""
    if kind == "transparency":
        if "soft mask" in text:
            return "soft mask"
        if "blend mode" in text:
            return "blend mode"
        return "transparency group"
    if kind == "shading":
        return "shading pattern"
    if kind == "spot":
        return "spot color"
    if kind == "icc":
        return "ICC color"
    if kind == "pattern":
        return "tiling pattern"
    if kind == "indexed":
        return "indexed color"
    if kind == "image":
        return "undecodable image"
    return kind or "unresolved construct"


def _render_measure(page, annots=False):
    """Rasterize at about 72 dpi and report whether the pixels are neutral."""
    try:
        import numpy as np
        import pymupdf
        long_pt = max(float(page.rect.width), float(page.rect.height), 1.0)
        dpi = float(RENDER_DPI)
        if long_pt / 72.0 * dpi > RENDER_MAX_EDGE:
            dpi = RENDER_MAX_EDGE * 72.0 / long_pt
        pix = page.get_pixmap(dpi=int(round(dpi)), alpha=False, colorspace=pymupdf.csRGB, annots=bool(annots))
        n = pix.n
        arr = np.frombuffer(pix.samples, np.uint8)
        arr = arr.reshape(pix.h, pix.stride)[:, :pix.w * n].reshape(pix.h, pix.w, n)
        step = 1
        if pix.w * pix.h > 400000:
            step = int((pix.w * pix.h / 400000) ** 0.5) or 1
        sample = arr[::step, ::step, :3].astype(np.int16)
        if sample.size == 0:
            return None
        mx = sample.max(axis=2)
        mn = sample.min(axis=2)
        chroma = (mx - mn) / 255.0
        color_frac = float((chroma > CHROMA_MAX).mean())
        if color_frac > 0.002:
            return {"neutral": False, "color_frac": color_frac, "bpp": None}
        y = (0.299 * sample[..., 0] + 0.587 * sample[..., 1] + 0.114 * sample[..., 2])
        y8 = np.clip(y, 0, 255).astype(np.uint8)
        hist = np.bincount(y8.ravel(), minlength=256)
        total = float(hist.sum()) or 1.0
        black_cut = int(BLACK_MAX * 255)
        white_cut = int(WHITE_MIN * 255)
        mid = float(hist[black_cut + 1:white_cut].sum())
        if mid / total < 0.015:
            return {"neutral": True, "bpp": 1, "color_frac": color_frac}
        sig = [i for i in range(black_cut + 1, white_cut) if hist[i] / total >= 0.004]
        if not sig:
            return {"neutral": True, "bpp": 1, "color_frac": color_frac}
        clusters = 1
        for a, b in zip(sig, sig[1:]):
            if b - a > 8:
                clusters += 1
        bpp = bpp_for_levels(clusters + 2)
        return {"neutral": True, "bpp": bpp, "color_frac": color_frac}
    except Exception:
        return None


def _apply_render(page_rec, measured):
    if not page_rec.get("needs_render"):
        return page_rec
    checks = [b for b in page_rec["blockers"] if b.get("severity") == "check"]
    names = []
    for b in checks:
        short = _check_name(b)
        if short not in names:
            names.append(short)
    present = ", ".join(names) if names else "unresolved construct"
    notes = list(page_rec.get("notes") or [])
    if measured is None:
        page_rec["label"] = "NEEDS-CHECK"
        page_rec["target"] = "color"
        page_rec["suggested_bpp"] = None
        notes.append("NEEDS-CHECK (%s present)" % present)
    elif not measured["neutral"]:
        if measured["color_frac"] < 0.02:
            page_rec["label"] = "MOSTLY-BW-WITH-COLOR-ACCENTS"
            phrase = "MOSTLY-BW-WITH-COLOR-ACCENTS by render"
        else:
            page_rec["label"] = "COLOR"
            phrase = "COLOR by render"
        page_rec["target"] = "color"
        page_rec["suggested_bpp"] = None
        notes.append("%s (%s present)" % (phrase, present))
    else:
        render_bpp = int(measured["bpp"] or 1)
        stream_bpp = int(page_rec.get("gray_bpp") or 1)
        bpp = max(stream_bpp, render_bpp)
        if bpp <= 1:
            page_rec["label"] = "BW"
            page_rec["target"] = "bw-1"
            page_rec["suggested_bpp"] = 1
            phrase = "BW by render"
        else:
            page_rec["label"] = "GRAY"
            page_rec["target"] = "gray-%d" % bpp
            page_rec["suggested_bpp"] = bpp
            phrase = "GRAY by render" if render_bpp > 1 else "render is neutral"
        for b in checks:
            b["severity"] = "warning"
            if not str(b["text"]).endswith("(warning)"):
                b["text"] = b["text"] + " (warning)"
        notes.insert(0, "%s (%s present)" % (phrase, present))
    page_rec["notes"] = notes
    page_rec["blocker_count"] = len(page_rec["blockers"]) + len(notes)
    page_rec["needs_render"] = False
    return page_rec


def _document(pages, image_table):
    labels = [p["label"] for p in pages] or ["BW"]
    for label in ("COLOR", "MOSTLY-BW-WITH-COLOR-ACCENTS", "NEEDS-CHECK", "GRAY", "BW"):
        if label in labels:
            break
    else:
        label = "BW"
    if label == "BW":
        target, bpp = "bw-1", 1
    elif label == "GRAY":
        bpp = max([p["suggested_bpp"] or 1 for p in pages if p["label"] == "GRAY"] or [1])
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
        for note in p.get("notes") or []:
            if note not in seen:
                seen.add(note)
                blockers.append({"page": p["page"], "kind": "note", "text": note, "severity": "note"})
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
    require_analysis_deps()
    import pikepdf
    import pymupdf
    doc = pymupdf.open(path)
    pdf = pikepdf.open(path)
    cache = {"instr": {}, "forms": {}}
    image_table = []
    seen_hash = {}
    pages = []
    try:
        for index, page in enumerate(doc):
            if cancel is not None and cancel.is_set():
                return None
            page_no = index + 1
            contrib = _empty_contrib()
            try:
                pk_page = pdf.pages[index]
                res = pk_page.get("/Resources", None)
                if res is None:
                    res = pk_page.resources
                _page_group(pk_page.obj, contrib)
                _walk_content(pdf, pk_page.obj, res, contrib, set(), 0, cache)
            except Exception as exc:
                _add_blocker(contrib, "stream", "content stream could not be parsed (%s) p{p}" % exc, "check")
            images = _image_records(doc, page, page_no, contrib["placements"])
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
            texts = _text_items(page, contrib["tr3"] > 0)
            _mark_red_text(texts)
            _annotations(page, contrib)
            rec = _page_decision(page_no, images, texts, contrib)
            if rec.get("needs_render"):
                annots = any(b.get("kind") == "annot" and b.get("severity") == "check" for b in rec["blockers"])
                rec = _apply_render(rec, _render_measure(page, annots=annots))
            pages.append(rec)
    finally:
        doc.close()
        pdf.close()
    summary = _document(pages, image_table)
    return {"summary": summary, "pages": pages, "images": image_table}


def analyze_lines(data):
    """Short ANALYZE log lines, without a timestamp."""
    if not data:
        return []
    if data.get("error"):
        lines = ["failed: %s" % (data.get("message") or data["error"].strip().splitlines()[-1])]
        lines.extend(data["error"].splitlines())
        return lines
    s = data["summary"]
    lines = ["document %s target %s pages %d images %d duplicates %d blockers %d" % (
        s["label"], s["target"], s["pages"], s["images"], s["duplicates"], s["blocker_count"])]
    for p in data["pages"]:
        lines.append("p%d %s target %s %s blockers %d" % (
            p["page"], p["label"], p["target"], p["image_summary"], p["blocker_count"]))
        for b in p["blockers"]:
            lines.append("blocker %s" % b["text"])
        for note in p.get("notes") or []:
            lines.append("note %s" % note)
    return lines


def format_report(data):
    """Human summary for --analyze-only."""
    if not data:
        return "analysis cancelled"
    if data.get("error"):
        return data.get("message") or data["error"]
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
        for note in p.get("notes") or []:
            lines.append("  - %s" % note)
    return "\n".join(lines)


def missing_message(exc):
    """Human text for a ModuleNotFoundError or MissingDependency."""
    if isinstance(exc, MissingDependency):
        return str(exc)
    if isinstance(exc, ModuleNotFoundError):
        name = getattr(exc, "name", None) or "unknown"
        pip = _PIP_NAMES.get(name, name)
        return "Missing package: %s. Install it with: pip install %s" % (pip, pip)
    return None
