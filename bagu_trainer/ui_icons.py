"""DPI-aware monochrome line icons for navigation and key actions.

Icons are defined as vector primitives in a 24-unit square and rasterised with
Pillow at the requested pixel size (supersampled for anti-aliasing), then
converted to ``PhotoImage``.  They are cached on the owning theme and released
with it, so no emoji or external image assets are used.
"""
from PIL import Image, ImageDraw, ImageTk


# Primitive tuples: ("line", x1, y1, x2, y2) and ("rect", x1, y1, x2, y2) and
# ("ellipse", x1, y1, x2, y2) and ("arc", x1, y1, x2, y2, start, end).
_ICONS = {
    "dashboard": [  # 2x2 grid of tiles
        ("rect", 3, 3, 11, 11), ("rect", 13, 3, 21, 11),
        ("rect", 3, 13, 11, 21), ("rect", 13, 13, 21, 21),
    ],
    "practice": [  # concentric target (goal / practice)
        ("ellipse", 4, 4, 20, 20), ("ellipse", 9, 9, 15, 15),
    ],
    "history": [  # clock with hands
        ("ellipse", 4, 4, 20, 20),
        ("line", 12, 12, 12, 7), ("line", 12, 12, 16, 14),
    ],
    "projects": [  # folder
        ("line", 3, 7, 8, 7), ("line", 8, 7, 10, 9),
        ("rect", 3, 9, 21, 18),
    ],
    "bank": [  # closed book with spine
        ("line", 4, 5, 12, 5), ("line", 12, 5, 20, 5),
        ("line", 4, 5, 4, 19), ("line", 20, 5, 20, 19),
        ("line", 4, 19, 12, 19), ("line", 12, 19, 20, 19),
        ("line", 12, 5, 12, 19),
    ],
    "model": [  # processor chip with pins
        ("rect", 6, 6, 18, 18), ("rect", 9, 9, 15, 15),
        ("line", 12, 2, 12, 6), ("line", 12, 18, 12, 22),
        ("line", 2, 12, 6, 12), ("line", 18, 12, 22, 12),
    ],
    "settings": [  # three sliders
        ("line", 3, 7, 21, 7), ("ellipse", 8, 5, 11, 9),
        ("line", 3, 12, 21, 12), ("ellipse", 15, 10, 18, 14),
        ("line", 3, 17, 21, 17), ("ellipse", 10, 15, 13, 19),
    ],
    "collapse": [  # double chevron toward the left
        ("line", 13, 6, 8, 12), ("line", 8, 12, 13, 18),
    ],
    "expand": [  # double chevron toward the right
        ("line", 11, 6, 16, 12), ("line", 16, 12, 11, 18),
    ],
}


def _stroke_weight(px_size):
    return max(1, round(px_size * 1.8 / 24))


def make_icon(theme, name, color, size=20):
    """Return a cached PhotoImage for ``name`` at ``size`` logical px (scaled)."""
    px_size = theme.px(size)
    cache = getattr(theme, "_icon_cache", None)
    if cache is None:
        cache = theme._icon_cache = {}
    key = (name, px_size, color)
    if key in cache:
        return cache[key]
    primitives = _ICONS.get(name, _ICONS["dashboard"])
    scale = px_size / 24.0
    supersample = 4
    big = px_size * supersample
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    width = _stroke_weight(big)
    for primitive in primitives:
        kind = primitive[0]
        coords = [int(round(v * scale * supersample)) for v in primitive[1:]]
        if kind == "line":
            draw.line(coords, fill=color, width=width)
        elif kind == "rect":
            draw.rectangle(coords, outline=color, width=width)
        elif kind == "ellipse":
            draw.ellipse(coords, outline=color, width=width)
        elif kind == "arc":
            x1, y1, x2, y2, start, end = coords
            draw.arc((x1, y1, x2, y2), start, end, fill=color, width=width)
    if supersample > 1:
        image = image.resize((px_size, px_size), Image.LANCZOS)
    photo = ImageTk.PhotoImage(image)
    cache[key] = photo
    return photo
