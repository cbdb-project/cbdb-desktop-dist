r"""Recolour the CBDB badge to Harvard Crimson and emit assets/favicon.ico.

    python make-favicon.py                     # logo in the repo root -> assets\
    python make-favicon.py --preview           # also write a 16/32/48 strip
    python make-favicon.py --ink '#540405'     # keep the master's own colour

The source is the .png in the repository root (LOGO.png if there are several),
or whatever --source names. Outputs, both under assets\ :
    favicon.ico        16/32/48/64/128/256, for the browser tab
    logo-crimson.png   full-resolution recolour, same framing as the master

The source master is never modified.

Two things this has to get right:

Recolouring. The badge is white ink on a solid red ground with anti-aliased
edges. Replacing "pixels near the old red" leaves a dark halo along every
stroke, because the in-between pixels are blends. So each pixel is decomposed
into how much of it is white ink -- t, by projecting the colour onto the line
between the two inks -- and then recomposited as white over the new red. Edges
come out clean at any target colour.

Framing. The master has a wide white margin, and a favicon is displayed as
small as 16x16, so that margin has to go or the badge occupies a handful of
pixels in the middle. Trim it, then pad back to a square with the new red.

Run this only when the logo or the brand colour changes; build-dist.py copies
the committed favicon.ico into the payload rather than regenerating it.
"""

import argparse
import glob
import os

import numpy as np
from PIL import Image, ImageChops, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SIZES = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
HARVARD_CRIMSON = "#A51C30"
WHITE = np.array([255.0, 255.0, 255.0])


def find_logo():
    """The logo image in the repository root, whatever it happens to be called."""
    pngs = sorted(p for p in glob.glob(os.path.join(ROOT, "*.png"))
                  if not os.path.basename(p).startswith("."))
    exact = [p for p in pngs if os.path.basename(p).lower() == "logo.png"]
    if exact:
        return exact[0]
    if len(pngs) == 1:
        return pngs[0]
    if not pngs:
        raise SystemExit("no .png in %s -- put the logo there, or pass --source" % ROOT)
    raise SystemExit("several .png files in %s:\n  %s\nPass --source to choose."
                     % (ROOT, "\n  ".join(os.path.basename(p) for p in pngs)))


def parse_hex(s):
    s = s.lstrip("#")
    if len(s) != 6:
        raise SystemExit("colour must be #rrggbb, got %r" % s)
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def flatten(path):
    """Open as RGB over white; the master's margin is white and semi-transparent."""
    im = Image.open(path).convert("RGBA")
    flat = Image.new("RGB", im.size, (255, 255, 255))
    flat.paste(im, mask=im.split()[3])
    return flat


def dominant_ink(img):
    """The most common colour that is not the white ground -- the badge's red."""
    counts = sorted(img.getcolors(maxcolors=1 << 24), reverse=True)
    for _, colour in counts:
        if sum(colour) < 720:                    # i.e. not near-white
            return colour
    raise SystemExit("no non-white colour found; is this the right image?")


def recolour(img, old, new):
    """Rebuild the image as white ink over `new` instead of over `old`."""
    a = np.asarray(img, dtype=np.float64)
    old = np.array(old, dtype=np.float64)
    axis = WHITE - old
    # t = 0 -> pure ground, t = 1 -> pure white ink.
    t = ((a - old) @ axis) / (axis @ axis)
    t = np.clip(t, 0.0, 1.0)[..., None]
    new = np.array(new, dtype=np.float64)
    out = new + t * (WHITE - new)
    return Image.fromarray(np.rint(out).clip(0, 255).astype(np.uint8), "RGB")


def square(img, pad):
    box = ImageChops.difference(img, Image.new("RGB", img.size, (255, 255, 255))).getbbox()
    if box is None:
        raise SystemExit("image is entirely white")
    art = img.crop(box)
    side = max(art.size)
    out = Image.new("RGB", (side, side), pad)
    out.paste(art, ((side - art.size[0]) // 2, (side - art.size[1]) // 2))
    return out, art.size, box


def write_preview(sq, path):
    tile = 4
    strip = Image.new("RGB", ((16 + 32 + 48) * tile + 4 * 16, 48 * tile + 44), (245, 245, 245))
    draw = ImageDraw.Draw(strip)
    x = 16
    for s in (16, 32, 48):
        thumb = sq.resize((s, s), Image.LANCZOS).resize((s * tile, s * tile), Image.NEAREST)
        strip.paste(thumb, (x, 16))
        draw.text((x, 48 * tile + 26), "%dx%d" % (s, s), fill=(40, 40, 40))
        x += s * tile + 16
    strip.save(path)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", help="logo image (default: the .png in the repository root)")
    ap.add_argument("--outdir", default=os.path.join(HERE, "assets"))
    ap.add_argument("--ink", default=HARVARD_CRIMSON,
                    help="target ground colour as #rrggbb (default Harvard Crimson %s)"
                         % HARVARD_CRIMSON)
    ap.add_argument("--preview", action="store_true",
                    help="also write favicon-preview.png, the small sizes magnified")
    args = ap.parse_args()

    ink = parse_hex(args.ink)
    os.makedirs(args.outdir, exist_ok=True)
    source = args.source or find_logo()

    master = flatten(source)
    old = dominant_ink(master)
    print("source      %dx%d  %s" % (master.size + (source,)))
    print("ground      #%02X%02X%02X -> #%02X%02X%02X" % (old + ink))

    full = recolour(master, old, ink)
    logo = os.path.join(args.outdir, "logo-crimson.png")
    full.save(logo)
    print("recoloured  %s" % logo)

    sq, art_size, box = square(full, ink)
    print("trimmed     %dx%d  box=%s  -> square %dx%d"
          % (art_size + (box,) + sq.size))

    ico = os.path.join(args.outdir, "favicon.ico")
    sq.save(ico, format="ICO", sizes=SIZES)
    print("wrote       %s  (%d bytes, %s)"
          % (ico, os.path.getsize(ico), " ".join("%dx%d" % s for s in SIZES)))

    if args.preview:
        print("preview     %s" % write_preview(sq, os.path.join(args.outdir, "favicon-preview.png")))


if __name__ == "__main__":
    main()
