"""
NLLB translator for M3U Translator Subtitle (optional) - LibreTranslate-compatible, runs on the GPU.

Speaks the same API as LibreTranslate:
  POST /translate   {"q": "text" or ["text", ...], "source": "en" or "auto", "target": "nl"}
  GET  /languages
  POST /detect
  GET  /health
So the program needs no changes: only the address.
"""
import os
import re
import sys
import time
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

MODEL = os.getenv("NLLB_MODEL", "facebook/nllb-200-distilled-1.3B")
DEVICE = os.getenv("NLLB_DEVICE", "cuda")
COMPUTE = os.getenv("NLLB_COMPUTE", "int8")
BEAM = int(os.getenv("NLLB_BEAM", "4"))
MODELS_DIR = Path(os.getenv("MODELS_DIR", "/models"))
CT2_DIR = MODELS_DIR / ("ct2-" + MODEL.replace("/", "--") + "-int8")
# v0.20: translate a piece with several short sentences in one go (more coherence)
WHOLE_MAX_CHARS = int(os.getenv("NLLB_WHOLE_MAX_CHARS", "220"))
GLOSSARY = Path(os.getenv("NLLB_GLOSSARY", "/data/glossary.txt"))

# Plain language codes (as Whisper and LibreTranslate use them) -> NLLB codes
LANG = {
    "en": "eng_Latn", "nl": "nld_Latn", "fr": "fra_Latn", "de": "deu_Latn",
    "es": "spa_Latn", "it": "ita_Latn", "pt": "por_Latn", "pl": "pol_Latn",
    "tr": "tur_Latn", "ar": "arb_Arab", "ru": "rus_Cyrl", "uk": "ukr_Cyrl",
    "ja": "jpn_Jpan", "zh": "zho_Hans", "zt": "zho_Hant", "ko": "kor_Hang",
    "hi": "hin_Deva", "sv": "swe_Latn", "da": "dan_Latn", "no": "nob_Latn",
    "nb": "nob_Latn", "nn": "nno_Latn", "fi": "fin_Latn", "el": "ell_Grek",
    "cs": "ces_Latn", "ro": "ron_Latn", "hu": "hun_Latn", "bg": "bul_Cyrl",
    "hr": "hrv_Latn", "sr": "srp_Cyrl", "bs": "bos_Latn", "sk": "slk_Latn",
    "sl": "slv_Latn", "ga": "gle_Latn", "cy": "cym_Latn", "ca": "cat_Latn",
    "eu": "eus_Latn", "gl": "glg_Latn", "he": "heb_Hebr", "fa": "pes_Arab",
    "id": "ind_Latn", "ms": "zsm_Latn", "th": "tha_Thai", "vi": "vie_Latn",
    "ur": "urd_Arab", "bn": "ben_Beng", "ta": "tam_Taml", "te": "tel_Telu",
    "af": "afr_Latn", "sq": "als_Latn", "lt": "lit_Latn", "lv": "lvs_Latn",
    "et": "est_Latn", "is": "isl_Latn", "mk": "mkd_Cyrl", "be": "bel_Cyrl",
    "ka": "kat_Geor", "hy": "hye_Armn", "az": "azj_Latn", "kk": "kaz_Cyrl",
    "sw": "swh_Latn", "tl": "tgl_Latn", "pa": "pan_Guru", "mt": "mlt_Latn",
    "lb": "ltz_Latn",
    "so": "som_Latn", "am": "amh_Ethi", "ne": "npi_Deva", "mr": "mar_Deva",
    "gu": "guj_Gujr", "kn": "kan_Knda", "ml": "mal_Mlym", "si": "sin_Sinh",
    "my": "mya_Mymr", "km": "khm_Khmr", "lo": "lao_Laoo", "mn": "khk_Cyrl",
    "uz": "uzn_Latn", "ps": "pbt_Arab", "yo": "yor_Latn", "ha": "hau_Latn",
    "zu": "zul_Latn", "xh": "xho_Latn", "ht": "hat_Latn", "jw": "jav_Latn",
    "su": "sun_Latn", "tg": "tgk_Cyrl", "tk": "tuk_Latn", "eo": "epo_Latn",
}
NAMES = {
    "en": "English", "nl": "Dutch", "fr": "French", "de": "German", "es": "Spanish",
    "it": "Italian", "pt": "Portuguese", "pl": "Polish", "tr": "Turkish",
    "ar": "Arabic", "ru": "Russian", "uk": "Ukrainian", "ja": "Japanese",
    "zh": "Chinese", "ko": "Korean",
}

