#!/usr/bin/env python3
"""Pixelate the photographic areas of Vault screenshots, leaving the UI readable.

The public build ships no imagery at all; this produces a middle option — real
screenshots with every poster/portrait/video frame pixelated past recognition,
so the layout still documents itself.

Detection is structural rather than hand-placed coordinates: Vault's chrome is
flat and near-black, while poster art is bright and textured. Each block is
scored on mean luminance + local detail, the mask is grown slightly so edges
don't leak, and only those blocks are pixelated.

    python3 tools/censor_shots.py IN_DIR OUT_DIR [--block 16] [--pixel 22]
"""
import argparse
import os
import sys

try:
    from PIL import Image, ImageFilter
except ImportError:
    sys.exit("needs Pillow:  python3 -m pip install pillow")


def photographic_mask(img, block, mean_thr, sat_thr):
    """Block mask marking photo content.

    Discriminating on *colour* rather than detail is what keeps UI text sharp:
    Vault's chrome is monochrome (near-black panels, white text) while poster art
    carries skin tones and scene colour. Text does have high local detail, which
    is why an edge/detail score alone smears the labels.
    """
    w, h = img.size
    bw, bh = (w + block - 1) // block, (h + block - 1) // block
    small = img.resize((bw, bh), Image.BOX)          # per-block average colour
    hsv = small.convert("HSV")
    _, S, V = hsv.split()
    sp, vp = S.load(), V.load()

    # local detail per block: how far the block average sits from its neighbours.
    # Catches dark or desaturated photo frames that the colour test alone misses;
    # text also scores high here, but the opening pass below erases it.
    blurred = small.convert("L").filter(ImageFilter.GaussianBlur(2))
    flat = small.convert("L")
    bp, fp = blurred.load(), flat.load()

    mask = Image.new("L", (bw, bh), 0)
    mp = mask.load()
    for y in range(bh):
        for x in range(bw):
            if vp[x, y] < mean_thr:
                continue
            detail = abs(fp[x, y] - bp[x, y]) * 4
            if sp[x, y] >= sat_thr or detail >= 22:
                mp[x, y] = 255
    return mask


def open_then_grow(mask, erode, dilate):
    """Morphological opening kills thin/isolated blocks (text, accent pixels, badges),
    leaving only the large contiguous regions a poster forms; then grow to cover edges."""
    for _ in range(erode):
        mask = mask.filter(ImageFilter.MinFilter(3))
    for _ in range(erode + dilate):
        mask = mask.filter(ImageFilter.MaxFilter(3))
    return mask


def fill_bboxes(mask, min_blocks, max_box_frac):
    """Replace each surviving blob with its bounding box.

    Posters are rectangles, so this covers a poster completely even where detection
    only caught part of it (a dark or desaturated frame), and it squares off the
    ragged edges dilation leaves behind. Blobs smaller than min_blocks are dropped
    as noise rather than grown into a box.
    """
    w, h = mask.size
    px = mask.load()
    seen = bytearray(w * h)
    out = Image.new("L", (w, h), 0)
    od = out.load()
    for sy in range(h):
        for sx in range(w):
            if not px[sx, sy] or seen[sy * w + sx]:
                continue
            # iterative flood fill (4-connected); recursion would blow the stack
            stack = [(sx, sy)]
            seen[sy * w + sx] = 1
            cells = []
            x0 = x1 = sx
            y0 = y1 = sy
            while stack:
                x, y = stack.pop()
                cells.append((x, y))
                if x < x0: x0 = x
                if x > x1: x1 = x
                if y < y0: y0 = y
                if y > y1: y1 = y
                for nx, ny in ((x+1, y), (x-1, y), (x, y+1), (x, y-1)):
                    if 0 <= nx < w and 0 <= ny < h and px[nx, ny] \
                            and not seen[ny * w + nx]:
                        seen[ny * w + nx] = 1
                        stack.append((nx, ny))
            if len(cells) < min_blocks:
                continue
            # If a blob's box would swallow the frame, the blobs have bridged into one
            # region and boxing it would censor the whole screenshot. Fall back to the
            # blob's own cells, which still cover the content without eating the UI.
            box_area = (x1 - x0 + 1) * (y1 - y0 + 1)
            if box_area > max_box_frac * w * h:
                for x, y in cells:
                    od[x, y] = 255
                continue
            for y in range(y0, y1 + 1):
                for x in range(x0, x1 + 1):
                    od[x, y] = 255
    return out


def censor(path, out, block, pixel, mean_thr, sat_thr, erode, dilate, min_blocks,
           max_box_frac=0.35):
    img = Image.open(path).convert("RGB")
    w, h = img.size
    mask = photographic_mask(img, block, mean_thr, sat_thr)
    mask = open_then_grow(mask, erode, dilate)
    mask = fill_bboxes(mask, min_blocks, max_box_frac)
    bw, bh = mask.size
    covered = sum(1 for p in mask.getdata() if p)
    full = mask.resize((w, h), Image.NEAREST).point(lambda p: 255 if p > 127 else 0)

    small = img.resize((max(1, w // pixel), max(1, h // pixel)), Image.BILINEAR)
    pixelated = small.resize((w, h), Image.NEAREST)

    img.paste(pixelated, (0, 0), full)
    img.save(out, optimize=True)
    return 100.0 * covered / max(1, bw * bh)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--block", type=int, default=8)
    ap.add_argument("--pixel", type=int, default=26, help="pixelation strength")
    ap.add_argument("--mean", type=int, default=45, help="min block brightness (HSV V)")
    ap.add_argument("--sat", type=int, default=30, help="min block saturation (HSV S)")
    ap.add_argument("--erode", type=int, default=2, help="opening strength; kills text")
    ap.add_argument("--dilate", type=int, default=1)
    ap.add_argument("--maxbox", type=float, default=0.35,
                    help="blobs whose bbox exceeds this fraction of the frame are not boxed")
    ap.add_argument("--minblocks", type=int, default=12,
                    help="drop blobs smaller than this before boxing them")
    a = ap.parse_args()

    os.makedirs(a.dst, exist_ok=True)
    names = sorted(f for f in os.listdir(a.src) if f.lower().endswith(".png"))
    if not names:
        sys.exit(f"no PNGs in {a.src}")
    for n in names:
        pct = censor(os.path.join(a.src, n), os.path.join(a.dst, n),
                     a.block, a.pixel, a.mean, a.sat, a.erode, a.dilate, a.minblocks, a.maxbox)
        print(f"  {n:24} {pct:5.1f}% of blocks pixelated")


if __name__ == "__main__":
    main()
