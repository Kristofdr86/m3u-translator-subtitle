"""Language codes (ISO 639-1, as Whisper uses them) and their English names.

Needed because some speech servers return the language as a name ("english") and
because an LLM translator understands a language name better than a code.
"""

NAMES = {
    "en": "english", "zh": "chinese", "de": "german", "es": "spanish", "ru": "russian",
    "ko": "korean", "fr": "french", "ja": "japanese", "pt": "portuguese", "tr": "turkish",
    "pl": "polish", "ca": "catalan", "nl": "dutch", "ar": "arabic", "sv": "swedish",
    "it": "italian", "id": "indonesian", "hi": "hindi", "fi": "finnish", "vi": "vietnamese",
    "he": "hebrew", "uk": "ukrainian", "el": "greek", "ms": "malay", "cs": "czech",
    "ro": "romanian", "da": "danish", "hu": "hungarian", "ta": "tamil", "no": "norwegian",
    "th": "thai", "ur": "urdu", "hr": "croatian", "bg": "bulgarian", "lt": "lithuanian",
    "la": "latin", "mi": "maori", "ml": "malayalam", "cy": "welsh", "sk": "slovak",
    "te": "telugu", "fa": "persian", "lv": "latvian", "bn": "bengali", "sr": "serbian",
    "az": "azerbaijani", "sl": "slovenian", "kn": "kannada", "et": "estonian",
    "mk": "macedonian", "br": "breton", "eu": "basque", "is": "icelandic", "hy": "armenian",
    "ne": "nepali", "mn": "mongolian", "bs": "bosnian", "kk": "kazakh", "sq": "albanian",
    "sw": "swahili", "gl": "galician", "mr": "marathi", "pa": "punjabi", "si": "sinhala",
    "km": "khmer", "sn": "shona", "yo": "yoruba", "so": "somali", "af": "afrikaans",
    "oc": "occitan", "ka": "georgian", "be": "belarusian", "tg": "tajik", "sd": "sindhi",
    "gu": "gujarati", "am": "amharic", "yi": "yiddish", "lo": "lao", "uz": "uzbek",
    "fo": "faroese", "ht": "haitian creole", "ps": "pashto", "tk": "turkmen", "nn": "nynorsk",
    "mt": "maltese", "sa": "sanskrit", "lb": "luxembourgish", "my": "myanmar", "bo": "tibetan",
    "tl": "tagalog", "mg": "malagasy", "as": "assamese", "tt": "tatar", "haw": "hawaiian",
    "ln": "lingala", "ha": "hausa", "ba": "bashkir", "jw": "javanese", "su": "sundanese",
    "yue": "cantonese",
}
CODES = {v: k for k, v in NAMES.items()}
CODES.update({"castilian": "es", "flemish": "nl", "valencian": "ca", "moldavian": "ro",
              "moldovan": "ro", "burmese": "my", "pushto": "ps", "panjabi": "pa",
              "letzeburgesch": "lb", "sinhalese": "si", "haitian": "ht", "mandarin": "zh"})


def to_code(value):
    """'english', 'English', 'en', 'en-US' -> 'en'. Unknown -> None."""
    if not value:
        return None
    v = str(value).strip().lower().replace("_", "-")
    if v in NAMES:
        return v
    if v in CODES:
        return CODES[v]
    base = v.split("-")[0]
    if base in NAMES:
        return base
    if 2 <= len(base) <= 3 and base.isalpha():
        return base
    return None


def to_name(code):
    """'nl' -> 'Dutch' (for an LLM translator)."""
    n = NAMES.get(str(code or "").lower())
    return n.title() if n else str(code)


# ISO 639-2 (three letters) for the language label of the subtitle track in the stream.
# Kodi shows this label (e.g. 'Dutch') and can switch the track on automatically.
ISO639_2 = {
    "nl": "dut", "en": "eng", "de": "ger", "fr": "fre", "es": "spa", "it": "ita",
    "pt": "por", "pl": "pol", "sv": "swe", "da": "dan", "no": "nor", "fi": "fin",
    "cs": "cze", "sk": "slo", "hu": "hun", "ro": "rum", "el": "gre", "tr": "tur",
    "ru": "rus", "uk": "ukr", "bg": "bul", "hr": "hrv", "sr": "srp", "sl": "slv",
    "ar": "ara", "he": "heb", "fa": "per", "hi": "hin", "zh": "chi", "ja": "jpn",
    "ko": "kor", "vi": "vie", "th": "tha", "id": "ind", "ms": "may", "ca": "cat",
    "eu": "baq", "gl": "glg", "cy": "wel", "ga": "gle", "is": "ice", "et": "est",
    "lv": "lav", "lt": "lit", "sq": "alb", "mk": "mac", "af": "afr",
}


def to_iso639_2(code):
    """'nl' -> 'dut'. Unknown two-letter code -> 'und'; a three-letter code stays."""
    c = str(code or "").lower()
    if len(c) == 3:
        return c
    return ISO639_2.get(c, "und")
