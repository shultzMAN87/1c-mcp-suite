"""
Индексатор статей ИТС в Qdrant — v3 (Hybrid Search)
====================================================
Парсит PDF и TXT файлы из папки /data/its-articles и индексирует их
в Qdrant с ДВУМЯ векторами на каждый чанк:

  - dense  (плотный)  — multilingual-e5-base, поиск по смыслу
  - sparse (BM25)     — fastembed Qdrant/bm25, поиск по точным словам

При поиске агент отправляет один гибридный запрос с prefetch + RRF
(Reciprocal Rank Fusion) — нативный механизм Qdrant 1.10+, который
объединяет два списка результатов в один сбалансированный.

Зачем гибрид:
  Чисто плотный поиск ("по смыслу") иногда промахивается мимо точных
  технических терминов вроде «#std466», «УИДЗначения», «ОбработкаПроведения».
  BM25 их ловит. И наоборот — BM25 промахивается мимо синонимов и
  переформулировок, которые ловит плотный e5. Гибрид через RRF
  объединяет сильные стороны обоих.

Инструкция:
  1. Откройте статью на its.1c.ru в браузере
  2. Ctrl+P → Сохранить как PDF
  3. Положите PDF в папку its-articles/
  4. Запустите индексатор

Поддерживает: .pdf, .txt, .md
"""

import os
import re
import time
import hashlib
from pathlib import Path

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
COLLECTION_NAME = os.environ.get("ITS_COLLECTION", "its_articles")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-base")
BM25_MODEL = os.environ.get("BM25_MODEL", "Qdrant/bm25")
ARTICLES_DIR = os.environ.get("ITS_ARTICLES_DIR", "/data/its-articles")
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "400"))
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "80"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "32"))
FORCE_REINDEX = os.environ.get("ITS_FORCE_REINDEX", "").lower() in ("1", "true", "yes")


# ─── Парсинг файлов ──────────────────────────────────────────────────────

def parse_pdf(filepath):
    """Извлекает текст из PDF через PyMuPDF."""
    import fitz  # pymupdf

    doc = fitz.open(filepath)
    pages = []
    for page in doc:
        text = page.get_text()
        if text.strip():
            pages.append(text)
    doc.close()

    full_text = "\n\n".join(pages)

    # Чистим мусор (колонтитулы, номера страниц)
    lines = []
    for line in full_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if re.match(r'^\d{2}\.\d{2}\.\d{4},\s*\d{2}:\d{2}', line):
            continue  # "31.03.2026, 07:37"
        if "https://its.1c.ru/" in line and len(line) < 200:
            continue  # URL в колонтитуле
        if re.match(r'^\d+/\d+$', line):
            continue  # "1/4"
        lines.append(line)

    return "\n".join(lines)


def parse_txt(filepath):
    """Читает текстовый файл с автоопределением кодировки."""
    for enc in ("utf-8-sig", "utf-8", "windows-1251", "cp1251"):
        try:
            with open(filepath, "r", encoding=enc) as f:
                text = f.read()
            if len(text) > 50:
                return text
        except UnicodeDecodeError:
            continue
    return ""


def extract_title(text, filename):
    """Извлекает заголовок из текста или имени файла."""
    m = re.search(r'#std\d+\s*\n(.+)', text)
    if m:
        return m.group(1).strip()
    for line in text.split("\n"):
        line = line.strip()
        if len(line) > 5 and not line.startswith("#"):
            return line[:150]
    name = Path(filename).stem
    name = name.replace("_", " ").replace("  ", " ")
    return name[:150]


def extract_std_number(text):
    """Извлекает номер стандарта #stdNNN."""
    m = re.search(r'#std(\d+)', text)
    return m.group(1) if m else ""


def detect_category(text, filename):
    """Определяет категорию статьи."""
    lower = (text[:500] + filename).lower()
    if "стандарт" in lower or "std" in lower:
        return "Стандарты разработки"
    if "методик" in lower:
        return "Методические рекомендации"
    if "документац" in lower or "платформ" in lower:
        return "Документация платформы"
    return "ИТС"


