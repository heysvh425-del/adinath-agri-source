"""Create multi-resolution raster and ICO assets for the Adinath desktop build."""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "frontend" / "icons"
DESKTOP = ROOT / "desktop" / "assets"


def brand_mark(size: int) -> Image.Image:
    scale = size / 512
    point = lambda value: round(value * scale)
    image = Image.new("RGBA", (size, size), "#103d2b")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=point(112), fill="#103d2b")
    draw.polygon([(point(68), point(374)), (point(444), point(374)), (point(444), point(442)), (point(68), point(442))], fill="#4c9c45")
    for y in (394, 438):
        draw.arc((point(76), point(y - 42), point(436), point(y + 42)), 196, 344, fill="#dff0bc", width=point(12))
    draw.ellipse((point(314), point(95), point(410), point(191)), fill="#f3be4e")
    draw.polygon([(point(152), point(344)), (point(252), point(116)), (point(360), point(344)), (point(309), point(344)), (point(251), point(203)), (point(196), point(344))], fill="#f8fcf3")
    draw.polygon([(point(220), point(274)), (point(285), point(274)), (point(274), point(300)), (point(231), point(300))], fill="#103d2b")
    draw.pieslice((point(275), point(92), point(407), point(224)), 210, 340, fill="#9bd266")
    draw.line((point(281), point(216), point(382), point(88)), fill="#65b451", width=point(12))
    draw.polygon([(point(110), point(348)), (point(199), point(348)), (point(199), point(313)), (point(145), point(313)), (point(132), point(292)), (point(110), point(292))], fill="#f8fcf3", outline="#103d2b", width=point(6))
    draw.ellipse((point(114), point(335), point(152), point(373)), fill="#f8fcf3", outline="#103d2b", width=point(6))
    draw.ellipse((point(165), point(329), point(215), point(379)), fill="#f8fcf3", outline="#103d2b", width=point(6))
    return image


DESKTOP.mkdir(parents=True, exist_ok=True)
for filename, size in {
    WEB / "icon-192.png": 192, WEB / "adinath-agri-icon-192.png": 192,
    WEB / "icon-512.png": 512, WEB / "icon-512-maskable.png": 512,
    WEB / "apple-touch-icon.png": 180, DESKTOP / "adinath-agri-icon-192.png": 192,
    DESKTOP / "adinath-agri-logo-rendered.png": 512, DESKTOP / "adinath-agri-splash-logo.png": 512,
}.items():
    brand_mark(size).save(filename, "PNG", optimize=True)
brand_mark(512).save(DESKTOP / "adinath-agri-services.ico", format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