_translator = None
_tokenizer = None
_lock = threading.Lock()


def log(msg):
    print(time.strftime("%H:%M:%S"), f"[nllb] {msg}", flush=True)


def nllb_code(code):
    """'en' -> 'eng_Latn'. Also accepts existing NLLB codes."""
    if not code:
        return None
    code = code.strip()
    if "_" in code:
        return code
    code = code.lower()
    if code in LANG:
        return LANG[code]
    base = code.split("-")[0]
    return LANG.get(base)


def detect(text):
    try:
        from langdetect import detect_langs
        best = detect_langs(text)[0]
        lang = best.lang.split("-")[0]
        if lang == "zh" and "tw" in best.lang.lower():
            lang = "zt"
        return lang, round(best.prob * 100, 1)
    except Exception:
        return "en", 0.0


_SPLIT = re.compile(r"(?<=[.!?…。！？])\s+")


def split_sentences(text):
    parts = [p.strip() for p in _SPLIT.split(text.strip()) if p.strip()]
    return parts or [text.strip()]


def ensure_model():
    """First time: download the model and convert it to CTranslate2 (int8). Reuse it afterwards."""
    if (CT2_DIR / "model.bin").exists():
        return
    log(f"First start: downloading and converting {MODEL}. This takes 10-20 minutes, once.")
    from ctranslate2.converters import TransformersConverter
    tmp = CT2_DIR.with_name(CT2_DIR.name + ".busy")
    t = time.time()
    TransformersConverter(MODEL, low_cpu_mem_usage=True).convert(
        str(tmp), quantization="int8", force=True)
    if CT2_DIR.exists():
        import shutil
        shutil.rmtree(CT2_DIR)
    tmp.rename(CT2_DIR)
    log(f"Conversion done in {time.time() - t:.0f} s")


def load():
    global _translator, _tokenizer
    import ctranslate2
    from transformers import AutoTokenizer
    ensure_model()
    t = time.time()
    _tokenizer = AutoTokenizer.from_pretrained(MODEL)
    _translator = ctranslate2.Translator(str(CT2_DIR), device=DEVICE, compute_type=COMPUTE)
    log(f"Model loaded in {time.time() - t:.1f} s ({DEVICE}/{COMPUTE}, beam {BEAM})")
    t = time.time()
    out = translate_texts(["Every component of the engine is built right here."], "en", "nl")
    log(f"Test: {out[0]!r} ({time.time() - t:.2f} s)")


def _run(pieces, src, tgt):
    """Translate separate pieces of text in one GPU batch."""
    if not pieces:
        return []
    with _lock:
        _tokenizer.src_lang = src
        batch = [_tokenizer.convert_ids_to_tokens(_tokenizer.encode(p)) for p in pieces]
        out = _translator.translate_batch(
            batch,
            target_prefix=[[tgt]] * len(batch),
            beam_size=BEAM,
            max_decoding_length=256,
            repetition_penalty=1.1,
        )
        return [
            _tokenizer.decode(_tokenizer.convert_tokens_to_ids(r.hypotheses[0][1:]),
                              skip_special_tokens=True).strip()
            for r in out
        ]


def _too_short(src_text, out_text):
    """NLLB sometimes drops a sentence when there are several. Then the translation
    is noticeably shorter than the original."""
    return len(out_text) < 0.6 * len(src_text)


# ---- v0.20: glossary (fixed translations) -------------------------------------
_gloss = {"mtime": None, "rules": []}


def _glossary_file():
    """glossary.txt, or the older name woordenlijst.txt next to it."""
    if GLOSSARY.exists():
        return GLOSSARY
    old = GLOSSARY.with_name("woordenlijst.txt")
    return old if old.exists() else GLOSSARY


def glossary_rules():
    path = _glossary_file()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _gloss.update(mtime=None, rules=[])
        return []
    if mtime != _gloss["mtime"]:
        rules = []
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" not in line:
                continue
            wrong, right = (x.strip() for x in line.split("=", 1))
            if wrong:
                rules.append((re.compile(r"(?<!\w)" + re.escape(wrong) + r"(?!\w)", re.IGNORECASE), right))
        _gloss.update(mtime=mtime, rules=rules)
        log(f"Glossary loaded: {len(rules)} rules")
    return _gloss["rules"]


