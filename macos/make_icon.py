"""Draw the app icon (terracotta rounded square, white >_ prompt) into AppIcon.icns.

    uv run --with pillow python make_icon.py AppIcon.icns

To use your own icon instead, replace AppIcon.icns (e.g. from a 1024px PNG with
`iconutil`, or any icon editor) and rebuild with ./build.sh.
"""
import sys
from PIL import Image, ImageDraw, ImageFilter

S = 1024 * 4  # supersample, then downscale for smooth edges
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

# Apple's grid: 824x824 body centred in 1024, corner radius ~185
inset, radius = 100 * 4, 185 * 4
box = (inset, inset, S - inset, S - inset)

# soft drop shadow
shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(shadow).rounded_rectangle((box[0], box[1] + 12 * 4, box[2], box[3] + 12 * 4), radius, fill=(0, 0, 0, 90))
img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14 * 4)))

# vertical gradient body
top, bottom = (226, 128, 92), (194, 86, 54)
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = min(1, max(0, (y - box[1]) / (box[3] - box[1])))
    gd.line([(0, y), (S, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle(box, radius, fill=255)
img.paste(grad, (0, 0), mask)

# the prompt: a chevron ">" and an underscore "_"
d = ImageDraw.Draw(img)
w = 74 * 4
white = (255, 255, 255, 255)
cx, cy = 512 * 4, 512 * 4
p1, p2, p3 = (cx - 200 * 4, cy - 150 * 4), (cx - 40 * 4, cy), (cx - 200 * 4, cy + 150 * 4)
d.line([p1, p2, p3], fill=white, width=w, joint="curve")
for p in (p1, p2, p3):
    d.ellipse((p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2), fill=white)
u1, u2 = (cx + 20 * 4, cy + 150 * 4), (cx + 210 * 4, cy + 150 * 4)
d.line([u1, u2], fill=white, width=w)
for p in (u1, u2):
    d.ellipse((p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2), fill=white)

icon = img.resize((1024, 1024), Image.LANCZOS)
icon.save("icon_1024.png")
icon.save(sys.argv[1], sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)])
