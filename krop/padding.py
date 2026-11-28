# -*- coding: utf-8 -*-
"""Live trim padding.

Every trim reads the Padding field at the moment it runs. Nothing here
remembers a value from startup or from the previous trim.
"""


def parse_padding(text):
    """CSS-style padding text to [top, right, bottom, left].

    One number applies to every side. Two numbers are top/bottom then
    right/left. Three numbers are top, right/left, bottom. Empty is zero.
    """
    parts = [piece.strip() for piece in str(text or "").split(",") if piece.strip()]
    if not parts:
        return [0.0, 0.0, 0.0, 0.0]
    try:
        nums = [float(piece) for piece in parts]
    except ValueError:
        raise ValueError("padding must be one to four numbers")
    if len(nums) == 1:
        return nums * 4
    if len(nums) == 2:
        return nums * 2
    if len(nums) == 3:
        return [nums[0], nums[1], nums[2], nums[1]]
    if len(nums) == 4:
        return nums
    raise ValueError("padding must be one to four numbers")


def padding_from_window(window):
    """The Padding field as it is right now, not a cached copy."""
    return parse_padding(window.ui.editPadding.text())
