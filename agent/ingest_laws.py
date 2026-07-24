"""
Ingestion en masse des lois du Bénin (sgg.gouv.bj) dans la base de
connaissances globale de l'agent.

Flux, par loi : page listing -> PDF téléchargé -> texte extrait (pypdf, ou
OCR via Amazon Textract si le PDF est un scan sans couche texte — le cas
de la grande majorité des lois sur ce site) -> chunké -> embeddé (Titan)
-> stocké dans memory_embeddings (source_type='law', user_id NULL —
connaissance partagée, pas propre à un utilisateur) + une ligne dans
`laws` pour les métadonnées (numéro, titre, date, lien S3).

Script batch autonome (pas exécuté via la Lambda — un run complet dépasse
largement les 30s d'API Gateway, et l'OCR ajoute encore plus de latence
par document). Reprenable : une loi déjà présente dans `laws` (par
law_number) est sautée, donc une interruption/relance ne redouble pas le
travail déjà fait.

Le listing (pagination sur sgg.gouv.bj) reste séquentiel avec un délai —
par respect pour ce serveur public. L'ingestion elle-même (S3, Textract,
Bedrock — nos propres ressources AWS) est parallélisée par loi.

Usage :
    python ingest_laws.py                    # scrape + ingère tout
    python ingest_laws.py --pages 1-3         # limite à un sous-ensemble de pages (tests)
    python ingest_laws.py --dry-run           # scrape seulement, n'ingère rien (vérifie le total)
    python ingest_laws.py --workers 8         # parallélisme de l'ingestion (def. 5)
"""
import argparse
import html
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
import requests

import bedrock_client
import ingest
import memory

BASE_URL = "https://sgg.gouv.bj"
LISTING_URL = f"{BASE_URL}/documentheque/lois/"
HEADERS = {"User-Agent": "Continuum-Hackathon-Bot/1.0 (recherche academique - CockroachDB x AWS Hackathon)"}

REQUEST_DELAY = 0.3   # entre deux requêtes vers sgg.gouv.bj — respect du serveur public
MAX_CHUNKS_PER_LAW = 60
MIN_TEXT_CHARS = 50   # sous ce seuil, on considère le PDF comme un scan -> bascule OCR
DEFAULT_WORKERS = 20  # lois traitées en parallèle (download + OCR — I/O-bound, l'attente Textract ne coûte pas de CPU)
EMBED_WORKERS = 16    # pool PARTAGÉ pour les appels d'embedding Bedrock, toutes lois confondues —
                       # découplé du parallélisme "par loi" pour borner le débit réel vers Bedrock
                       # (un pool par loi imbriqué dans un pool par loi ferait exploser la concurrence)
TEXTRACT_POLL_INTERVAL = 2
TEXTRACT_MAX_WAIT = 90

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
_textract = boto3.client("textract", region_name=AWS_REGION)
_embed_pool = ThreadPoolExecutor(max_workers=EMBED_WORKERS)

ENTRY_RE = re.compile(
    r"href='/doc/([^']+)/'\s+class='doc-title[^']*'[^>]*>([^<]+)</a></h3>"
    r"(?:\s*<p class='black[^']*doc-desc[^']*'[^>]*>([^<]*)</p>)?",
    re.DOTALL,
)

MONTHS_FR = {
    "janv": 1, "févr": 2, "fevr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6,
    "juil": 7, "août": 8, "aout": 8, "sept": 9, "oct": 10, "nov": 11, "déc": 12, "dec": 12,
}

_print_lock = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def _with_retry(fn, *args, retries: int = 4, **kwargs):
    """CockroachDB (sérialisable) rejette parfois une transaction en cas de
    contention sous forte concurrence (SQLSTATE 40001) — un retry avec un
    léger backoff suffit, c'est le comportement attendu, pas une vraie panne."""
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if "40001" not in str(getattr(exc, "pgcode", "")) and "RETRY_SERIALIZABLE" not in str(exc):
                raise
            if attempt == retries - 1:
                raise
            time.sleep(0.3 * (attempt + 1))


def _parse_date(title: str):
    """Extrait une date du type 'du 16 mars 2026' ou 'du 03 févr. 2026' depuis le titre. None si non trouvée."""
    m = re.search(r"du (\d{1,2})\s+([a-zA-Zéû.]+)\.?\s+(\d{4})", title)
    if not m:
        return None
    day, month_raw, year = m.groups()
    month = MONTHS_FR.get(month_raw.lower().rstrip("."))
    if not month:
        return None
    return f"{year}-{month:02d}-{int(day):02d}"


