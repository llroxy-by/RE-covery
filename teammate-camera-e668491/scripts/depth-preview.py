# depth-preview.py - turn a 16-bit grayscale depth PNG into an 8-bit preview + stats.
#
# WHY PURE STDLIB
#   The frames come from robot-cam.ps1, which encodes depth with its own zlib+struct
#   PNG writer using filter type 0 on every row and 16-bit BIG-endian samples. No PIL,
#   no numpy, no cv2 on this PC - and none are needed to reverse exactly that layout.
#
# USAGE
#   python3 depth-preview.py <file16.png> [more.png ...]
# Writes <name>_preview.png next to each input and prints one stats line per file.

import sys, zlib, struct, array, os

def read_png(path):
    raw = open(path, 'rb').read()
    if raw[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('not a PNG: ' + path)
    pos, idat, w = 8, bytearray(), None
    while pos < len(raw):
        ln = struct.unpack('>I', raw[pos:pos + 4])[0]
        tag = raw[pos + 4:pos + 8]
        data = raw[pos + 8:pos + 8 + ln]
        if tag == b'IHDR':
            w, h, depth, ctype, _, _, inter = struct.unpack('>IIBBBBB', data)
            if inter != 0:
                raise ValueError('interlaced PNG not supported')
        elif tag == b'IDAT':
            idat += data
        elif tag == b'IEND':
            break
        pos += 12 + ln
    return w, h, depth, ctype, zlib.decompress(bytes(idat))

def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc: return a
    if pb <= pc: return b
    return c

def unfilter(buf, w, h, bpp):
    stride = w * bpp
    out = bytearray(h * stride)
    prev = bytearray(stride)
    i = 0
    for y in range(h):
        f = buf[i]; i += 1
        row = bytearray(buf[i:i + stride]); i += stride
        if f == 1:
            for x in range(bpp, stride): row[x] = (row[x] + row[x - bpp]) & 0xFF
        elif f == 2:
            for x in range(stride): row[x] = (row[x] + prev[x]) & 0xFF
        elif f == 3:
            for x in range(stride):
                left = row[x - bpp] if x >= bpp else 0
                row[x] = (row[x] + ((left + prev[x]) >> 1)) & 0xFF
        elif f == 4:
            for x in range(stride):
                left = row[x - bpp] if x >= bpp else 0
                ul = prev[x - bpp] if x >= bpp else 0
                row[x] = (row[x] + _paeth(left, prev[x], ul)) & 0xFF
        out[y * stride:(y + 1) * stride] = row
        prev = row
    return out

def write_gray8(path, w, h, rows):
    def chunk(tag, data):
        body = tag + data
        return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)
    raw = b''.join(b'\x00' + bytes(r) for r in rows)
    ihdr = struct.pack('>IIBBBBB', w, h, 8, 0, 0, 0, 0)
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
                + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))

def preview(path):
    w, h, depth, ctype, comp = read_png(path)
    bpp = 2 if depth == 16 else 1
    px = unfilter(comp, w, h, bpp)
    n = w * h
    if depth == 16:
        a = array.array('H'); a.frombytes(px)
        if sys.byteorder == 'little': a.byteswap()
        vals = list(a)
    else:
        vals = list(px)
    nz = [v for v in vals if v > 0]
    zero_pct = 100.0 * (n - len(nz)) / n if n else 0.0
    # Percentile clamp + gamma. A plain 2%..98% linear stretch renders almost black:
    # the far-field noise tail (tens of metres) eats the top of the range while the
    # useful 1-3 m of a wrist camera sits in the bottom decile. 2%..90% with gamma 0.65
    # puts a typical median surface around mid-grey, which is what a human can judge.
    if nz:
        s = sorted(nz)
        lo, hi = s[int(len(s) * 0.02)], s[int(len(s) * 0.90)]
        mid, mx = s[len(s) // 2], s[-1]
    else:
        lo = hi = mid = mx = 0
    span = max(1, hi - lo)
    rows = []
    for y in range(h):
        row = bytearray(w)
        base = y * w
        for x in range(w):
            v = vals[base + x]
            if v == 0:
                row[x] = 0
            else:
                t = (v - lo) / span
                if t <= 0:
                    row[x] = 0
                elif t >= 1:
                    row[x] = 255
                else:
                    row[x] = int((t ** 0.65) * 255)
        rows.append(row)
    out = os.path.splitext(path)[0] + '_preview.png'
    write_gray8(out, w, h, rows)
    print('%s\n  %dx%d  depth=%d  valid=%.1f%%  p2=%d p50=%d p98=%d max=%d (raw)'
          % (os.path.basename(path), w, h, depth, 100 - zero_pct, lo, mid, hi, mx))
    print('  preview -> ' + os.path.basename(out))
    return out

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('usage: python3 depth-preview.py <file.png> [...]')
        sys.exit(2)
    for p in sys.argv[1:]:
        preview(p)
