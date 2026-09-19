"""Generates app.ico (used by the desktop shortcut and the app window). Run: python make_icon.py"""
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 512


def build() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # vertical gradient background clipped to a rounded square
    grad = Image.new("RGBA", (SIZE, SIZE))
    gd = ImageDraw.Draw(grad)
    top, bottom = (91, 140, 255), (124, 92, 255)
    for y in range(SIZE):
        t = y / (SIZE - 1)
        gd.line([(0, y), (SIZE, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=110, fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    # calendar body
    d.rounded_rectangle([104, 132, 408, 428], radius=40, fill=(255, 255, 255, 255))
    d.rounded_rectangle([104, 132, 408, 212], radius=40, fill=(59, 79, 216, 255))
    d.rectangle([104, 180, 408, 212], fill=(59, 79, 216, 255))
    # binder rings
    for x in (176, 336):
        d.rounded_rectangle([x - 14, 96, x + 14, 168], radius=14, fill=(255, 255, 255, 255))
    # day grid
    for row in range(3):
        for col in range(4):
            x0 = 136 + col * 66
            y0 = 236 + row * 62
            color = (199, 210, 254, 255)
            if (row, col) == (1, 2):
                color = (52, 211, 153, 255)
            d.rounded_rectangle([x0, y0, x0 + 46, y0 + 40], radius=10, fill=color)
    return img


if __name__ == "__main__":
    out = Path(__file__).resolve().parent
    icon = build()
    icon.save(out / "app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    icon.resize((256, 256), Image.LANCZOS).save(out / "app_icon.png")
    print("Wrote app.ico and app_icon.png")
