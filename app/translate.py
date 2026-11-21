"""Translation: connections to a translator of your choice (TRANSLATE_ENGINE).

  libretranslate  A server with the LibreTranslate API (/translate, /languages).
                  The included NLLB service (services/nllb) speaks this API too.
  llm             A language model via an OpenAI-compatible chat API
                  (/v1/chat/completions), e.g. Ollama, llama.cpp, vLLM, LM Studio
                  or an online service.

On an error the original text stays visible; the stream always keeps running.
"""
import json
import re
import time
import urllib.error
import urllib.request

from languages import to_name


class _Base:
    name = "?"

    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.cache = {}
        self.down_until = 0.0
        self.last_down_log = 0.0
        self.last_error = None       # (time, text) for the web interface

    def _down(self, e):
        self.down_until = time.time() + 30
        self.last_error = (time.strftime("%H:%M:%S"), f"not reachable: {e}"[:200])
        if time.time() - self.last_down_log > 60:
            self.log(f"Translator ({self.name}) not reachable ({e}); showing the original "
                     "for 30 s, then trying again")
            self.last_down_log = time.time()

    def refresh(self):
        pass

    def supports(self, src):
        return True

    def translate(self, texts, src):
        """Translates sentences. On an error the original text is kept visible."""
        dst = self.cfg["dst_lang"]
        if not texts or src == dst:
            return list(texts)
        todo = list(dict.fromkeys(t for t in texts if (src, t) not in self.cache))
        if todo and time.time() < self.down_until:
            return [self.cache.get((src, t), t) for t in texts]
        if todo:
            got = self._translate(todo, src, dst)
            if got is None:
                if time.time() >= self.down_until:
                    self.last_error = (time.strftime("%H:%M:%S"),
                                       f"no translation {src} -> {dst} (see the log)")
                return list(texts)
            self.last_error = None
            for orig, tr in zip(todo, got):
                self.cache[(src, orig)] = tr
            if len(self.cache) > 4000:
                self.cache.clear()
        return [self.cache.get((src, t), t) for t in texts]

    def _translate(self, todo, src, dst):
        raise NotImplementedError


class LibreTranslate(_Base):
    name = "libretranslate"

    def __init__(self, cfg, log):
        super().__init__(cfg, log)
        self.sources = None        # languages the server can translate to TARGET_LANG
        self.bad = set()
        self.checked = 0
        self.refresh_ok = False

    def describe(self):
        return self.cfg["lt_url"] or "(TRANSLATE_URL empty)"

    def refresh(self):
        """Asks the server which languages it knows (/languages)."""
        self.checked = time.time()
        if not self.cfg["lt_url"]:
            self.sources = set()
            self.log("NOTE: TRANSLATE_URL is empty; the original text is shown")
            return
        try:
            with urllib.request.urlopen(self.cfg["lt_url"] + "/languages",
                                        timeout=self.cfg["lt_timeout"]) as r:
                langs = json.loads(r.read().decode())
            dst = self.cfg["dst_lang"]
            self.sources = {l["code"] for l in langs if dst in (l.get("targets") or [])}
            self.bad.clear()
            self.refresh_ok = True
            self.log(f"Translator knows to '{dst}': {', '.join(sorted(self.sources)) or 'nothing'}")
        except Exception as e:  # noqa: BLE001
            # Unknown languages then go via English (if the speech recognition can do that).
            self.sources = set()
            self.refresh_ok = False
            self.log("Language list of the translator not reachable; languages go via English:", e)

    def supports(self, src):
        if src == self.cfg["dst_lang"]:
            return True
        if time.time() - self.checked > (600 if self.refresh_ok else 60):
            self.refresh()
        return src not in self.bad and self.sources is not None and src in self.sources

    def _translate(self, todo, src, dst):
        if not self.cfg["lt_url"]:
            return None
        body = {"q": todo, "source": src, "target": dst, "format": "text"}
        if self.cfg["lt_key"]:
            body["api_key"] = self.cfg["lt_key"]
        try:
            req = urllib.request.Request(
                self.cfg["lt_url"] + "/translate", data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.cfg["lt_timeout"]) as r:
                res = json.loads(r.read().decode())
            tr = res.get("translatedText", [])
            if isinstance(tr, str):
                tr = [tr]
            if not isinstance(tr, list) or len(tr) != len(todo):
                raise ValueError(f"unexpected number of translations for {len(todo)} sentences")
            out = []
            for t in tr:
                if not isinstance(t, str) or not t.strip():
                    raise ValueError("empty translation")
                out.append(t.strip())
            return out
        except urllib.error.HTTPError as e:
            self.log(f"Translation error ({src} -> {dst}), original kept:", e)
            if e.code == 400:
                self.bad.add(src)
            return None
        except ValueError as e:
            self.log(f"Translator gave an unusable answer ({src}), original kept:", e)
            return None
        except Exception as e:  # noqa: BLE001
            self._down(e)
            return None


