# NLLB translator for M3U Translator Subtitle - LibreTranslate-compatible API on the GPU (optional)
FROM nvidia/cuda:12.3.2-cudnn9-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models/hf \
    MODELS_DIR=/models

RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip curl \
    && rm -rf /var/lib/apt/lists/*

# torch (CPU only) is only needed to convert the model once
RUN pip3 install --no-cache-dir "numpy<2" torch==2.3.1 --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple

RUN pip3 install --no-cache-dir \
    "numpy<2" \
    ctranslate2==4.5.0 \
    transformers==4.44.2 \
    sentencepiece \
    protobuf \
    fastapi \
    "uvicorn[standard]" \
    python-multipart \
    langdetect \
    accelerate==0.33.0

WORKDIR /app
COPY server.py /app/server.py

HEALTHCHECK --interval=30s --timeout=5s --start-period=30m \
  CMD curl -fs http://127.0.0.1:5000/health || exit 1

EXPOSE 5000
CMD ["python3", "/app/server.py"]
