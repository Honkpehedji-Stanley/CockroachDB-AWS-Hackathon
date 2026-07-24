"""
Module mémoire de l'agent.

Deux canaux d'accès à CockroachDB, volontairement distincts :

1. SQL direct (psycopg2) : lecture/écriture rapide du contexte, de
   l'historique de conversation, des tâches, et recherche vectorielle
   (Distributed Vector Indexing).
2. MCP Server managé (HTTP/JSON-RPC) : exposé comme un OUTIL que Claude
   peut appeler lui-même pendant son raisonnement (introspection du
   schéma, requêtes ad hoc, état du cluster) — c'est l'agent qui décide
   de s'en servir, pas juste un script de test.
"""
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import psycopg2
import psycopg2.extras
import requests

SESSION_TTL = timedelta(days=7)

DATABASE_URL = os.environ["DATABASE_URL"]

MCP_URL = "https://cockroachlabs.cloud/mcp"
MCP_CLUSTER_ID = os.environ["COCKROACH_CLUSTER_ID"]
MCP_API_KEY = os.environ["COCKROACH_MCP_API_KEY"]
MCP_DATABASE = os.environ.get("COCKROACH_DATABASE", "defaultdb")


def get_connection():
    return psycopg2.connect(DATABASE_URL)


# ---------------------------------------------------------------------
# Canal 1 — SQL direct
# ---------------------------------------------------------------------

def get_or_create_user(name: str) -> str:
    """Retourne le user_id existant ou en crée un nouveau. Retourne un UUID (str)."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id FROM user_context WHERE name = %s", (name,))
        row = cur.fetchone()
        if row:
            return str(row[0])
        new_id = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO user_context (user_id, name) VALUES (%s, %s)",
            (new_id, name),
        )
        conn.commit()
        return new_id


class AuthError(Exception):
    """Nom déjà pris, identifiants invalides, ou session absente/expirée."""


def create_account(name: str, password: str) -> str:
    """Crée un compte avec mot de passe (haché, jamais stocké en clair). Retourne le user_id."""
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM user_context WHERE name = %s", (name,))
        if cur.fetchone():
            raise AuthError("This name is already taken.")
        new_id = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO user_context (user_id, name, password_hash) VALUES (%s, %s, %s)",
            (new_id, name, password_hash),
        )
        conn.commit()
        return new_id


def verify_credentials(name: str, password: str) -> str:
    """Vérifie nom + mot de passe. Retourne le user_id si valide, lève AuthError sinon."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id, password_hash FROM user_context WHERE name = %s", (name,))
        row = cur.fetchone()
        if not row or not row[1]:
            raise AuthError("Incorrect name or password.")
        user_id, password_hash = row
        if not bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8")):
            raise AuthError("Incorrect name or password.")
        return str(user_id)


def create_session(user_id: str) -> str:
    """Émet un jeton de session opaque, stocké côté serveur avec expiration."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + SESSION_TTL
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions (session_token, user_id, expires_at) VALUES (%s, %s, %s)",
            (token, user_id, expires_at),
        )
        conn.commit()
    return token


def get_user_from_session(token: str) -> str:
    """Résout un jeton de session en user_id. Lève AuthError si absent/expiré."""
    if not token:
        raise AuthError("Missing session — please sign in again.")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT user_id FROM sessions WHERE session_token = %s AND expires_at > now()",
            (token,),
        )
        row = cur.fetchone()
        if not row:
            raise AuthError("Session expired or invalid — please sign in again.")
        return str(row[0])


# ---------------------------------------------------------------------
# Fils de discussion (threads) — une conversation distincte façon
# ChatGPT/Claude. `get_recent_conversations` (contexte envoyé au modèle)
# est scopé au fil courant ; la recherche vectorielle (search_similar_memories)
# reste globale à l'utilisateur, donc l'agent peut rappeler un souvenir
# d'un autre fil même si son contexte conversationnel, lui, est cloisonné.
# ---------------------------------------------------------------------

def create_thread(user_id: str) -> str:
    thread_id = str(uuid.uuid4())
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO threads (thread_id, user_id) VALUES (%s, %s)",
            (thread_id, user_id),
        )
        conn.commit()
    return thread_id


def verify_thread_owner(thread_id: str, user_id: str) -> bool:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM threads WHERE thread_id = %s AND user_id = %s", (thread_id, user_id))
        return cur.fetchone() is not None


def list_threads(user_id: str, limit: int = 50) -> list[dict]:
    with get_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT thread_id, title, created_at, updated_at
            FROM threads
            WHERE user_id = %s
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (user_id, limit),
        )
        return cur.fetchall()


def touch_thread(thread_id: str, title_candidate: str) -> None:
    """Met à jour `updated_at` (fait remonter le fil dans la liste) et fixe
    le titre une seule fois, sur le tout premier message (COALESCE)."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE threads SET updated_at = now(), title = COALESCE(title, %s) WHERE thread_id = %s",
            (title_candidate[:60], thread_id),
        )
        conn.commit()