DEFAULT_PROMPT = (
    "You translate live TV subtitles from {src} to {dst}. "
    "You get a JSON array of subtitle lines in order; they may be parts of one sentence. "
    "Translate each line naturally and concisely, keep names and numbers, keep the meaning. "
    "Do not add explanations or sound descriptions. "
    "Answer with only a JSON array of exactly {n} strings, in the same order."
)


class LLMTranslator(_Base):
    """Translation with a language model via /v1/chat/completions."""
    name = "llm"

    def describe(self):
        return f"{self.cfg['llm_url']} (model {self.cfg['llm_model'] or '?'})"

    def refresh(self):
        if not self.cfg["llm_url"]:
            self.log("NOTE: LLM_URL is empty; the original text is shown")

    def _chat(self, messages):
        body = {"model": self.cfg["llm_model"], "messages": messages, "temperature": 0,
                "stream": False}
        headers = {"Content-Type": "application/json"}
        if self.cfg["llm_key"]:
            headers["Authorization"] = f"Bearer {self.cfg['llm_key']}"
        req = urllib.request.Request(self.cfg["llm_url"] + "/chat/completions",
                                     data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=self.cfg["llm_timeout"]) as r:
            res = json.loads(r.read().decode())
        return res["choices"][0]["message"]["content"]

    @staticmethod
    def parse(content, n):
        """Get a JSON list of n texts from the answer (also inside ```json ... ```)."""
        content = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
        m = re.search(r"\[.*\]", content, re.S)
        if m:
            try:
                arr = json.loads(m.group(0))
                if (isinstance(arr, list) and len(arr) == n
                        and all(isinstance(x, str) and x.strip() for x in arr)):
                    return [x.strip() for x in arr]
            except json.JSONDecodeError:
                pass
        if n == 1:
            text = content.strip().strip('"').strip()
            return [text] if text and not text.startswith("[") else None
        return None

    def _translate(self, todo, src, dst):
        if not self.cfg["llm_url"]:
            return None
        prompt = (self.cfg["llm_prompt"] or DEFAULT_PROMPT).format(
            src=to_name(src), dst=to_name(dst), n=len(todo))
        msgs = [{"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(todo, ensure_ascii=False)}]
        try:
            got = self.parse(self._chat(msgs), len(todo))
            if got is None and len(todo) > 1:
                # Model returned a different number of lines: line by line
                got = []
                for t in todo:
                    one = self.parse(self._chat(
                        [{"role": "system", "content": (self.cfg["llm_prompt"] or DEFAULT_PROMPT)
                          .format(src=to_name(src), dst=to_name(dst), n=1)},
                         {"role": "user", "content": json.dumps([t], ensure_ascii=False)}]), 1)
                    got.append(one[0] if one else t)
            if got is None:
                self.log(f"LLM gave an unusable answer ({src}), original kept")
            return got
        except urllib.error.HTTPError as e:
            self.log(f"LLM error HTTP {e.code} ({src} -> {dst}), original kept")
            return None
        except (KeyError, IndexError, ValueError) as e:
            self.log(f"LLM gave an unexpected answer ({e}), original kept")
            return None
        except Exception as e:  # noqa: BLE001
            self._down(e)
            return None


ENGINES = {"libretranslate": LibreTranslate, "nllb": LibreTranslate,
           "llm": LLMTranslator, "openai": LLMTranslator}


def make_translator(cfg, log):
    kind = cfg["tr_engine"]
    if kind not in ENGINES:
        log(f"NOTE: TRANSLATE_ENGINE={kind} unknown; using libretranslate")
        kind = "libretranslate"
    return ENGINES[kind](cfg, log)


def test(cfg, log):
    """For the web interface: does this translator work? Returns (ok, message)."""
    tr = make_translator(cfg, lambda *a: None)
    src = "en" if cfg["dst_lang"] != "en" else "nl"
    sample = "Good evening, and welcome to the news." if src == "en" else "Goedenavond en welkom bij het nieuws."
    try:
        if isinstance(tr, LibreTranslate):
            if not cfg["lt_url"]:
                return False, "TRANSLATE_URL is empty"
            with urllib.request.urlopen(cfg["lt_url"] + "/languages",
                                        timeout=max(3, cfg["lt_timeout"])) as r:
                langs = json.loads(r.read().decode())
            targets = {l["code"] for l in langs if cfg["dst_lang"] in (l.get("targets") or [])}
            if src not in targets:
                return False, f"server cannot translate {src} -> {cfg['dst_lang']}"
            got = tr._translate([sample], src, cfg["dst_lang"])
        else:
            if not cfg["llm_url"]:
                return False, "LLM_URL is empty"
            got = LLMTranslator.parse(tr._chat([
                {"role": "system", "content": DEFAULT_PROMPT.format(
                    src=to_name(src), dst=to_name(cfg["dst_lang"]), n=1)},
                {"role": "user", "content": json.dumps([sample])}]), 1)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} from {e.url}"
    except Exception as e:  # noqa: BLE001
        return False, f"not reachable: {e}"
    if not got or got[0].strip() == sample:
        return False, "the server answered, but did not translate"
    return True, f"OK: \"{sample}\" -> \"{got[0]}\""
