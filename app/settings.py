"""Web page /settings: change the subtitle style live.

Changes are stored in DATA_DIR/settings.json and take priority over the values in
.env. They work right away for new subtitles (because of the buffer you see them in
Kodi after about half a minute). 'Back to .env values' removes the web values again.
"""
import base64
import glob
import hmac
import io
import json
import os
import threading

# key in CFG -> (kind, min, max, label, explanation)
FIELDS = {
    "font_file": ("font", None, None, "Font", "Own .ttf/.otf files: put them in data/fonts/."),
    "font_size": ("int", 20, 120, "Font size", "On a 1080-line picture. 54-60 is good, 70+ is large."),
    "outline": ("int", 0, 20, "Outline around the letters", "0 = none, 2 = thin, 8 = thick."),
    "box": ("int", 0, 100, "Dark box behind the text", "0 = no box, 50 = half transparent, 100 = black."),
    "new_color": ("color", None, None, "Colour of the newest line", ""),
    "old_color": ("color", None, None, "Colour of the older line", "Only when more than one sentence is on screen."),
    "position": ("choice", ["bottom", "top"], None, "Position", "Bottom or top of the picture."),
    "margin": ("int", 0, 400, "Distance to the edge", "In pixels on 1080 lines."),
    "sub_max_lines": ("int", 1, 4, "Max. lines on screen", "Subtitling guideline: 2."),
    "lines": ("int", 1, 3, "Sentences on screen at once", "1 = like in films; 2 = previous sentence stays above."),
    "sub_max_chars": ("int", 0, 80, "Characters per line", "Guideline: 42. 0 = screen width only."),
    "read_cps": ("float", 6, 25, "Reading speed (characters per second)", "Lower = longer on screen, but further behind the speech."),
    "read_min": ("float", 0.5, 5, "Minimum time on screen (seconds)", "Even a short word stays this long."),
    "read_max_delay": ("float", 0, 20, "Max. behind the speech (seconds)", ""),
    "hold": ("float", 1, 15, "Max. on screen without a new sentence (seconds)", ""),
    "show_original": ("bool", None, None, "Show original text", "Small and yellow below the translation."),
    # --- languages ---
    "src_lang": ("lang", "auto", None, "Language of the channels", "Automatic = detect per channel."),
    "dst_lang": ("lang", None, None, "Subtitle language", "The language you want to read."),
    "lang_allowlist": ("langs", None, None, "Only accept these languages",
                       "Codes with commas, e.g. en,fr,de,es. Empty = all languages."),
    # --- speech recognition ---
    "asr_engine": ("engine", None, None, "Speech recognition",
                   "http = server with an OpenAI-compatible API. 'command' and 'plugin' can only be chosen when set in .env."),
    "asr_url": ("url", None, None, "Server address", "Usually ends in /v1, e.g. http://192.168.1.20:8000/v1"),
    "asr_model": ("text", None, None, "Model name", "As the server knows it. Empty = server default."),
    "asr_key": ("secret", None, None, "API key", "Only when the server asks for one."),
    # --- translator ---
    "tr_engine": ("choice", ["libretranslate", "llm"], None, "Translator",
                  "libretranslate = LibreTranslate or the NLLB service; llm = language model (OpenAI-compatible chat API)."),
    "lt_url": ("url", None, None, "Translator address", "E.g. http://nllb:5000 or http://192.168.1.20:5000"),
    "lt_key": ("secret", None, None, "API key", "Only when the server asks for one."),
    "llm_url": ("url", None, None, "Language model address", "Usually ends in /v1, e.g. http://192.168.1.20:11434/v1"),
    "llm_model": ("text", None, None, "Model name", "As the server knows it."),
    "llm_key": ("secret", None, None, "API key", "Only when the server asks for one."),
}

# Fields that change the speech recognition or the translator (applied without restart).
ASR_KEYS = {"asr_engine", "asr_url", "asr_model", "asr_key"}
TRANSLATOR_KEYS = {"tr_engine", "lt_url", "lt_key", "llm_url", "llm_model", "llm_key", "dst_lang"}
LANGUAGE_KEYS = {"src_lang", "lang_allowlist"}
SECRET_KEYS = {k for k, f in FIELDS.items() if f[0] == "secret"}

_LOCK = threading.Lock()
_ENV_DEFAULTS = {}
_FONT_DIRS = ["/usr/share/fonts", "/usr/local/share/fonts"]


def _file(cfg):
    return os.path.join(cfg["model_dir"], "settings.json")


def font_dirs(cfg):
    return _FONT_DIRS + [os.path.join(cfg["model_dir"], "fonts")]


def list_fonts(cfg):
    """[(path, name)] of all fonts, sorted by name."""
    from PIL import ImageFont
    out, seen = [], set()
    for d in font_dirs(cfg):
        for ext in ("ttf", "otf", "TTF", "OTF"):
            for p in glob.glob(os.path.join(d, "**", f"*.{ext}"), recursive=True):
                try:
                    fam, style = ImageFont.truetype(p, 20).getname()
                    name = f"{fam} {style}".strip()
                except Exception:  # noqa: BLE001
                    continue
                if name in seen:
                    continue
                seen.add(name)
                out.append((p, name))
    return sorted(out, key=lambda x: x[1].lower())