def apply_glossary(text):
    for rx, right in glossary_rules():
        def keep_case(m, right=right):
            w = m.group(0)
            return right[:1].upper() + right[1:] if w[:1].isupper() else right
        text = rx.sub(keep_case, text)
    return text


# ---- v0.24: checks for made-up and dropped text -----------------------------------
# NLLB was trained largely on texts of the European Parliament. With a messy piece
# of sentence it sometimes falls back on such standard phrases (patterns in Dutch output).
_EU_OUT = re.compile(r"europese unie|europees parlement|lidstaten|lidstaat|de commissie|"
                     r"interne markt|de raad|commissaris|mevrouw de voorzitter|"
                     r"mijnheer de voorzitter|dit verslag|de rapporteur", re.IGNORECASE)
_EU_SRC = re.compile(r"europ|union|unión|union|parlam|commiss|comisi|kommission|council|"
                     r"consejo|conseil|rat\b|member state|estados miembros|rapport|report|"
                     r"informe|presid|vorsitz|chair", re.IGNORECASE)
_END = re.compile(r"[.!?…。！？][\"')\]]*$")
_SPLIT_WORDS = re.compile(r"\s(?=(?:pero|porque|but|because|mais|parce que|aber|weil|"
                          r"ma|perché|però|mas|porque|ale|ponieważ|ama|çünkü|но|потому что)\s)",
                          re.IGNORECASE)


def hallucinated(src, out):
    return bool(_EU_OUT.search(out)) and not _EU_SRC.search(src)


def _names(text):
    """Proper nouns (capital letter in the middle of a sentence) and numbers of 2+ digits."""
    toks = re.findall(r"[^\s]+", text)
    names = []
    for i, t in enumerate(toks):
        w = re.sub(r"^\W+|\W+$", "", t)
        if not w.replace("-", "").isalpha() or w.lower() in ("okay", "ok"):
            continue
        if i > 0 and len(w) >= 3 and w[:1].isupper() and not _END.search(toks[i - 1]):
            names.append(w)
    nums = [re.sub(r"[.,]", "", n) for n in re.findall(r"\d[\d.,]*\d|\d{2,}", text)]
    return names, nums


def missing(src, out, lang=None):
    """Which names or numbers from the original are missing in the translation?
    (German: all nouns have a capital letter, so only numbers.)"""
    names, nums = _names(src)
    if lang in ("deu_Latn", "ltz_Latn"):
        names = []
    low = out.lower()
    digits = re.sub(r"[.,]", "", out)
    miss = [n for n in names if n[:4].lower() not in low]
    miss += [n for n in nums if n not in digits]
    return miss


def problem(src, out, lang=None):
    """Returns the reason when the translation is suspicious, otherwise None."""
    if not out.strip():
        return "empty"
    if hallucinated(src, out):
        return "made up (EU phrase)"
    if len(out) < 0.6 * len(src):
        return "much shorter"
    n_src, n_out = len(split_sentences(src)), len(split_sentences(out))
    if n_src > 1 and n_out < n_src:
        return f"{n_src - n_out} sentence(s) dropped"
    miss = missing(src, out, lang)
    if miss:
        return "missing: " + ", ".join(miss[:3])
    return None


def clauses(text):
    """Cut into smaller pieces: sentences, then commas, then words like 'pero'/'but'."""
    out = []
    for sent in split_sentences(text):
        if len(sent) <= 60:
            out.append(sent)
            continue
        parts = [p for p in re.split(r"(?<=[,;:])\s+", sent) if p.strip()]
        for p in parts:
            if len(p) > 80:
                out.extend(x for x in _SPLIT_WORDS.split(p) if x.strip())
            else:
                out.append(p)
    return out


def _prepare(text):
    """v0.24: NLLB works best with 'real' sentences. Capital letter at the start and a
    full stop at the end when they are missing; undone after translating."""
    t = text.strip()
    added = not _END.search(t)
    if added:
        t += "."
    if t[:1].islower():
        t = t[:1].upper() + t[1:]
    return t, added


def _finish(out, added, orig):
    if added and out.endswith(".") and not out.endswith("..."):
        out = out[:-1]
    if orig[:1].islower() and out[:1].isupper():
        first = re.match(r"\w+", out)
        if not (first and first.group(0) in orig):
            out = out[:1].lower() + out[1:]
    return out


