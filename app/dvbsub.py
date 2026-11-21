"""M3U Translator Subtitle - subtitles as pictures (PGS/.sup).

ffmpeg turns them into DVB subtitles (as on digital TV) inside the stream.
Kodi shows them with its own player; on/off with the subtitle button.
"""
import os
import struct

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
# v0.28: default DejaVu Sans (regular weight): with a thick outline like Kodi's own subtitles
FONT_NICE = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_fonts = {}


def font_file(style):
    for f in (style.get("font_file") or "", FONT_NICE, FONT_BOLD):
        if f and os.path.exists(f):
            return f
    return FONT_BOLD


def font(path, size):
    k = (path, size)
    if k not in _fonts:
        _fonts[k] = ImageFont.truetype(path, size)
    return _fonts[k]


# v0.37: break lines as in subtitling guidelines (e.g. public broadcasters):
# at logical places, keep word groups together, do not end on a small word.
# Word lists for Dutch and English (the most common target languages).
SMALL_END = {"de", "het", "een", "van", "in", "op", "met", "voor", "aan", "te", "en", "of",
             "om", "bij", "naar", "uit", "over", "door", "tot", "als", "dan", "maar", "want",
             "hun", "haar", "zijn", "mijn", "je", "jouw", "ons", "onze", "deze", "dit", "dat",
             "die", "er", "zo", "nog", "niet", "geen", "heel", "zeer",
             "the", "a", "an", "to", "and", "on", "at", "for", "with", "by", "from", "his",
             "her", "my", "your", "our", "their", "this", "that", "these", "those", "not",
             "no", "very", "is", "was"}
GOOD_START = {"en", "maar", "of", "want", "omdat", "dat", "die", "als", "wanneer", "toen",
              "zodat", "terwijl", "hoewel", "dus", "waardoor", "waarin", "wat", "wie", "om",
              "voordat", "nadat", "zonder", "tot", "totdat", "sinds", "doordat",
              "and", "but", "or", "because", "which", "who", "when", "while", "although",
              "so", "if", "before", "after", "until", "since", "without", "where"}

def _bare(w):
    return w.lower().strip(",.;:!?…\"'()")


def wrap(draw, text, fnt, maxw, stroke, max_chars=0):
    """Text in lines that fit (width in pixels and at most max_chars characters).
    Within what fits, the nicest break point is chosen."""
    words = text.split()
    if not words:
        return []

    def fits(ws):
        t = " ".join(ws)
        if max_chars and len(t) > max_chars:
            return False
        return draw.textlength(t, font=fnt) + 2 * stroke <= maxw

    lines, i, n = [], 0, len(words)
    while i < n:
        k = 1
        while i + k < n and fits(words[i:i + k + 1]):
            k += 1
        if i + k >= n:
            lines.append(" ".join(words[i:]))
            break
        full = len(" ".join(words[i:i + k]))
        best, best_score = k, None
        for j in range(k, 0, -1):
            part = " ".join(words[i:i + j])
            if j < k and len(part) < 0.55 * full:
                break
            last, nxt = words[i + j - 1], words[i + j]
            score = -0.25 * (k - j)                  # rather a full line
            if last[-1:] in ",.;:!?…":
                score += 3                           # after punctuation: ideal
            if _bare(nxt) in GOOD_START:
                score += 2                           # new line starts with a clause/conjunction
            if _bare(last) in SMALL_END and last[-1:] not in ",.;:!?…":
                score -= 3                           # do not end on 'the', 'of', 'de', 'van'...
            if best_score is None or score > best_score:
                best, best_score = j, score
        lines.append(" ".join(words[i:i + best]))
        i += best
    return lines


def split_lines(text, W, H, style):
    """v0.32: in which lines does this text fall on screen? (same sizes as render)"""
    scale = H / 1080.0
    size = max(14, int(style["font_size"] * scale))
    stroke = max(1, int(round(style["outline"] * scale))) if style["outline"] > 0 else 0
    pad = max(6, int(14 * scale))
    fnt = font(font_file(style), size)
    tmp = ImageDraw.Draw(Image.new("L", (1, 1)))
    return wrap(tmp, text, fnt, int(W * 0.86) - 2 * pad, stroke, style.get("max_chars", 0))