def clean(key, value, cfg):
    """Check and convert; ValueError for a wrong value."""
    kind, lo, hi, label, _ = FIELDS[key]
    if kind == "int":
        v = int(float(value))
        if not lo <= v <= hi:
            raise ValueError(f"{label}: between {lo} and {hi}")
        return v
    if kind == "float":
        v = round(float(value), 2)
        if not lo <= v <= hi:
            raise ValueError(f"{label}: between {lo} and {hi}")
        return v
    if kind == "bool":
        return value in (True, 1, "1", "true", "on", "yes")
    if kind == "color":
        v = str(value).strip().lstrip("#").upper()
        if len(v) != 6 or any(c not in "0123456789ABCDEF" for c in v):
            raise ValueError(f"{label}: colour as RRGGBB")
        return v
    if kind == "choice":
        if value not in lo:
            raise ValueError(f"{label}: choose {', '.join(lo)}")
        return value
    if kind in ("text", "secret", "url"):
        v = str(value or "").strip()
        if len(v) > 300 or any(c in v for c in "\r\n"):
            raise ValueError(f"{label}: too long")
        if kind == "url":
            v = v.rstrip("/")
            if v and not v.lower().startswith(("http://", "https://")):
                raise ValueError(f"{label}: must start with http:// or https://")
        return v
    if kind == "lang":
        from languages import NAMES
        v = str(value or "").strip().lower()
        if (lo and v == lo) or v in NAMES:
            return v
        raise ValueError(f"{label}: unknown language")
    if kind == "langs":
        from languages import NAMES
        raw = value if isinstance(value, (set, list, tuple)) else str(value or "").split(",")
        codes = {str(x).strip().lower() for x in raw if str(x).strip()}
        bad = sorted(c for c in codes if c not in NAMES)
        if bad:
            raise ValueError(f"{label}: unknown code(s) {', '.join(bad)}")
        return codes or None
    if kind == "engine":
        v = str(value or "").strip().lower()
        if v not in engine_options(cfg):
            raise ValueError(f"{label}: choose {', '.join(engine_options(cfg))}")
        return v
    if kind == "font":
        v = str(value or "")
        if v == "":
            return ""
        real = os.path.realpath(v)
        if not any(real.startswith(os.path.realpath(d) + os.sep) for d in font_dirs(cfg)):
            raise ValueError(f"{label}: unknown font")
        if not os.path.isfile(real):
            raise ValueError(f"{label}: file does not exist")
        return v
    raise ValueError(key)


def engine_options(cfg):
    """http always; command/plugin only when set up in .env (no programs from the web page)."""
    opts = ["http"]
    if cfg.get("asr_command"):
        opts.append("command")
    if cfg.get("asr_plugin"):
        opts.append("plugin")
    return opts


def _plain(k, v):
    """Value that can go into JSON."""
    if k == "lang_allowlist":
        return ",".join(sorted(v)) if v else ""
    return v


def current(cfg, hide_secrets=True):
    out = {k: _plain(k, cfg[k]) for k in FIELDS}
    if hide_secrets:
        for k in SECRET_KEYS:
            out[k] = ""
    return out


def merged(cfg, values):
    """Copy of cfg with the (checked) values from the web page; empty secrets keep their value.
    Used for testing a connection before saving."""
    c = dict(cfg)
    for k, v in values.items():
        if k not in FIELDS or (k in SECRET_KEYS and not v):
            continue
        try:
            c[k] = clean(k, v, cfg)
        except ValueError:
            pass
    return c


def load(cfg, log):
    """At start: remember the .env values and put the web values on top."""
    with _LOCK:
        if not _ENV_DEFAULTS:
            _ENV_DEFAULTS.update({k: cfg[k] for k in FIELDS})
        try:
            with open(_file(cfg), encoding="utf-8") as f:
                saved = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:  # noqa: BLE001
            log(f"NOTE: {_file(cfg)} is not readable ({e}); using .env values")
            return
        used = []
        for k, v in saved.items():
            if k in FIELDS:
                try:
                    cfg[k] = clean(k, v, cfg)
                    used.append(k)
                except ValueError as e:
                    log(f"NOTE: web setting skipped: {e}")
        if used:
            log(f"Web settings loaded ({len(used)}): " + ", ".join(sorted(used)))


