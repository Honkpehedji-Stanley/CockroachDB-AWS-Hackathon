"""
Phase 3 — Ingestion de documents (PDF et texte) dans la mémoire vectorielle.

Flux : S3 (stockage brut) -> extraction de texte -> découpage en chunks
-> embedding Titan -> stockage dans CockroachDB
   (memory_embeddings, source_type='document').

Chaque document reçoit un `document_id` (UUID) unique, partagé par tous
ses chunks via la colonne `source_id` — pratique pour tracer d'où vient
un souvenir, ou pour supprimer un document entier plus tard.
"""
import io
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import boto3
from pypdf import PdfReader

import bedrock_client
import memory

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
S3_BUCKET = os.environ.get("DOCUMENTS_BUCKET")

CHUNK_SIZE = 1000     # caractères par chunk
CHUNK_OVERLAP = 200   # chevauchement entre chunks (évite de couper une idée en deux)

# Garde-fous pour rester sous les 30s d'intégration API Gateway (plafond AWS
# fixe pour les intégrations Lambda proxy en HTTP API, non modifiable) quand
# l'ingestion est déclenchée depuis une requête HTTP synchrone (upload chat).
MAX_CHUNKS_SYNC = 40
EMBEDDING_WORKERS = 8

_s3 = boto3.client("s3", region_name=AWS_REGION)


def _extract_text(filename: str, raw_bytes: bytes) -> str:
    """Extrait le texte brut d'un fichier .pdf ou .txt/.md."""
    if filename.lower().endswith(".pdf"):
        reader = PdfReader(io.BytesIO(raw_bytes))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    return raw_bytes.decode("utf-8", errors="ignore")


def _chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Découpe le texte en chunks avec chevauchement, pour une meilleure granularité de recherche."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start += chunk_size - overlap
    return chunks


def upload_document_to_s3(local_path: str, key: str | None = None) -> str:
    """Upload un fichier local vers S3 (stockage brut, avant traitement)."""
    key = key or os.path.basename(local_path)
    _s3.upload_file(local_path, S3_BUCKET, key)
    return key


def upload_bytes_to_s3(raw_bytes: bytes, key: str) -> str:
    """Upload des octets déjà en mémoire vers S3 — utilisé par la Lambda (pas de fichier local)."""
    _s3.put_object(Bucket=S3_BUCKET, Key=key, Body=raw_bytes)
    return key


def ingest_document_from_s3(key: str, user_id: str) -> dict:
    """Télécharge un document depuis S3 et l'indexe en mémoire vectorielle."""
    obj = _s3.get_object(Bucket=S3_BUCKET, Key=key)
    raw_bytes = obj["Body"].read()
    text = _extract_text(key, raw_bytes)
    return _ingest_text(text, user_id, filename=key)


def ingest_local_document(local_path: str, user_id: str) -> dict:
    """Ingestion directe depuis un fichier local, sans passer par S3 — pratique pour les tests."""
    with open(local_path, "rb") as f:
        raw_bytes = f.read()
    text = _extract_text(local_path, raw_bytes)
    return _ingest_text(text, user_id, filename=os.path.basename(local_path))


def ingest_bytes(raw_bytes: bytes, user_id: str, filename: str, max_chunks: int = MAX_CHUNKS_SYNC) -> dict:
    """Extraction + indexation à partir d'octets déjà en mémoire (pas de fichier
    local ni de re-téléchargement S3) — utilisé quand un document est joint
    directement à un message de chat. Le texte extrait est inclus dans le
    résultat pour être injecté tel quel dans le prompt de cette réponse.
    """
    text = _extract_text(filename, raw_bytes)
    result = _ingest_text(text, user_id, filename, max_chunks=max_chunks)
    result["extracted_text"] = text
    return result


def _embed_and_store_chunk(chunk: str, user_id: str, document_id: str) -> None:
    embedding = bedrock_client.generate_embedding(chunk)
    memory.save_memory_embedding(
        user_id=user_id,
        content=chunk,
        embedding=embedding,
        source_type="document",
        source_id=document_id,
    )


def _ingest_text(text: str, user_id: str, filename: str, max_chunks: int = MAX_CHUNKS_SYNC) -> dict:
    document_id = str(uuid.uuid4())
    chunks = _chunk_text(text)
    truncated = len(chunks) > max_chunks
    chunks = chunks[:max_chunks]

    # Appels Bedrock + écritures CockroachDB en parallèle (I/O-bound, chaque
    # thread ouvre sa propre connexion psycopg2) — un document de 20 chunks
    # traité en série dépassait les 30s de timeout API Gateway.
    with ThreadPoolExecutor(max_workers=EMBEDDING_WORKERS) as pool:
        list(pool.map(lambda c: _embed_and_store_chunk(c, user_id, document_id), chunks))

    return {
        "filename": filename,
        "document_id": document_id,
        "chunks_stored": len(chunks),
        "truncated": truncated,
    }


if __name__ == "__main__":
    # Test local : ingère un fichier texte et un PDF passés en argument.
    # Usage : python ingest.py mon_fichier.txt mon_fichier.pdf
    import sys

    if len(sys.argv) < 2:
        print("Usage: python ingest.py <fichier1> [fichier2 ...]")
        sys.exit(1)

    test_user_id = memory.get_or_create_user("stanley")
    for path in sys.argv[1:]:
        result = ingest_local_document(path, test_user_id)
        print(f"Ingéré : {result}")