def render(blocks, W, H, style):
    """blocks: list (text, colour RGB). Returns (x, y, P image with index 0 = transparent)."""
    scale = H / 1080.0
    size = max(14, int(style["font_size"] * scale))
    stroke = max(1, int(round(style["outline"] * scale))) if style["outline"] > 0 else 0
    pad = max(6, int(14 * scale))
    fnt = font(font_file(style), size)
    tmp = ImageDraw.Draw(Image.new("L", (1, 1)))
    maxw = int(W * 0.86)
    lines = []
    for text, col in blocks:
        for ln in wrap(tmp, text, fnt, maxw - 2 * pad, stroke, style.get("max_chars", 0)):
            lines.append((ln, col))
    lines = lines[-style.get("max_lines", 3):]     # oldest lines drop first
    if not lines:
        return None
    return _draw(lines, fnt, tmp, stroke, pad, W, H, scale, style)


MAX_RLE = 48000     # a DVB packet may be at most 64 KB; stay well below


def render_fit(blocks, W, H, style):
    """Like render, but drops the oldest line until the picture is small enough."""
    st = dict(style)
    while True:
        item = render(blocks, W, H, st)
        if item is None or st.get("max_lines", 3) <= 1:
            return item
        if len(rle(item[2][0])) <= MAX_RLE:
            return item
        st["max_lines"] = st.get("max_lines", 3) - 1


def _draw(lines, fnt, tmp, stroke, pad, W, H, scale, style):
    """v0.28: letters with a thick, round black outline and soft edges (anti-aliasing).
    Outline and letter are drawn separately and then merged with a fixed palette,
    so that the edges do not get 'steps' from the 16-colour DVB palette."""
    asc, desc = fnt.getmetrics()
    lh = asc + desc + stroke * 2
    gap = int(lh * 0.04)
    tw = int(max(tmp.textlength(t, font=fnt) for t, _ in lines)) + 2 * stroke
    bw, bh = min(W, tw + 2 * pad), len(lines) * lh + (len(lines) - 1) * gap + 2 * pad
    bw += bw % 2
    bh += bh % 2
    edge = Image.new("L", (bw, bh), 0)          # rand (+ letter)
    de = ImageDraw.Draw(edge)
    fills = {}                                   # colour -> mask of the letter
    y = pad
    for t, col in lines:
        x = (bw - de.textlength(t, font=fnt)) / 2
        if stroke:
            de.text((x, y + stroke), t, font=fnt, fill=255,
                    stroke_width=stroke, stroke_fill=255)
        else:
            de.text((x, y + stroke), t, font=fnt, fill=255)
        m = fills.setdefault(tuple(col), Image.new("L", (bw, bh), 0))
        ImageDraw.Draw(m).text((x, y + stroke), t, font=fnt, fill=255)
        y += lh + gap
    box = 0
    if style["box"] > 0:
        box = int(255 * min(100, style["box"]) / 100)
    margin = int(style["margin"] * scale)
    x0 = (W - bw) // 2
    y0 = margin if style["position"] == "top" else H - margin - bh
    return max(0, x0), max(0, y0), compose(edge, fills, box, pad)


A_STEPS = (64, 128, 192, 255)      # black outline: soft outside in 4 steps


def compose(edge, fills, box=0, pad=0):
    """Fixed palette: 0 = transparent, 1-4 = black with increasing opacity (outline),
    then per text colour 3 blend steps black->colour (soft inside)."""
    e = np.array(edge, dtype=np.int32)
    h, w = e.shape
    if box:                                    # old: dark box behind the text
        e = np.maximum(e, box)
    palette = [(0, 0, 0, 0)] + [(0, 0, 0, a) for a in A_STEPS]
    if box:
        palette[1:] = [(0, 0, 0, box)] + [(0, 0, 0, a) for a in A_STEPS[1:]]
    idx = np.zeros((h, w), dtype=np.uint8)
    lev = np.clip(np.rint(e / 255.0 * 4), 0, 4).astype(np.uint8)
    idx[lev > 0] = lev[lev > 0]
    for col, m in list(fills.items())[:3]:
        base = len(palette)
        for k in (1, 2, 3):
            f = k / 3.0
            palette.append(tuple(int(round(c * f)) for c in col) + (255,))
        fa = np.array(m, dtype=np.int32)
        fl = np.clip(np.rint(fa / 255.0 * 3), 0, 3).astype(np.uint8)
        sel = fl > 0
        idx[sel] = (base - 1 + fl[sel]).astype(np.uint8)
    return idx, palette