def save(cfg, values, log, apply=None):
    """Check, apply and store new values. Returns the errors.
    apply(changed_keys) makes engine/language changes active; it returns an error text
    (then everything is put back) or None."""
    errors, good = {}, {}
    for k, v in values.items():
        if k not in FIELDS:
            continue
        if k in SECRET_KEYS and not v:
            continue                       # empty = keep the stored key
        try:
            good[k] = clean(k, v, cfg)
        except ValueError as e:
            errors[k] = str(e)
    if errors:
        return errors
    with _LOCK:
        if not _ENV_DEFAULTS:
            _ENV_DEFAULTS.update({k: cfg[k] for k in FIELDS})
        old = {k: cfg[k] for k in good}
        cfg.update(good)
        keys = {k for k in good if good[k] != old[k]}
        if apply and keys:
            err = apply(keys)
            if err:
                cfg.update(old)
                return {"_": err}
        changed = {k: _plain(k, cfg[k]) for k in FIELDS if cfg[k] != _ENV_DEFAULTS.get(k)}
        path = _file(cfg)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(changed, f, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    shown = {k: ("***" if k in SECRET_KEYS and v else v) for k, v in changed.items()}
    log("Web settings saved: " + (", ".join(f"{k}={v}" for k, v in sorted(shown.items()))
                                  or "everything as .env"))
    return {}


def reset(cfg, log, apply=None):
    with _LOCK:
        if _ENV_DEFAULTS:
            keys = {k for k in _ENV_DEFAULTS if cfg[k] != _ENV_DEFAULTS[k]}
            cfg.update(_ENV_DEFAULTS)
            if apply and keys:
                apply(keys)
        try:
            os.remove(_file(cfg))
        except FileNotFoundError:
            pass
    log("Web settings cleared: back to .env")


def preview_png(cfg, values, text_new, text_old=""):
    """Preview picture (PNG, 1280x720) with the same drawing functions as the DVB subtitle."""
    import numpy as np
    from PIL import Image
    import dvbsub
    st = dict(current(cfg))
    for k, v in values.items():
        if k in FIELDS:
            try:
                st[k] = clean(k, v, cfg)
            except ValueError:
                pass
    W, H = 1280, 720
    # background: sky and ground, so you can see how the outline works on light and dark
    y = np.linspace(0, 1, H)[:, None]
    x = np.linspace(0, 1, W)[None, :]
    sky = np.stack([90 + 80 * y + 0 * x, 140 + 60 * y + 0 * x, 200 + 30 * y + 0 * x], -1)
    ground = np.stack([60 + 40 * x + 0 * y, 90 + 30 * x + 0 * y, 50 + 0 * x + 0 * y], -1)
    bg = np.where((y > 0.62)[..., None], ground, sky)
    bg[int(H * 0.45):int(H * 0.95), int(W * 0.08):int(W * 0.30)] = (235, 235, 230)   # light wall
    img = Image.fromarray(bg.clip(0, 255).astype("uint8"), "RGB").convert("RGBA")

    def rgb(h):
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    blocks = []
    if text_old and st["lines"] > 1:
        blocks.append((text_old, rgb(st["old_color"])))
    blocks.append((text_new, rgb(st["new_color"])))
    if st["show_original"]:
        blocks.append(("Original text in the language of the channel.", (255, 229, 0)))
    style = {"font_size": st["font_size"], "outline": st["outline"], "box": st["box"],
             "font_file": st["font_file"], "margin": st["margin"], "position": st["position"],
             "max_lines": max(1, st["sub_max_lines"]) + (1 if st["show_original"] else 0),
             "max_chars": max(0, st["sub_max_chars"])}
    item = dvbsub.render(blocks, W, H, style)
    if item is not None:
        x0, y0, (idx, pal) = item
        lut = np.array(pal + [(0, 0, 0, 0)] * (256 - len(pal)), dtype=np.uint8)
        sub = Image.fromarray(lut[idx], "RGBA")
        img.alpha_composite(sub, (x0, y0))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def check_auth(cfg, header):
    """True when there is no password, or when the correct password was given
    (the user name does not matter)."""
    pw = cfg.get("web_password") or ""
    if not pw:
        return True
    if not header or not header.startswith("Basic "):
        return False
    try:
        user_pw = base64.b64decode(header[6:]).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return False
    given = user_pw.split(":", 1)[1] if ":" in user_pw else user_pw
    return hmac.compare_digest(given.encode(), pw.encode())


def page_html():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "web", "index.html"), encoding="utf-8") as f:
        return f.read()


def api_info(cfg):
    fields = []
    from languages import NAMES
    for k, (kind, lo, hi, label, help_) in FIELDS.items():
        f = {"key": k, "kind": kind, "label": label, "help": help_,
             "env": "" if k in SECRET_KEYS else _plain(k, _ENV_DEFAULTS.get(k, cfg[k]))}
        if kind in ("int", "float"):
            f.update(min=lo, max=hi)
        if kind == "choice":
            f["options"] = lo
        if kind == "engine":
            f["options"] = engine_options(cfg)
        if kind == "lang":
            f["auto"] = bool(lo)
        if kind == "secret":
            f["is_set"] = bool(cfg[k])
        fields.append(f)
    langs = sorted(({"code": c, "name": n.title()} for c, n in NAMES.items()), key=lambda x: x["name"])
    return {"fields": fields, "values": current(cfg), "languages": langs,
            "fonts": [{"path": "", "name": "Default (DejaVu Sans)"}]
            + [{"path": p, "name": n} for p, n in list_fonts(cfg)]}
