"""G3: the PNG pictures of a sample as pixels, standard library only (docs/ICONS.md).

decode reads the non-interlaced PNG of 8 bits per channel (grey, RGB, grey with alpha, RGBA) and palette PNG of 1 to 8
bits, with tRNS; anything else raises ValueError and the picture is not an icon. ink finds the colour of a one-colour
pictogram; recolour writes the same pictogram in another colour, keeping its alpha: the template author's own icons,
scaled and recoloured as template C, slide 25 allows ("масштабировать и изменять цвет").
"""
from collections import Counter
from itertools import accumulate, compress
import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
MAX_PIXELS = 4096 * 4096
# A pixel counts as drawn from this alpha on; a pictogram is one colour when this share of its drawn pixels lies within
# INK_DISTANCE of the most frequent colour (anti-aliased edges keep the colour and change only the alpha).
DRAWN_ALPHA = 128
INK_DISTANCE = 48
ONE_COLOUR = .9


def _chunks(raw):
    if raw[:8] != SIGNATURE:
        raise ValueError("не PNG")
    at = 8
    while at + 8 <= len(raw):
        length, kind = struct.unpack(">I4s", raw[at:at + 8])
        yield kind, raw[at + 8:at + 8 + length]
        at += 12 + length


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def _add_bytes(a, b):
    """Bytewise sum modulo 256 of two byte strings of one length, on whole integers (no carry between bytes)."""
    n = len(a)
    x, y = int.from_bytes(a, "big"), int.from_bytes(b, "big")
    low, high = int.from_bytes(b"\x7f" * n, "big"), int.from_bytes(b"\x80" * n, "big")
    return bytearray((((x & low) + (y & low)) ^ ((x ^ y) & high)).to_bytes(n, "big"))


