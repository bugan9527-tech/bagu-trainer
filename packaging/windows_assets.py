"""Build-time product artwork and PE resources; never imports application state."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bagu_trainer.version import __version__

PRODUCT = "AI 八股训练器"
ICON = ROOT / "packaging" / "BaguTrainer.ico"


def create_icon():
    # Match ui_theme.primary and ui_icons.practice. Render each size separately
    # so small shell icons retain their strokes; the source of truth is code.
    from PIL import Image, ImageDraw
    images = []
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        scale = 4
        image = Image.new("RGBA", (size * scale, size * scale))
        draw = ImageDraw.Draw(image)
        unit = size * scale / 24
        def box(a, b, c, d):
            return tuple(round(v * unit) for v in (a, b, c, d))
        draw.rounded_rectangle(box(1, 1, 23, 23), radius=round(5 * unit), fill="#2563EB")
        draw.ellipse(box(5, 5, 19, 19), outline="white", width=max(1, round(1.8 * unit)))
        draw.ellipse(box(10, 10, 14, 14), fill="white")
        images.append(image.resize((size, size), Image.Resampling.LANCZOS))
    images[-1].save(ICON, format="ICO", sizes=[im.size for im in images], append_images=images[:-1])


def version_resource():
    from PyInstaller.utils.win32.versioninfo import (
        VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct,
        VarFileInfo, VarStruct,
    )
    numeric = tuple(int(part) for part in __version__.split(".")) + (0,)
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numeric, prodvers=numeric, mask=0x3F,
                          flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
        kids=[StringFileInfo([StringTable("080404B0", [
            StringStruct("CompanyName", "BaguTrainer"),
            StringStruct("FileDescription", PRODUCT),
            StringStruct("FileVersion", __version__),
            StringStruct("InternalName", "BaguTrainer"),
            StringStruct("OriginalFilename", "BaguTrainer.exe"),
            StringStruct("ProductName", PRODUCT),
            StringStruct("ProductVersion", __version__),
            StringStruct("LegalCopyright", "Copyright © 2026 bugan9527-tech"),
        ])]), VarFileInfo([VarStruct("Translation", [2052, 1200])])],
    )


if __name__ == "__main__":
    create_icon()
    print(ICON)