def _fetch(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def list_all_laws(start_page: int = 1, end_page: int | None = None) -> list[dict]:
    """Parcourt les pages de listing (toutes, ou [start_page, end_page] si fourni —
    pratique pour tester sans crawler tout le site) et retourne les métadonnées."""
    laws = []
    page = start_page
    while end_page is None or page <= end_page:
        url = LISTING_URL if page == 1 else f"{LISTING_URL}{page}/"
        page_html = _fetch(url)
        matches = ENTRY_RE.findall(page_html)
        if not matches:
            break
        for slug, title_text, description in matches:
            law_number = slug.replace("loi-", "", 1)
            laws.append({
                "slug": slug,
                "law_number": law_number,
                "title": html.unescape(title_text.strip()),
                "description": html.unescape(description.strip()) if description and description.strip() else None,
                "promulgated_on": _parse_date(title_text),
                "source_url": f"{BASE_URL}/doc/{slug}/",
                "download_url": f"{BASE_URL}/doc/{slug}/download",
            })
        _log(f"[list] page {page}: {len(matches)} lois (total {len(laws)})")
        page += 1
        time.sleep(REQUEST_DELAY)
    return laws


def _ocr_via_textract(bucket: str, key: str) -> str:
    """OCR asynchrone (nécessaire pour les PDF multi-pages) via Amazon Textract,
    sur un objet déjà présent dans S3. Utilisé pour les lois scannées (la
    grande majorité du corpus) — pypdf ne trouve aucune couche texte."""
    job_id = _textract.start_document_text_detection(
        DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}}
    )["JobId"]

    waited = 0
    result = None
    while waited < TEXTRACT_MAX_WAIT:
        time.sleep(TEXTRACT_POLL_INTERVAL)
        waited += TEXTRACT_POLL_INTERVAL
        result = _textract.get_document_text_detection(JobId=job_id)
        status = result["JobStatus"]
        if status == "SUCCEEDED":
            break
        if status == "FAILED":
            raise RuntimeError(f"Textract job failed for {key}")
    else:
        raise TimeoutError(f"Textract job timed out for {key}")

    lines = [b["Text"] for b in result.get("Blocks", []) if b["BlockType"] == "LINE"]
    next_token = result.get("NextToken")
    while next_token:
        page = _textract.get_document_text_detection(JobId=job_id, NextToken=next_token)
        lines.extend(b["Text"] for b in page.get("Blocks", []) if b["BlockType"] == "LINE")
        next_token = page.get("NextToken")
    return "\n".join(lines)


def _embed_and_store_law_chunk(chunk: str, law_id: str) -> None:
    embedding = bedrock_client.generate_embedding(chunk)
    _with_retry(
        memory.save_memory_embedding,
        user_id=None,
        content=chunk,
        embedding=embedding,
        source_type="law",
        source_id=law_id,
    )


def ingest_one_law(entry: dict) -> str:
    """Télécharge, extrait (pypdf, repli OCR Textract si nécessaire), chunke,
    embedde et stocke une loi. Retourne un statut court pour le log."""
    if memory.get_law_by_number(entry["law_number"]):
        return "skip (déjà en base)"

    resp = requests.get(entry["download_url"], headers=HEADERS, timeout=30)
    resp.raise_for_status()
    raw_bytes = resp.content

    s3_key = f"laws/{entry['slug']}.pdf"
    ingest.upload_bytes_to_s3(raw_bytes, s3_key)

    text = ingest._extract_text(entry["slug"] + ".pdf", raw_bytes)
    used_ocr = False
    if len(text.strip()) < MIN_TEXT_CHARS:
        text = _ocr_via_textract(ingest.S3_BUCKET, s3_key)
        used_ocr = True

    if len(text.strip()) < MIN_TEXT_CHARS:
        return "skip (aucun texte exploitable, même après OCR)"

    law_id = _with_retry(
        memory.save_law,
        law_number=entry["law_number"],
        title=entry["title"],
        description=entry["description"],
        promulgated_on=entry["promulgated_on"],
        source_url=entry["source_url"],
        s3_key=s3_key,
    )

    chunks = ingest._chunk_text(text)[:MAX_CHUNKS_PER_LAW]
    futures = [_embed_pool.submit(_embed_and_store_law_chunk, c, law_id) for c in chunks]
    for f in futures:
        f.result()

    tag = "OCR" if used_ocr else "texte natif"
    return f"ingéré ({len(chunks)} chunks, {tag})"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", help="ex. 1-3 pour limiter aux premières pages (tests)")
    parser.add_argument("--dry-run", action="store_true", help="scrape seulement, n'ingère rien")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="lois traitées en parallèle")
    args = parser.parse_args()

    _log("=== Étape 1 : listing des lois ===")
    if args.pages:
        lo, hi = (int(x) for x in args.pages.split("-"))
        all_laws = list_all_laws(start_page=lo, end_page=hi)
    else:
        all_laws = list_all_laws()
    _log(f"Total trouvé : {len(all_laws)} lois")

    if args.dry_run:
        _log("--dry-run : arrêt avant ingestion.")
        return

    _log(f"=== Étape 2 : ingestion ({args.workers} lois en parallèle) ===")
    done, skipped, errors = 0, 0, 0
    total = len(all_laws)
    completed = 0

    def _process(i_entry):
        i, entry = i_entry
        try:
            status = ingest_one_law(entry)
        except Exception as exc:  # noqa: BLE001 — on continue sur le reste du corpus
            status = f"ERREUR: {exc}"
        return i, entry, status

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_process, item) for item in enumerate(all_laws, 1)]
        for future in as_completed(futures):
            i, entry, status = future.result()
            completed += 1
            if status.startswith("skip"):
                skipped += 1
            elif status.startswith("ERREUR"):
                errors += 1
            else:
                done += 1
            _log(f"[{completed}/{total}] {entry['law_number']} — {status}")

    _log(f"=== Terminé : {done} ingérées, {skipped} sautées, {errors} erreurs ===")
    _log(f"Total en base : {memory.count_laws()} lois")


if __name__ == "__main__":
    sys.exit(main())