def _unfilter(data, width, height, bpp, row_bytes):
    out = bytearray(height * row_bytes)
    prev = bytearray(row_bytes)
    at = 0
    for y in range(height):
        kind = data[at]
        row = bytearray(data[at + 1:at + 1 + row_bytes])
        at += 1 + row_bytes
        if kind == 1:
            # Sub: a running sum of every channel along the row.
            for c in range(bpp):
                row[c::bpp] = bytes(map((255).__and__, accumulate(row[c::bpp])))
        elif kind == 2:
            row = _add_bytes(row, prev)
        elif kind == 3:
            for i in range(row_bytes):
                row[i] = (row[i] + ((row[i - bpp] if i >= bpp else 0) + prev[i]) // 2) & 255
        elif kind == 4:
            for i in range(row_bytes):
                row[i] = (row[i] + _paeth(row[i - bpp] if i >= bpp else 0, prev[i], prev[i - bpp] if i >= bpp else 0)) & 255
        elif kind != 0:
            raise ValueError("неизвестный фильтр строки PNG")
        out[y * row_bytes:(y + 1) * row_bytes] = row
        prev = row
    return out


def decode(raw):
    """(width, height, RGBA bytes) of a PNG."""
    header, palette, alpha, data = None, None, None, bytearray()
    for kind, body in _chunks(raw):
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = [tuple(body[i:i + 3]) for i in range(0, len(body) - 2, 3)]
        elif kind == b"tRNS":
            alpha = body
        elif kind == b"IDAT":
            data += body
        elif kind == b"IEND":
            break
    if header is None:
        raise ValueError("нет заголовка PNG")
    width, height, depth, colour, _, _, interlace = header
    if interlace or colour not in CHANNELS or width * height > MAX_PIXELS or width == 0 or height == 0:
        raise ValueError("неподдерживаемый PNG")
    if colour != 3 and depth != 8 or colour == 3 and depth not in (1, 2, 4, 8) or colour == 3 and not palette:
        raise ValueError("неподдерживаемая глубина PNG")
    channels = CHANNELS[colour]
    row_bytes = (width * channels * depth + 7) // 8
    pixels = _unfilter(zlib.decompress(bytes(data)), width, height, max(1, channels * depth // 8), row_bytes)
    rgba = bytearray(width * height * 4)
    # Common layouts by slices; the rest (palette under 8 bits, a transparent colour of tRNS) pixel by pixel.
    if colour == 6:
        return width, height, pixels
    if colour in (0, 2, 4) and alpha is None or colour == 4 or colour == 3 and depth == 8:
        if colour == 3:
            table = [palette[i] if i < len(palette) else (0, 0, 0) for i in range(256)]
            for c in range(3):
                rgba[c::4] = pixels.translate(bytes(t[c] for t in table))
            rgba[3::4] = pixels.translate(bytes(alpha[i] if alpha is not None and i < len(alpha) else 255 for i in range(256)))
        else:
            for c in range(3):
                rgba[c::4] = pixels[(c if colour == 2 else 0)::channels]
            rgba[3::4] = pixels[1::2] if colour == 4 else b"\xff" * (width * height)
        return width, height, rgba
    for y in range(height):
        row = pixels[y * row_bytes:(y + 1) * row_bytes]
        for x in range(width):
            o = (y * width + x) * 4
            if colour == 3:
                bit = x * depth
                index = (row[bit // 8] >> (8 - depth - bit % 8)) & ((1 << depth) - 1)
                r, g, b = palette[index] if index < len(palette) else (0, 0, 0)
                a = alpha[index] if alpha is not None and index < len(alpha) else 255
            else:
                p = row[x * channels:(x + 1) * channels]
                if colour in (0, 4):
                    r = g = b = p[0]
                    a = p[1] if colour == 4 else 255
                    if colour == 0 and alpha is not None and len(alpha) >= 2 and p[0] == alpha[1]:
                        a = 0
                else:
                    r, g, b = p[0], p[1], p[2]
                    a = p[3] if colour == 6 else 255
                    if colour == 2 and alpha is not None and len(alpha) >= 6 and (r, g, b) == (alpha[1], alpha[3], alpha[5]):
                        a = 0
            rgba[o:o + 4] = bytes((r, g, b, a))
    return width, height, rgba


def top_row_clear(raw, below=16):
    """Owner request of 29.09.2026 (the third variant on pictures of the template): the share of the first row of a PNG with an
    alpha channel (grey or RGB with alpha, 8 bits) whose alpha is under `below`, or None for any other PNG. Only the first row
    is unpacked, so a large picture costs no full decode (the spring of template A 7, 1080 x 972: 2 s decoded whole). A cut-out
    object of the template (a 3D figure on a transparent ground) is clear along its top edge; a screenshot, a photograph or a
    panel is not."""
    try:
        header, stream = None, zlib.decompressobj()
        out = b""
        for kind, body in _chunks(raw):
            if kind == b"IHDR":
                header = struct.unpack(">IIBBBBB", body)
                width, _, depth, colour, _, _, interlace = header
                if colour not in (4, 6) or depth != 8 or interlace or width == 0:
                    return None
                channels = CHANNELS[colour]
                row_bytes = width * channels
            elif kind == b"IDAT" and header is not None:
                out += stream.decompress(stream.unconsumed_tail + body, 1 + row_bytes - len(out))
                if len(out) >= 1 + row_bytes:
                    row = _unfilter(out[:1 + row_bytes], width, 1, channels, row_bytes)
                    alpha = row[channels - 1::channels]
                    return sum(1 for a in alpha if a < below) / width
            elif kind == b"IEND":
                break
    except (ValueError, zlib.error, struct.error):
        return None
    return None


def _chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def encode(width, height, rgba):
    """An RGBA PNG, rows without filter."""
    stride = width * 4
    rows = b"".join(b"\x00" + bytes(rgba[y * stride:(y + 1) * stride]) for y in range(height))
    return (SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(rows, 9)) + _chunk(b"IEND", b""))


def ink(width, height, rgba):
    """The colour of a pictogram and how much of it is that colour: {"ink": "RRGGBB", "one_colour": share of drawn pixels
    near the ink, "coverage": share of the canvas drawn}; None for a picture without drawn pixels."""
    rgba = bytes(rgba)
    channels = [rgba[c::4] for c in range(4)]
    drawn_mask = channels[3].translate(bytes(int(v >= DRAWN_ALPHA) for v in range(256)))
    drawn = drawn_mask.count(1)
    if not drawn:
        return None
    bins = Counter(compress(zip(*(c.translate(bytes(v >> 4 for v in range(256))) for c in channels[:3])), drawn_mask))
    key = max(bins, key=lambda k: (bins[k], k))
    # The mean colour of the most frequent bin, not the bin centre: the ink of the author, to 1/255.
    in_bin = drawn_mask
    for c, k in zip(channels[:3], key):
        in_bin = _and(in_bin, c.translate(bytes(int(v >> 4 == k) for v in range(256))))
    count = in_bin.count(1)
    colour = tuple(round(sum(compress(c, in_bin)) / count) for c in channels[:3])
    near = drawn_mask
    for c, value in zip(channels[:3], colour):
        near = _and(near, c.translate(bytes(int(abs(v - value) <= INK_DISTANCE) for v in range(256))))
    return {"ink": "%02X%02X%02X" % colour, "one_colour": round(near.count(1) / drawn, 4), "coverage": round(drawn / (width * height), 4)}


def _and(a, b):
    """Bytewise AND of two masks of 0 and 1 bytes."""
    return (int.from_bytes(a, "big") & int.from_bytes(b, "big")).to_bytes(len(a), "big")


def pad(width, height, rgba, aspect):
    """The picture centred on a transparent canvas of the proportions `aspect` (width / height), so that a frame of those
    proportions shows it undistorted."""
    if abs(width / height - aspect) <= .01 * aspect:
        return width, height, rgba
    new_w, new_h = (round(height * aspect), height) if width / height < aspect else (width, round(width / aspect))
    canvas = bytearray(new_w * new_h * 4)
    dx, dy = (new_w - width) // 2, (new_h - height) // 2
    for y in range(height):
        start = ((y + dy) * new_w + dx) * 4
        canvas[start:start + width * 4] = rgba[y * width * 4:(y + 1) * width * 4]
    return new_w, new_h, canvas


def recolour(raw, colour, source=None, aspect=None):
    """The pictogram of a PNG in another colour: every pixel takes the colour and keeps its alpha. With the source ink
    given, a drawn pixel far from it (a light detail inside a one-colour pictogram) becomes a hole, as a knock-out.
    With aspect given, the result is padded to those proportions (pad)."""
    width, height, rgba = decode(raw)
    if aspect:
        width, height, rgba = pad(width, height, rgba, aspect)
    rgba = bytearray(rgba)
    n = width * height
    alpha = bytes(rgba[3::4])
    if source:
        # 255 where the pixel stays: not drawn, or near the source ink in every channel; 0 for a far drawn pixel.
        keep = alpha.translate(bytes(0 if v >= DRAWN_ALPHA else 255 for v in range(256)))
        near = b"\xff" * n
        for c, value in enumerate(bytes.fromhex(source)):
            near = _and(near, bytes(rgba[c::4]).translate(bytes(255 if abs(v - value) <= INK_DISTANCE else 0 for v in range(256))))
        keep = (int.from_bytes(keep, "big") | int.from_bytes(near, "big")).to_bytes(n, "big")
        alpha = _and(alpha, keep)
    for c, value in enumerate(bytes.fromhex(colour)):
        rgba[c::4] = bytes((value,)) * n
    rgba[3::4] = alpha
    return encode(width, height, rgba)