# ---------------------------------------------------------------- write PGS
def rgb_to_ycrcb(r, g, b):
    # BT.709, video range
    y = 16 + 0.1826 * r + 0.6142 * g + 0.0620 * b
    cb = 128 - 0.1006 * r - 0.3386 * g + 0.4392 * b
    cr = 128 + 0.4392 * r - 0.3989 * g - 0.0403 * b
    c = lambda v: int(max(16, min(240, round(v))))  # noqa: E731
    return c(y), c(cr), c(cb)


def rle(idx):
    out = bytearray()
    for row in idx:
        n = len(row)
        edges = np.flatnonzero(np.diff(row)) + 1
        starts = np.concatenate(([0], edges))
        ends = np.concatenate((edges, [n]))
        for s, e in zip(starts.tolist(), ends.tolist()):
            c, L = int(row[s]), e - s
            while L > 0:
                l = min(L, 16383)
                if c == 0:
                    out += bytes([0, l]) if l < 64 else bytes([0, 0x40 | (l >> 8), l & 0xFF])
                elif l < 3:
                    out += bytes([c]) * l
                elif l < 64:
                    out += bytes([0, 0x80 | l, c])
                else:
                    out += bytes([0, 0xC0 | (l >> 8), l & 0xFF, c])
                L -= l
        out += b"\x00\x00"
    return bytes(out)


def seg(pts, typ, data):
    p = int(round(pts * 90000)) & 0xFFFFFFFF
    return b"PG" + struct.pack(">IIBH", p, 0, typ, len(data)) + data


def display_set(t, W, H, comp, item):
    out = b""
    if item is None:            # clear
        pcs = struct.pack(">HHBHBBBB", W, H, 0x10, comp, 0x00, 0, 0, 0)
        out += seg(t, 0x16, pcs)
        out += seg(t, 0x17, struct.pack(">BBHHHH", 1, 0, 0, 0, W, H))
        return out + seg(t, 0x80, b"")
    x, y, (idx, palette) = item
    h, w = idx.shape
    pcs = struct.pack(">HHBHBBBB", W, H, 0x10, comp, 0x80, 0, 0, 1)
    pcs += struct.pack(">HBBHH", 0, 0, 0, x, y)
    out += seg(t, 0x16, pcs)
    out += seg(t, 0x17, struct.pack(">BBHHHH", 1, 0, x, y, w, h))
    pds = bytes([0, 0])
    for i, (r, g, b, a) in enumerate(palette):
        Y, Cr, Cb = rgb_to_ycrcb(r, g, b)
        pds += bytes([i, Y, Cr, Cb, a])
    out += seg(t, 0x14, pds)
    data = rle(idx)
    total = len(data) + 4
    body = struct.pack(">HH", w, h) + data
    first = True
    while body or first:
        room = 65535 - (11 if first else 4)
        part, body = body[:room], body[room:]
        flag = (0x80 if first else 0) | (0x40 if not body else 0)
        head = struct.pack(">HBB", 0, 0, flag)
        if first:
            head += total.to_bytes(3, "big")
        out += seg(t, 0x15, head + part)
        first = False
    return out + seg(t, 0x80, b"")


def write_sup(path, events, W, H, style, t_clear_at=None, t_end=None):
    """events: (start, end, [(text, colour)]) in absolute seconds (stream PTS).
    Every text gets its own 'clear' at the end. Needed: ffmpeg (-fix_sub_duration)
    uses it as the end of the text. A last text without 'clear' is dropped by ffmpeg
    (tested in v0.31). t_end is not used (only for clarity)."""
    out, comp = b"", 0
    for a, b, blocks in events:
        item = render_fit(blocks, W, H, style)
        if item is None:
            continue
        out += display_set(a, W, H, comp, item)
        comp += 1
        out += display_set(b, W, H, comp, None)
        comp += 1
    if not out:                 # always a subtitle stream, even without text
        t = t_clear_at or 0.0
        out = display_set(t, W, H, 0, None)
    with open(path, "wb") as f:
        f.write(out)
