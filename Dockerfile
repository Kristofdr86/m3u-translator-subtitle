# M3U Translator Subtitle - small container: ffmpeg + Python, no speech model and no GPU needed.
# Speech recognition and translation run wherever you want (see README: ASR_ENGINE, TRANSLATE_ENGINE).
#
# Ubuntu 24.04 because of its ffmpeg 6.1: the stream joining for Kodi (seamless subtitles,
# no stalls at the seams) was tested with exactly this ffmpeg version.
# Versions are pinned, so a new build does the same thing.
# Check for newer versions: ./scripts/check-updates.sh
FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg python3 python3-pip fonts-dejavu-core fonts-open-sans fonts-liberation2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# silero-vad without dependencies: only the small voice-detection model (ONNX),
# no torch. onnxruntime runs it on the CPU.
RUN pip3 install --break-system-packages --no-cache-dir \
      "numpy==2.2.6" "pillow==11.3.0" "onnxruntime==1.22.1" \
    && pip3 install --break-system-packages --no-cache-dir --no-deps "silero-vad==5.1.2"

COPY app/ /app/
ENV PYTHONUNBUFFERED=1 \
    TMPDIR=/work
EXPOSE 8088
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/status', timeout=4)" || exit 1
CMD ["python3", "/app/main.py"]
