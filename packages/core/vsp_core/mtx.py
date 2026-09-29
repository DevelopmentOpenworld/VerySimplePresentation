"""MicroType Express (MTX) decompression of the fonts PowerPoint embeds (plan item 38, owner answer Р1 (а), 25.09.2026).

PowerPoint embeds fonts as EOT with MTX-compressed data. Windows unpacks them through its embedding library; a Linux server
has nothing for it, so this module does it in the standard library after the W3C member submission "MicroType Express
(MTX) Font Format" (2008, https://www.w3.org/submissions/MTX/): three LZCOMP blocks (adaptive Huffman coding over an LZ77
copy scheme, section 3.3 and Appendix C), the run-length layer LZCOMP applies to some blocks (only its interface is in the
submission; the escape scheme was checked against Windows), then the Compact Table Format back to TrueType: cvt (5.2),
glyf and loca (5.6-5.11, the triplet encoding WOFF2 later took over), the pushed values of every glyph program with its
hop codes (6.2) and the instructions (7). hdmx and VDMX (device metrics of Windows hinting) and DSIG (no longer valid) are
left out. Checked against Windows on the corpus (analysis/style-experiments/20260925-fonts-inventory/README.md): every
table and every glyph of every font Windows could load is the same (outline, on-curve flags, pushed values, code).
"""
import struct

BIT_BYTES = [bytes((b >> k) & 1 for k in range(7, -1, -1)) for b in range(256)]