def get_thread_messages(user_id: str, thread_id: str, limit: int = 200) -> list[dict]:
    with get_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT conversation_id, role, content, created_at
            FROM conversations
            WHERE user_id = %s AND thread_id = %s
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (user_id, thread_id, limit),
        )
        return cur.fetchall()


def delete_messages_from(user_id: str, thread_id: str, from_conversation_id: str) -> None:
    """Supprime un message et tout ce qui le suit dans le fil (édition d'un
    message déjà envoyé = on retire l'ancien échange puis on rejoue depuis là)."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM conversations
            WHERE user_id = %s AND thread_id = %s
              AND created_at >= (
                  SELECT created_at FROM conversations
                  WHERE conversation_id = %s AND user_id = %s AND thread_id = %s
              )
            """,
            (user_id, thread_id, from_conversation_id, user_id, thread_id),
        )
        conn.commit()


def get_recent_conversations(user_id: str, thread_id: str, limit: int = 10) -> list[dict]:
    with get_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT role, content, created_at
            FROM conversations
            WHERE user_id = %s AND thread_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (user_id, thread_id, limit),
        )
        rows = cur.fetchall()
        return list(reversed(rows))  # ordre chronologique


def save_conversation(user_id: str, role: str, content: str, thread_id: str) -> str:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversations (user_id, role, content, thread_id) VALUES (%s, %s, %s, %s) RETURNING conversation_id",
            (user_id, role, content, thread_id),
        )
        conversation_id = cur.fetchone()[0]
        conn.commit()
        return str(conversation_id)


