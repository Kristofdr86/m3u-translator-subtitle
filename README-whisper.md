# Private add-on: Whisper speech server

Not part of the public project. Runs faster-whisper (large-v3, int8) on an NVIDIA GPU
with an OpenAI-compatible API, so M3U Translator Subtitle can use it as `ASR_ENGINE=http`.

Install (in the project folder):

    unzip -o whisper-addon-V1.zip -d .
    sed -i 's#^COMPOSE_FILE=.*#COMPOSE_FILE=docker-compose.yml:docker-compose.nllb.yml:docker-compose.whisper.yml#; s#^ASR_URL=.*#ASR_URL=http://whisper:8000/v1#' .env
    printf 'docker-compose.whisper.yml\nservices/whisper/\nREADME-whisper.md\n' >> .gitignore
    docker compose up -d --build
    docker compose logs -f whisper

The first start downloads the model (about 3 GB) into data/whisper-models/.
Test from this PC: curl http://127.0.0.1:8000/health
