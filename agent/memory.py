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
import uuid

import psycopg2
import psycopg2.extras
import requests

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


def save_memory_embedding(user_id: str, content: str, embedding: list[float], source_type: str = "conversation") -> None:
    """Stocke le texte ET son embedding dans la même transaction (mémoire vectorielle)."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO memory_embeddings (user_id, source_type, content, embedding)
            VALUES (%s, %s, %s, %s)
            """,
            (user_id, source_type, content, embedding),
        )
        conn.commit()


def search_similar_memories(user_id: str, query_embedding: list[float], top_k: int = 5) -> list[dict]:
    """
    Recherche par similarité vectorielle (distance L2, cohérente avec
    l'index `vector_l2_ops` créé sur memory_embeddings).
    """
    with get_connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT content, source_type, created_at,
                   embedding <-> %s::VECTOR AS distance
            FROM memory_embeddings
            WHERE user_id = %s
            ORDER BY distance ASC
            LIMIT %s
            """,
            (query_embedding, user_id, top_k),
        )
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