def save_memory_embedding(
    user_id: str,
    content: str,
    embedding: list[float],
    source_type: str = "conversation",
    source_id: str | None = None,
) -> None:
    """
    Stocke le texte ET son embedding dans la même transaction (mémoire vectorielle).
    `source_id` identifie le document d'origine (UUID) pour les chunks issus
    de l'ingestion de documents (Phase 3) — permet de retrouver tous les
    chunks d'un même document si besoin.
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO memory_embeddings (user_id, source_type, source_id, content, embedding)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (user_id, source_type, source_id, content, embedding),
        )
        conn.commit()


def search_similar_memories(
    user_id: str,
    query_embedding: list[float],
    top_k: int = 5,
    source_type: str | None = None,
) -> list[dict]:
    """
    Recherche par similarité vectorielle (distance L2, cohérente avec
    l'index `vector_l2_ops` créé sur memory_embeddings).
    `source_type` (optionnel) filtre sur 'conversation', 'document' ou 'law'.

    Cherche à la fois dans la mémoire personnelle de l'utilisateur
    (user_id = %s) et dans la base de connaissances globale — les lois
    ingérées avec user_id NULL, partagées par tous les utilisateurs.
    """
    query = """
        SELECT content, source_type, source_id, created_at,
               embedding <-> %s::VECTOR AS distance
        FROM memory_embeddings
        WHERE (user_id = %s OR user_id IS NULL)
    """
    params: list = [query_embedding, user_id]
    if source_type:
        query += " AND source_type = %s"
        params.append(source_type)
    query += " ORDER BY distance ASC LIMIT %s"
    params.append(top_k)

    with get_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        return cur.fetchall()


# ---------------------------------------------------------------------
# Base de connaissances globale — lois du Bénin (scrapées, pas propres à
# un utilisateur). Voir agent/ingest_laws.py pour le pipeline d'ingestion.
# ---------------------------------------------------------------------

def get_law_by_number(law_number: str) -> dict | None:
    """Utilisé par le script d'ingestion pour la reprise (skip si déjà en base)."""
    with get_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT law_id FROM laws WHERE law_number = %s", (law_number,))
        return cur.fetchone()


def save_law(
    law_number: str,
    title: str,
    description: str | None,
    promulgated_on,
    source_url: str,
    s3_key: str,
) -> str:
    law_id = str(uuid.uuid4())
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO laws (law_id, law_number, title, description, promulgated_on, source_url, s3_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (law_id, law_number, title, description, promulgated_on, source_url, s3_key),
        )
        conn.commit()
    return law_id


def count_laws() -> int:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM laws")
        return cur.fetchone()[0]


def get_laws_by_ids(law_ids: list[str]) -> dict[str, dict]:
    """Métadonnées (numéro, titre) pour un lot de law_id — utilisé pour citer
    la loi exacte dans le panneau Mémoire du frontend, pas juste afficher
    un chunk de texte brut sans contexte."""
    if not law_ids:
        return {}
    with get_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT law_id, law_number, title FROM laws WHERE law_id = ANY(%s)", (law_ids,))
        return {str(row["law_id"]): row for row in cur.fetchall()}


def create_task(user_id: str, title: str, metadata: dict | None = None) -> str:
    task_id = str(uuid.uuid4())
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tasks (task_id, user_id, title, metadata) VALUES (%s, %s, %s, %s)",
            (task_id, user_id, title, json.dumps(metadata or {})),
        )
        conn.commit()
    return task_id


# ---------------------------------------------------------------------
# Canal 2 — MCP Server (appelé par l'agent lui-même comme outil)
# ---------------------------------------------------------------------

def call_mcp_tool(tool_name: str, arguments: dict) -> dict:
    """
    Appelle un outil du MCP Server managé CockroachDB.
    Utilisé quand Claude décide, pendant son raisonnement, d'inspecter
    le schéma ou l'état du cluster plutôt que de répondre en mémoire.
    """
    arguments = {"database": MCP_DATABASE, **arguments}
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    response = requests.post(
        MCP_URL,
        headers={
            "Content-Type": "application/json",
            "mcp-cluster-id": MCP_CLUSTER_ID,
            "Authorization": f"Bearer {MCP_API_KEY}",
        },
        json=payload,
        timeout=10,
    )
    response.raise_for_status()
    # Le serveur répond en SSE ("event: message\ndata: {...}") ou en JSON pur
    # selon le client ; on gère les deux cas.
    text = response.text.strip()
    if text.startswith("event:"):
        text = text.split("data:", 1)[1].strip()
    return json.loads(text)


# Définition des outils MCP exposés à Claude, au format "tool use" Anthropic.
# On expose volontairement un sous-ensemble en lecture seule pour l'agent
# conversationnel (bonne pratique de sécurité / production readiness).
MCP_TOOLS_SCHEMA = [
    {
        "name": "list_tables",
        "description": "Liste les tables de la base de données CockroachDB.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_table_schema",
        "description": "Retourne le schéma détaillé (colonnes, index) d'une table CockroachDB.",
        "input_schema": {
            "type": "object",
            "properties": {"table": {"type": "string"}},
            "required": ["table"],
        },
    },
    {
        "name": "select_query",
        "description": "Exécute une requête SELECT en lecture seule sur CockroachDB.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]