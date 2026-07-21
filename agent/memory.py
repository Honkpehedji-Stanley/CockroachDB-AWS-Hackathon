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


def get_conversation_history(user_id: str, limit: int = 200) -> list[dict]:
    """Historique complet (jusqu'à `limit` messages), pour le panneau dédié du frontend —
    distinct de get_recent_conversations() qui alimente le contexte du prompt (limité à 10)."""
    with get_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT role, content, created_at
            FROM conversations
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (user_id, limit),
        )
        rows = cur.fetchall()
        return list(reversed(rows))


def get_recent_conversations(user_id: str, limit: int = 10) -> list[dict]:
    with get_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT role, content, created_at
            FROM conversations
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (user_id, limit),
        )
        rows = cur.fetchall()
        return list(reversed(rows))  # ordre chronologique


def save_conversation(user_id: str, role: str, content: str) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversations (user_id, role, content) VALUES (%s, %s, %s)",
            (user_id, role, content),
        )
        conn.commit()


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
    `source_type` (optionnel) filtre sur 'conversation' ou 'document'.
    """
    query = """
        SELECT content, source_type, source_id, created_at,
               embedding <-> %s::VECTOR AS distance
        FROM memory_embeddings
        WHERE user_id = %s
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