def _translate_pieces(pieces, src, tgt):
    prepped = [_prepare(p) for p in pieces]
    outs = _run([p for p, _ in prepped], src, tgt)
    return [_finish(o, a, p) for o, (_, a), p in zip(outs, prepped, pieces)]


def translate_texts(texts, source, target):
    """Translate a list of texts.
    v0.20: a short piece with several sentences goes to NLLB in one go (coherence).
    v0.24: then a check. When something was made up or dropped, again in smaller
    pieces. When it stays made up, rather the original than a made-up sentence."""
    src = nllb_code(source)
    tgt = nllb_code(target)
    if not src or not tgt:
        raise ValueError(f"Unknown language: {source} -> {target}")
    if src == tgt:
        return list(texts)

    results = ["" for _ in texts]
    todo = [i for i, t in enumerate(texts) if t and t.strip()]

    # first attempt: short piece in one go, long piece per sentence
    whole = [i for i in todo if len(texts[i]) <= WHOLE_MAX_CHARS]
    for i, out in zip(whole, _translate_pieces([texts[i].strip() for i in whole], src, tgt)):
        results[i] = out
    for i in [i for i in todo if len(texts[i]) > WHOLE_MAX_CHARS]:
        results[i] = " ".join(_translate_pieces(split_sentences(texts[i]), src, tgt))

    # v0.24: check and retry
    for i in todo:
        why = problem(texts[i], results[i], src)
        if not why:
            continue
        pieces = clauses(texts[i])
        retry = " ".join(_translate_pieces(pieces, src, tgt)) if len(pieces) > 1 else results[i]
        if hallucinated(texts[i], retry):
            log(f"retry failed ({why}), original shown: {texts[i][:60]!r}")
            results[i] = texts[i].strip()
            continue
        better = (problem(texts[i], retry, src) is None or len(retry) > len(results[i]) * 1.1
                  or hallucinated(texts[i], results[i]))
        log(f"retry ({why}) in {len(pieces)} pieces: "
            f"{'better' if better else 'not better'} -> {(retry if better else results[i])[:70]!r}")
        if better:
            results[i] = retry

    return [apply_glossary(r) for r in results]


app = FastAPI(title="NLLB translator for M3U Translator Subtitle")


async def read_params(request: Request):
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            return await request.json()
        except Exception:
            return {}
    form = await request.form()
    data = {}
    for k in form.keys():
        vals = form.getlist(k)
        data[k] = vals if (k == "q" and len(vals) > 1) else vals[0]
    return data


@app.post("/translate")
async def translate(request: Request):
    p = await read_params(request)
    q = p.get("q")
    source = (p.get("source") or "auto")
    target = (p.get("target") or "nl")
    if q is None:
        return JSONResponse({"error": "No 'q' given"}, status_code=400)
    is_list = isinstance(q, list)
    texts = [str(x) for x in q] if is_list else [str(q)]

    detected = None
    if source == "auto":
        lang, conf = detect(" ".join(texts))
        source = lang
        detected = {"language": lang, "confidence": conf}

    t = time.time()
    try:
        # v0.30: in a separate thread, so that /health keeps answering while translating
        out = await run_in_threadpool(translate_texts, texts, source, target)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    dur = time.time() - t
    log(f"{source}->{target} {dur:.2f}s | {texts[0][:70]!r} -> {out[0][:70]!r}")

    resp = {"translatedText": out if is_list else out[0]}
    if detected:
        resp["detectedLanguage"] = [detected] * len(out) if is_list else detected
    return resp


@app.post("/detect")
async def detect_route(request: Request):
    p = await read_params(request)
    lang, conf = await run_in_threadpool(detect, str(p.get("q", "")))
    return [{"language": lang, "confidence": conf}]


@app.get("/languages")
def languages():
    codes = sorted(LANG.keys())
    return [{"code": c, "name": NAMES.get(c, c), "targets": codes} for c in codes]


@app.get("/frontend/settings")
def settings():
    return {"apiKeys": False, "keyRequired": False, "charLimit": -1, "language": {}}


@app.get("/health")
def health():
    return {"status": "ok" if _translator is not None else "laden", "model": MODEL,
            "device": DEVICE, "compute": COMPUTE, "beam": BEAM}


if __name__ == "__main__":
    import uvicorn
    try:
        load()
    except Exception as e:
        log(f"ERROR while loading: {e}")
        sys.exit(1)
    log("Ready. Listening on port 5000 (LibreTranslate-compatible).")
    uvicorn.run(app, host="0.0.0.0", port=5000, log_level="warning")
