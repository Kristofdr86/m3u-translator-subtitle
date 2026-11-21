"""Voice activity detection (VAD) on the CPU, independent of any speech recognition.

Uses the Silero VAD model (MIT licence) via onnxruntime. The model ships in the
pip package 'silero-vad' (installed without torch, see Dockerfile). When the model or
onnxruntime is missing, this falls back to a simple energy measurement. That one is
worse at recognising music as 'no speech', but everything keeps working.

Audio is always numpy float32, 16 kHz mono.
"""
import os
import threading

import numpy as np

SR = 16000
WINDOW = 512          # Silero v5: windows of 512 samples (32 ms) at 16 kHz
CONTEXT = 64          # Silero v5: the last 64 samples of the previous window go along


def _model_path():
    env = os.environ.get("VAD_MODEL")
    if env and os.path.exists(env):
        return env
    try:
        # Find the file without importing the package (that would need torch).
        import importlib.util
        spec = importlib.util.find_spec("silero_vad")
        for d in (spec.submodule_search_locations or []) if spec else []:
            p = os.path.join(d, "data", "silero_vad.onnx")
            if os.path.isfile(p):
                return p
    except Exception:  # noqa: BLE001
        pass
    return None


class SileroVAD:
    def __init__(self, path):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.sess = ort.InferenceSession(path, sess_options=opts,
                                         providers=["CPUExecutionProvider"])
        self.lock = threading.Lock()
        self.name = "silero"

    def probs(self, audio):
        """Probability of speech per 32 ms window."""
        n = len(audio)
        pad = (-n) % WINDOW
        x = np.pad(audio.astype(np.float32), (0, pad))
        state = np.zeros((2, 1, 128), dtype=np.float32)
        ctx = np.zeros(CONTEXT, dtype=np.float32)
        sr = np.array(SR, dtype=np.int64)
        out = []
        with self.lock:
            for i in range(0, len(x), WINDOW):
                chunk = x[i:i + WINDOW]
                inp = np.concatenate([ctx, chunk])[None, :]
                p, state = self.sess.run(None, {"input": inp, "state": state, "sr": sr})
                out.append(float(p[0][0]))
                ctx = chunk[-CONTEXT:]
        return np.array(out, dtype=np.float32)


class EnergyVAD:
    """Fallback: loudness per window compared to the quietest part of the piece."""
    name = "energy"

    def probs(self, audio):
        n = len(audio) // WINDOW
        if n == 0:
            return np.zeros(0, dtype=np.float32)
        frames = audio[:n * WINDOW].reshape(n, WINDOW)
        rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
        db = 20 * np.log10(rms)
        floor = np.percentile(db, 10)
        # 0 at the noise floor, 1 from 18 dB above it, and never speech below -50 dBFS
        p = np.clip((db - floor - 6) / 12.0, 0, 1)
        p[db < -50] = 0
        return p.astype(np.float32)


_VAD = None
_VAD_LOCK = threading.Lock()


def get_vad():
    global _VAD
    with _VAD_LOCK:
        if _VAD is None:
            path = _model_path()
            if path:
                try:
                    _VAD = SileroVAD(path)
                except Exception:  # noqa: BLE001
                    _VAD = EnergyVAD()
            else:
                _VAD = EnergyVAD()
        return _VAD


def speech_regions(audio, threshold=0.35, min_silence=0.35, min_speech=0.25, pad=0.0):
    """Speech regions [(start, end)] in seconds.
    threshold: probability from which a window counts as speech (VAD_THRESHOLD).
    min_silence: shorter pauses belong to the same sentence.
    min_speech: shorter 'speech' does not count (click, tap).
    pad: extra margin around each region."""
    if audio is None or len(audio) < WINDOW:
        return []
    p = get_vad().probs(audio)
    step = WINDOW / SR
    neg = max(threshold - 0.15, 0.01)          # same hysteresis as Silero itself
    regions, start, quiet = [], None, None
    for i, v in enumerate(p):
        t = i * step
        if start is None:
            if v >= threshold:
                start, quiet = t, None
        else:
            if v < neg:
                if quiet is None:
                    quiet = t
                elif t - quiet >= min_silence:
                    regions.append((start, quiet))
                    start, quiet = None, None
            elif v >= threshold:
                quiet = None
    if start is not None:
        regions.append((start, quiet if quiet is not None else len(p) * step))
    total = len(audio) / SR
    out = []
    for a, b in regions:
        if b - a < min_speech:
            continue
        a, b = max(0.0, a - pad), min(total, b + pad)
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def keep_speech(audio, regions):
    """Mute everything outside the speech regions (times stay the same)."""
    out = np.zeros_like(audio)
    for a, b in regions:
        i0, i1 = int(a * SR), int(b * SR)
        out[i0:i1] = audio[i0:i1]
    return out


def backend_name():
    return get_vad().name