def chunk_text(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Разбивает текст на чанки слов с перекрытием."""
    words = text.split()
    if len(words) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunks.append(" ".join(words[start:end]))
        start += chunk_size - overlap
    return chunks


# ─── Сбор документов ──────────────────────────────────────────────────────

def collect_documents():
    """Собирает чанки из всех файлов в папке."""
    articles_dir = Path(ARTICLES_DIR)
    if not articles_dir.exists():
        print(f"⚠ Папка не найдена: {ARTICLES_DIR}")
        return []

    documents = []
    supported = {".pdf", ".txt", ".md"}
    files = sorted(f for f in articles_dir.iterdir() if f.suffix.lower() in supported)

    if not files:
        print(f"⚠ Нет файлов (.pdf, .txt, .md) в {ARTICLES_DIR}")
        print("  Инструкция:")
        print("  1. Откройте статью на its.1c.ru")
        print("  2. Ctrl+P → Сохранить как PDF")
        print("  3. Положите PDF в папку its-articles/")
        return []

    print(f"Найдено файлов: {len(files)}")

    for filepath in files:
        print(f"  Обработка: {filepath.name}", end="")
        try:
            if filepath.suffix.lower() == ".pdf":
                text = parse_pdf(filepath)
            else:
                text = parse_txt(filepath)

            if len(text.strip()) < 50:
                print(" — пропуск (мало текста)")
                continue

            title = extract_title(text, filepath.name)
            std_num = extract_std_number(text)
            category = detect_category(text, filepath.name)

            print(f" — '{title}' ({len(text)} символов)")

            chunks = chunk_text(text)
            for i, chunk in enumerate(chunks):
                doc_id = hashlib.md5(f"{filepath.name}:{i}".encode()).hexdigest()
                documents.append({
                    "id": doc_id,
                    "text": chunk,
                    "metadata": {
                        "filename": filepath.name,
                        "title": title,
                        "std_number": std_num,
                        "category": category,
                        "chunk_index": i,
                        "total_chunks": len(chunks),
                    },
                })

        except Exception as e:
            print(f" — ⚠ ошибка: {e}")

    print(f"\nВсего чанков: {len(documents)}")
    return documents


# ─── Qdrant: ожидание и совместимость коллекции ──────────────────────────

def wait_for_qdrant(client, timeout=60):
    """Ждём пока Qdrant поднимется."""
    print(f"Ожидание Qdrant ({QDRANT_URL})...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            client.get_collections()
            print("  ✓ Qdrant доступен")
            return
        except Exception:
            time.sleep(2)
    raise RuntimeError("Qdrant недоступен")


def collection_is_hybrid(client) -> bool:
    """
    Проверяет, существует ли коллекция в гибридном формате
    (named dense + sparse). Если коллекция была создана старым (v2)
    индексатором — она в плоском формате и должна быть пересоздана.
    """
    try:
        info = client.get_collection(COLLECTION_NAME)
    except Exception:
        return False

    try:
        params = info.config.params
        vectors = params.vectors
        sparse = getattr(params, "sparse_vectors", None)
        is_named_dense = isinstance(vectors, dict) and "dense" in vectors
        has_sparse = bool(sparse) and "sparse" in sparse
        return is_named_dense and has_sparse
    except Exception:
        return False


def collection_has_points(client) -> bool:
    try:
        info = client.get_collection(COLLECTION_NAME)
        return (info.points_count or 0) > 0
    except Exception:
        return False


# ─── Main ─────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Индексация статей ИТС → Qdrant (Hybrid: dense + BM25)")
    print("=" * 60)

    from qdrant_client import QdrantClient, models
    from sentence_transformers import SentenceTransformer
    from fastembed import SparseTextEmbedding

    client = QdrantClient(url=QDRANT_URL, timeout=60)
    wait_for_qdrant(client)

    # Решаем, что делать с существующей коллекцией
    exists = False
    try:
        client.get_collection(COLLECTION_NAME)
        exists = True
    except Exception:
        exists = False

    if exists:
        is_hybrid = collection_is_hybrid(client)
        has_points = collection_has_points(client)

        if FORCE_REINDEX:
            print(f"⚠ ITS_FORCE_REINDEX=true — удаляем '{COLLECTION_NAME}'")
            client.delete_collection(COLLECTION_NAME)
        elif not is_hybrid:
            print(f"⚠ Коллекция '{COLLECTION_NAME}' существует, но в старом (плоском) формате.")
            print("  Для гибридного поиска нужно пересоздать. Удаляю...")
            client.delete_collection(COLLECTION_NAME)
        elif has_points:
            print(f"✓ Коллекция '{COLLECTION_NAME}' уже существует и в гибридном формате.")
            print("  Для переиндексации:")
            print("    docker compose run --rm -e ITS_FORCE_REINDEX=true its-indexer")
            print("  или удалите вручную:")
            print(f"    curl -X DELETE {QDRANT_URL}/collections/{COLLECTION_NAME}")
            return

    documents = collect_documents()
    if not documents:
        return

    # ─── Загрузка моделей ────────────────────────────────────────────────
    print(f"\nЗагрузка dense-модели: {EMBEDDING_MODEL}")
    dense_model = SentenceTransformer(EMBEDDING_MODEL)
    dense_dim = dense_model.get_sentence_embedding_dimension()
    print(f"  ✓ Загружена (dim={dense_dim})")

    print(f"Загрузка sparse-модели (BM25): {BM25_MODEL}")
    sparse_model = SparseTextEmbedding(model_name=BM25_MODEL)
    print("  ✓ Загружена")

    # ─── Создание коллекции ──────────────────────────────────────────────
    print(f"\nСоздание коллекции '{COLLECTION_NAME}' (hybrid)...")
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config={
            "dense": models.VectorParams(
                size=dense_dim,
                distance=models.Distance.COSINE,
            ),
        },
        sparse_vectors_config={
            "sparse": models.SparseVectorParams(
                modifier=models.Modifier.IDF,
            ),
        },
    )
    print("  ✓ Создана")

    # ─── Генерация эмбеддингов ───────────────────────────────────────────
    is_e5 = "e5" in EMBEDDING_MODEL.lower()
    raw_texts = [d["text"] for d in documents]
    dense_inputs = [f"passage: {t}" if is_e5 else t for t in raw_texts]

    print(f"\nГенерация dense-векторов ({len(raw_texts)} чанков)...")
    dense_vectors = []
    for i in range(0, len(dense_inputs), BATCH_SIZE):
        batch = dense_inputs[i:i + BATCH_SIZE]
        emb = dense_model.encode(batch, show_progress_bar=False, normalize_embeddings=True)
        dense_vectors.extend(emb.tolist())
        if (i // BATCH_SIZE) % 5 == 0 and i > 0:
            print(f"  dense: {min(i + BATCH_SIZE, len(dense_inputs))}/{len(dense_inputs)}")

    print(f"\nГенерация sparse-векторов BM25 ({len(raw_texts)} чанков)...")
    # fastembed возвращает iterator из SparseEmbedding(indices, values)
    sparse_embeddings = list(sparse_model.embed(raw_texts, batch_size=BATCH_SIZE))
    print(f"  ✓ Готово ({len(sparse_embeddings)} векторов)")

    # ─── Загрузка точек в Qdrant ─────────────────────────────────────────
    print("\nЗапись в Qdrant...")
    points = []
    for i, (doc, dvec, svec) in enumerate(zip(documents, dense_vectors, sparse_embeddings)):
        points.append(
            models.PointStruct(
                id=i,
                vector={
                    "dense": dvec,
                    "sparse": models.SparseVector(
                        indices=svec.indices.tolist(),
                        values=svec.values.tolist(),
                    ),
                },
                payload={"text": doc["text"], **doc["metadata"]},
            )
        )

    upsert_batch = 100
    total = len(points)
    for i in range(0, total, upsert_batch):
        client.upsert(
            collection_name=COLLECTION_NAME,
            points=points[i:i + upsert_batch],
            wait=True,
        )
        if (i // upsert_batch) % 5 == 0 and i > 0:
            print(f"  upsert: {min(i + upsert_batch, total)}/{total}")
    print(f"  ✓ Записано {total} точек")

    print("\n" + "=" * 60)
    print(f"✓ Индексация завершена: {total} чанков в '{COLLECTION_NAME}'")
    print(f"  Векторы: dense (e5, {dense_dim}) + sparse (BM25)")
    print("=" * 60)


if __name__ == "__main__":
    main()
