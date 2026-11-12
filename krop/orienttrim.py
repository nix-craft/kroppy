# -*- coding: utf-8 -*-
"""Keep a trim in the same orientation as the full page.

The full page is the displayed page, which already includes /Rotate
(KropFullBox or MediaBox as shown). Boxes are (left, top, right, bottom).
"""


def _grow(start, end, need, room_before, room_after, prefer_after, limit_before, limit_after):
    """Lengthen [start, end] by up to ``need``, preferring one side.

    The preferred side is used first. Anything that does not fit is placed
    on the other side. The result stays inside the limits.
    """
    if need <= 1e-6:
        return start, end, False
    first_room = room_after if prefer_after else room_before
    second_room = room_before if prefer_after else room_after
    first = min(need, max(0.0, first_room))
    second = min(need - first, max(0.0, second_room))
    if prefer_after:
        end += first
        start -= second
    else:
        start -= first
        end += second
    if start < limit_before:
        start = limit_before
    if end > limit_after:
        end = limit_after
    return start, end, (first + second) > 1e-6


def keep_page_orientation(crop, content, full):
    """Grow ``crop`` so it is not turned relative to ``full``.

    A portrait page whose crop is wider than it is tall grows downward or
    upward, toward the side with more blank space between the content and
    the page edge. A landscape page whose crop is taller than it is wide
    grows right or left the same way. Returns (box, adjusted).
    """
    left, top, right, bottom = [float(v) for v in crop]
    content_left, content_top, content_right, content_bottom = [float(v) for v in content]
    full_left, full_top, full_right, full_bottom = [float(v) for v in full]
    page_width = full_right - full_left
    page_height = full_bottom - full_top
    crop_width = right - left
    crop_height = bottom - top
    if page_width <= 0 or page_height <= 0 or crop_width <= 0 or crop_height <= 0:
        return (left, top, right, bottom), False
    if page_height > page_width and crop_width > crop_height:
        need = crop_width - crop_height
        above = max(0.0, content_top - full_top)
        below = max(0.0, full_bottom - content_bottom)
        room_top = max(0.0, top - full_top)
        room_bottom = max(0.0, full_bottom - bottom)
        top, bottom, adjusted = _grow(
            top, bottom, need, room_top, room_bottom, below >= above, full_top, full_bottom)
        return (left, top, right, bottom), adjusted
    if page_width > page_height and crop_height > crop_width:
        need = crop_height - crop_width
        blank_left = max(0.0, content_left - full_left)
        blank_right = max(0.0, full_right - content_right)
        room_left = max(0.0, left - full_left)
        room_right = max(0.0, full_right - right)
        left, right, adjusted = _grow(
            left, right, need, room_left, room_right, blank_right >= blank_left, full_left, full_right)
        return (left, top, right, bottom), adjusted
    return (left, top, right, bottom), False
