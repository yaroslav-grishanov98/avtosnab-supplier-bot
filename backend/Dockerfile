FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-rus \
    tesseract-ocr-eng \
    antiword \
    libpq-dev \
    gcc \
    curl \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir poetry==2.1.3

RUN poetry config virtualenvs.in-project true

# Run Django and worker commands from the Poetry environment installed below.
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --without docling

# Embedding-модель сопоставления товаров запекается в образ: рантайм не зависит от доступности Hugging Face.
# Должна совпадать с CATALOG_EMBEDDING_MODEL; при смене модели через env образ нужно пересобрать с новым ARG,
# иначе приложение скачает её при первом обращении (см. api/v1/catalog/embeddings.py).
ARG CATALOG_EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
ENV HF_HOME=/opt/hf
RUN python -c "import os; from sentence_transformers import SentenceTransformer; SentenceTransformer(os.environ['CATALOG_EMBEDDING_MODEL'])"

COPY . .

RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