class Bits:
    """The bits of a block, most significant first, one byte per bit: indexing is much faster than shifting in Python."""
    def __init__(self, data):
        self.stream, self.pos = b''.join(BIT_BYTES[b] for b in data), 0

    def bit(self):
        if self.pos >= len(self.stream):
            raise ValueError('MTX: end of data')
        self.pos += 1
        return self.stream[self.pos - 1]

    def value(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | self.bit()
        return v


def bits_used(x):
    return max(1, x.bit_length())


class AHuff:
    """Adaptive Huffman tree of AHUFF.C: nodes 1..2*range-1, leaves range..2*range-1, weights kept in decreasing order."""
    def __init__(self, bits, rng):
        self.bits, self.range = bits, rng
        n = 2 * rng
        self.up = [0] * n; self.left = [0] * n; self.right = [0] * n; self.code = [-1] * n; self.weight = [0] * n
        for i in range(2, n):
            self.up[i] = i // 2; self.weight[i] = 1
        for i in range(1, rng):
            self.left[i] = 2 * i; self.right[i] = 2 * i + 1
        self.index = [0] * rng
        for i in range(rng):
            self.code[i] = -1; self.code[rng + i] = i; self.left[rng + i] = -1; self.right[rng + i] = -1; self.index[i] = rng + i
        self._init_weight(1)
        bit_count2 = 0
        if 256 < rng < 512:
            bit_count2 = bits_used(rng - 256 - 1) + 1
        if bit_count2:
            self.update(self.index[256]); self.update(self.index[257])
            for _ in range(12): self.update(self.index[rng - 3])
            for _ in range(6): self.update(self.index[rng - 2])
        else:
            for _ in range(2):
                for i in range(rng): self.update(self.index[i])

    def _init_weight(self, a):
        stack, order = [a], []
        while stack:
            x = stack.pop(); order.append(x)
            if self.code[x] < 0:
                stack += [self.left[x], self.right[x]]
        for x in reversed(order):
            if self.code[x] < 0:
                self.weight[x] = self.weight[self.left[x]] + self.weight[self.right[x]]

    def _swap(self, a, b):
        up, left, right, code, weight = self.up, self.left, self.right, self.code, self.weight
        upa, upb = up[a], up[b]
        left[a], left[b] = left[b], left[a]; right[a], right[b] = right[b], right[a]
        code[a], code[b] = code[b], code[a]; weight[a], weight[b] = weight[b], weight[a]
        up[a], up[b] = upa, upb
        for x in (a, b):
            if code[x] < 0:
                up[left[x]] = x; up[right[x]] = x
            else:
                self.index[code[x]] = x

    def update(self, a):
        weight, up = self.weight, self.up
        while a != 1:
            w = weight[a]; b = a - 1
            if weight[b] == w:
                while weight[b] == w: b -= 1
                b += 1
                if b > 1:
                    self._swap(a, b); a = b
            weight[a] = w + 1
            a = up[a]
        weight[a] += 1

    def read(self):
        code, left, right, bits = self.code, self.left, self.right, self.bits
        stream, p, a = bits.stream, bits.pos, 1
        try:
            while True:
                a = right[a] if stream[p] else left[a]
                p += 1
                if code[a] >= 0: break
        except IndexError:
            raise ValueError('MTX: end of data') from None
        bits.pos = p
        symbol = code[a]
        self.update(a)
        return symbol


PRELOAD = 2 * 32 * 96 + 4 * 256


def unpack(data):
    """One LZCOMP block of an MTX font (version 3: a run-length bit first)."""
    bits = Bits(data)
    run_length = bits.bit()
    dist = AHuff(bits, 8); length_tree = AHuff(bits, 8)
    out_len = bits.value(24)
    ranges, dist_max = 1, 1 + (1 << 3) - 1
    while dist_max < out_len:
        ranges += 1; dist_max = 1 + (1 << (3 * ranges)) - 1
    dup2 = 256 + (1 << 3) * ranges; dup4 = dup2 + 1; dup6 = dup4 + 1
    sym = AHuff(bits, dup6 + 1)
    buf = bytearray()
    for k in range(32):
        for j in range(96): buf += bytes((k, j))
    for j in range(256): buf += bytes((j, j, j, j))
    assert len(buf) == PRELOAD
    end = PRELOAD + out_len
    while len(buf) < end:
        s = sym.read()
        if s < 256:
            buf.append(s)
        elif s == dup2:
            buf.append(buf[-2])
        elif s == dup4:
            buf.append(buf[-4])
        elif s == dup6:
            buf.append(buf[-6])
        else:
            bits_ = s - 256
            n_ranges = bits_ // 8 + 1; bits_ %= 8
            value = 0
            while True:
                done = (bits_ & 4) == 0
                value = (value << 2) | (bits_ & 3)
                if done: break
                bits_ = length_tree.read()
            length = value + 2
            distance = 0
            for _ in range(n_ranges):
                distance = (distance << 3) | dist.read()
            distance += 1
            if distance >= 512: length += 1
            start = len(buf) - distance - length + 1
            for j in range(length):
                buf.append(buf[start + j])
    out = bytes(buf[PRELOAD:end])
    return unrun(out) if run_length else out


def unrun(data):
    """Run-length layer of LZCOMP (RUNLENGTHCOMP, only its interface is in the submission): the first byte is the escape;
    escape, 0 is the escape byte itself; escape, n, v is v repeated n times."""
    if not data: return data
    escape, out, i = data[0], bytearray(), 1
    while i < len(data):
        b = data[i]; i += 1
        if b != escape:
            out.append(b); continue
        count = data[i]; i += 1
        if count == 0:
            out.append(escape)
        else:
            out += bytes((data[i],)) * count; i += 1
    return bytes(out)


def blocks(font_data):
    version = font_data[0]
    if version != 3:
        raise ValueError(f'MTX: version {version}')
    copy_limit = int.from_bytes(font_data[1:4], 'big')
    off2 = int.from_bytes(font_data[4:7], 'big'); off3 = int.from_bytes(font_data[7:10], 'big')
    return [unpack(font_data[10:off2]), unpack(font_data[off2:off3]), unpack(font_data[off3:])]


class Reader:
    def __init__(self, data, pos=0):
        self.data, self.pos = data, pos

    def u8(self):
        v = self.data[self.pos]; self.pos += 1; return v

    def u16(self):
        v = struct.unpack_from('>H', self.data, self.pos)[0]; self.pos += 2; return v

    def s16(self):
        v = struct.unpack_from('>h', self.data, self.pos)[0]; self.pos += 2; return v

    def take(self, n):
        v = self.data[self.pos:self.pos + n]
        if len(v) != n: raise ValueError('MTX: truncated data')
        self.pos += n; return v

    def u255(self):
        code = self.u8()
        if code == 253: return self.u16()
        if code == 255: return self.u8() + 253
        if code == 254: return self.u8() + 506
        return code

    def s255(self):
        code = self.u8()
        if code == 253: return self.s16()
        sign = 1
        if code == 250:
            sign = -1; code = self.u8()
        if code == 255: value = self.u8() + 250
        elif code == 254: value = self.u8() + 500
        else: value = code
        return value * sign


def cvt(data):
    r = Reader(data); out = []; value = 0
    for _ in range(r.u16()):
        code = r.u8()
        if code < 238: delta = code
        elif code >= 248: delta = r.u8() + (code - 248 + 1) * 238
        elif code >= 239: delta = -(r.u8() + (code - 239) * 238)
        else: delta = r.s16()
        value = (value + delta) & 0xFFFF
        out.append(value)
    return struct.pack(f'>{len(out)}H', *out)


def triplet(flag, r):
    """dx, dy of one point (the triplet encoding WOFF2 later took over, section 5.2 there)."""
    def sign(f, v): return v if f & 1 else -v
    if flag < 10:
        return 0, sign(flag, ((flag & 14) << 7) + r.u8())
    if flag < 20:
        return sign(flag, (((flag - 10) & 14) << 7) + r.u8()), 0
    if flag < 84:
        b0 = flag - 20; b1 = r.u8()
        return sign(flag, 1 + (b0 & 0x30) + (b1 >> 4)), sign(flag >> 1, 1 + ((b0 & 0x0C) << 2) + (b1 & 0x0F))
    if flag < 120:
        b0 = flag - 84; x = r.u8(); y = r.u8()
        return sign(flag, 1 + ((b0 // 12) << 8) + x), sign(flag >> 1, 1 + (((b0 % 12) >> 2) << 8) + y)
    if flag < 124:
        b1 = r.u8(); b2 = r.u8(); b3 = r.u8()
        return sign(flag, (b1 << 4) + (b2 >> 4)), sign(flag >> 1, ((b2 & 0x0F) << 8) + b3)
    b1, b2, b3, b4 = r.u8(), r.u8(), r.u8(), r.u8()
    return sign(flag, (b1 << 8) + b2), sign(flag >> 1, (b3 << 8) + b4)


def push_values(r, count):
    values = []
    while len(values) < count:
        code = r.data[r.pos]
        if code in (251, 252):
            r.pos += 1; a = values[-2]
            values += [a, r.s255(), a]
            if code == 252: values += [r.s255(), a]
        else:
            values.append(r.s255())
    if len(values) != count: raise ValueError('MTX: push data overruns its count')
    return values


def push_code(values):
    """TrueType instructions that push `values`: runs of bytes and of words, PUSHB/PUSHW up to 8, NPUSHB/NPUSHW above."""
    out = bytearray(); i = 0
    while i < len(values):
        word = not 0 <= values[i] <= 255
        j = i
        while j < len(values) and j - i < 255 and (not 0 <= values[j] <= 255) == word: j += 1
        run = values[i:j]; n = len(run)
        if n <= 8: out.append((0xB8 if word else 0xB0) + n - 1)
        else: out += bytes((0x41 if word else 0x40, n))
        out += struct.pack(f'>{n}h', *run) if word else bytes(run)
        i = j
    return bytes(out)


def glyph(r, push, code):
    """One glyph of the CTF glyf stream as TrueType bytes (b'' for an empty glyph)."""
    contours = r.s16()
    if contours == 0: return b''
    if contours == -1:
        bbox = r.take(8); start = r.pos
        while True:
            flags = r.u16(); r.u16()
            r.take(4 if flags & 1 else 2)
            if flags & 8: r.take(2)
            elif flags & 0x40: r.take(4)
            elif flags & 0x80: r.take(8)
            if not flags & 0x20: break
        components = r.data[start:r.pos]
        if flags & 0x100:
            pushes, size = r.u255(), r.u255()
            instructions = push_code(push_values(push, pushes)) + code.take(size)
            components += struct.pack('>H', len(instructions)) + instructions
        return struct.pack('>h', -1) + bbox + components
    bbox = None
    if contours == 0x7FFF:
        contours = r.s16(); bbox = r.take(8)
    # The first value is the end point of the first contour, the others are point counts of the next contours.
    ends = [r.u255()]
    for _ in range(contours - 1):
        ends.append(ends[-1] + r.u255())
    n = ends[-1] + 1
    flags = r.take(n)
    xs, ys = [], []
    for f in flags:
        dx, dy = triplet(f & 0x7F, r)
        xs.append(dx); ys.append(dy)
    pushes, size = r.u255(), r.u255()
    instructions = push_code(push_values(push, pushes)) + code.take(size)
    if bbox is None:
        px = py = 0; ax, ay = [], []
        for dx, dy in zip(xs, ys):
            px += dx; py += dy; ax.append(px); ay.append(py)
        bbox = struct.pack('>4h', min(ax), min(ay), max(ax), max(ay))
    out = bytearray(struct.pack('>h', contours) + bbox + struct.pack(f'>{contours}H', *ends))
    out += struct.pack('>H', len(instructions)) + instructions
    tflags, xbytes, ybytes = bytearray(), bytearray(), bytearray()
    for f, dx, dy in zip(flags, xs, ys):
        t = 0 if f & 0x80 else 1
        if dx == 0: t |= 0x10
        elif -255 <= dx <= 255: t |= 0x02 | (0x10 if dx > 0 else 0); xbytes.append(abs(dx))
        else: xbytes += struct.pack('>h', dx)
        if dy == 0: t |= 0x20
        elif -255 <= dy <= 255: t |= 0x04 | (0x20 if dy > 0 else 0); ybytes.append(abs(dy))
        else: ybytes += struct.pack('>h', dy)
        tflags.append(t)
    return bytes(out + tflags + xbytes + ybytes)


def checksum(data):
    data = data + b'\0' * (-len(data) % 4)
    return sum(struct.unpack(f'>{len(data) // 4}I', data)) & 0xFFFFFFFF


def to_ttf(block1, block2, block3):
    count = struct.unpack_from('>H', block1, 4)[0]
    tables = {}
    for i in range(count):
        tag, _, offset, length = struct.unpack_from('>4sIII', block1, 12 + 16 * i)
        tables[tag.decode('latin-1')] = block1[offset:offset + length]
    for tag in ('hdmx', 'VDMX', 'DSIG'):
        tables.pop(tag, None)
    if 'cvt ' in tables: tables['cvt '] = cvt(tables['cvt '])
    if 'glyf' in tables:
        glyphs = struct.unpack_from('>H', tables['maxp'], 4)[0]
        g, push, code = Reader(tables['glyf']), Reader(block2), Reader(block3)
        body, offsets = bytearray(), []
        for _ in range(glyphs):
            offsets.append(len(body))
            body += glyph(g, push, code)
            body += b'\0' * (-len(body) % 4)
        offsets.append(len(body))
        if push.pos != len(block2) or code.pos != len(block3):
            raise ValueError('MTX: glyph programs do not use their push data and instructions exactly')
        head = bytearray(tables['head'])
        long_loca = offsets[-1] > 0x1FFFE or struct.unpack_from('>h', head, 50)[0] == 1
        struct.pack_into('>h', head, 50, 1 if long_loca else 0)
        tables['head'] = bytes(head)
        tables['glyf'] = bytes(body)
        tables['loca'] = struct.pack(f'>{len(offsets)}I', *offsets) if long_loca else struct.pack(f'>{len(offsets)}H', *[o // 2 for o in offsets])
    head = bytearray(tables['head']); struct.pack_into('>I', head, 8, 0); tables['head'] = bytes(head)
    tags = sorted(tables)
    n = len(tags); power = 1 << (n.bit_length() - 1)
    out = bytearray(struct.pack('>IHHHH', 0x00010000, n, power * 16, power.bit_length() - 1, n * 16 - power * 16))
    offset = 12 + 16 * n; body = bytearray()
    for tag in tags:
        data = tables[tag]
        out += struct.pack('>4sIII', tag.encode('latin-1'), checksum(data), offset + len(body), len(data))
        body += data + b'\0' * (-len(data) % 4)
    font = bytearray(out + body)
    head_offset = struct.unpack_from('>I', font, 12 + 16 * tags.index('head') + 8)[0]
    struct.pack_into('>I', font, head_offset + 8, (0xB1B0AFBA - checksum(bytes(font))) & 0xFFFFFFFF)
    return bytes(font)


def decompress(font_data):
    """TrueType or OpenType bytes of the MTX data of an EOT (flag 4 of the EOT header)."""
    return to_ttf(*blocks(font_data))
