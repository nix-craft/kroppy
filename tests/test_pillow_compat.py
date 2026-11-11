"""Indexed quantization still runs on Pillow 8 and 9.0, before the enums."""
import numpy as np


def test_indexed_quantize_without_pillow_91_enums(monkeypatch):
    from PIL import Image
    from krop.imagetarget import _indexed, pil_const

    enums = {}
    for name in ("Dither", "Quantize", "Resampling", "Transpose"):
        if hasattr(Image, name):
            enums[name] = getattr(Image, name)
    real_convert = Image.Image.convert

    def convert(self, *args, **kwargs):
        # Pillow 13's convert looks up Image.Dither. Put the enums back only
        # for that call, so the test still simulates their absence for us.
        for name, value in enums.items():
            setattr(Image, name, value)
        try:
            return real_convert(self, *args, **kwargs)
        finally:
            for name in enums:
                if hasattr(Image, name):
                    delattr(Image, name)

    monkeypatch.setattr(Image.Image, "convert", convert)
    for name in enums:
        monkeypatch.delattr(Image, name, raising=False)

    # The integers Pillow kept on Image itself before 9.1.
    assert pil_const(Image, "Dither", "NONE", None) == Image.NONE
    assert pil_const(Image, "Dither", "FLOYDSTEINBERG", None) == Image.FLOYDSTEINBERG
    assert pil_const(Image, "Quantize", "MEDIANCUT", None) == Image.MEDIANCUT
    assert pil_const(Image, "Quantize", "FASTOCTREE", None) == Image.FASTOCTREE
    assert pil_const(Image, "Quantize", "LIBIMAGEQUANT", None) == Image.LIBIMAGEQUANT
    assert pil_const(Image, "Resampling", "LANCZOS", None) == Image.LANCZOS
    assert pil_const(Image, "Transpose", "ROTATE_90", None) == Image.ROTATE_90

    real = Image.Image.quantize

    def old_quantize(self, colors=256, method=None, kmeans=0, palette=None):
        # Pillow 9.0.1 rejects the dither keyword added in 9.1.
        return real(self, colors=colors, method=method, kmeans=kmeans, palette=palette)

    monkeypatch.setattr(Image.Image, "quantize", old_quantize)
    arr = np.zeros((8, 8, 3), np.uint8)
    arr[:4] = (200, 10, 10)
    arr[4:] = (10, 10, 200)
    back, ok = _indexed(arr, 4, dither=False)
    assert ok
    assert back.shape == arr.shape
    assert len({tuple(int(v) for v in px) for px in back.reshape(-1, 3)}) <= 4
    dithered, ok_d = _indexed(arr, 4, dither=True)
    assert ok_d
    assert dithered.shape == arr.shape

    # Image.NONE is 0, which is also NEAREST. The preview resampler must not
    # pass that into resize when the Resampling enum is missing. Pillow 13's
    # own resize looks up Resampling by name, so record the argument instead.
    from krop.preview import _resize_box
    seen = {}

    def fake_resize(self, size, resample=None, *args, **kwargs):
        seen["resample"] = resample
        seen["size"] = size
        bands = len(self.getbands())
        shape = (size[1], size[0]) if bands == 1 else (size[1], size[0], bands)
        return Image.fromarray(np.zeros(shape, np.uint8))

    monkeypatch.setattr(Image.Image, "resize", fake_resize)
    board = np.zeros((8, 8, 3), np.uint8)
    _resize_box(board, 2, 2)
    assert seen["size"] == (2, 2)
    assert seen["resample"] == Image.BOX
    assert seen["resample"] not in (0, Image.NONE, Image.NEAREST)
