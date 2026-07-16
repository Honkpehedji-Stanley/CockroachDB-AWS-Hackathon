"""
Point d'entrée Lambda. Reçoit une requête utilisateur, construit le
contexte depuis CockroachDB, appelle Claude (Bedrock), gère les éventuels
appels d'outils MCP décidés par le modèle, puis persiste la conversation
et son embedding.
"""
import base64
import json
import uuid

import bedrock_client
import ingest
import memory

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
    "Access-Control-Allow-Headers": "content-type",
}

MAX_UPLOAD_BYTES = 4 * 1024 * 1024  # 4 Mo décodés — marge sous la limite Lambda (~6 Mo côté payload base64)
ALLOWED_UPLOAD_EXTENSIONS = (".pdf", ".txt", ".md")

SYSTEM_PROMPT = """Tu es un assistant IA avec une mémoire persistante stockée dans CockroachDB.
Utilise le contexte fourni (historique récent + souvenirs pertinents) pour répondre de façon
personnalisée et cohérente avec les échanges précédents.
Si tu as besoin d'inspecter le schéma de la base ou d'exécuter une requête de vérification,
utilise les outils MCP mis à ta disposition."""


def build_prompt_context(user_id: str, user_message: str) -> tuple[str, list[dict]]:
    """Assemble l'historique récent + les souvenirs sémantiquement proches.

    Retourne aussi la liste des souvenirs utilisés (pour affichage côté
    frontend — rend la recherche vectorielle visible, pas juste interne).
    """
    recent = memory.get_recent_conversations(user_id, limit=10)
    history_text = "\n".join(f"[{r['role']}] {r['content']}" for r in recent)

    query_embedding = bedrock_client.generate_embedding(user_message)
    similar = memory.search_similar_memories(user_id, query_embedding, top_k=5)
    memories_text = "\n".join(f"- {m['content']} (distance={m['distance']:.3f})" for m in similar)

    context_block = (
        f"## Historique récent\n{history_text or '(aucun)'}\n\n"
        f"## Souvenirs pertinents\n{memories_text or '(aucun)'}\n\n"
        f"## Message actuel\n{user_message}"
    )
    return context_block, similar


def run_agent_turn(user_id: str, user_message: str) -> tuple[str, list[dict]]:
    context_block, similar_memories = build_prompt_context(user_id, user_message)
    messages = [{"role": "user", "content": context_block}]

    # Anthropic tool schema (le format attendu par l'API Messages/Bedrock)
    tools = memory.MCP_TOOLS_SCHEMA

    response = bedrock_client.call_claude(messages, tools=tools, system=SYSTEM_PROMPT)

    # Boucle "tool use" : Claude peut demander d'appeler un outil MCP
    # avant de donner sa réponse finale.
    while response.get("stop_reason") == "tool_use":
        tool_use_blocks = [b for b in response["content"] if b["type"] == "tool_use"]
        messages.append({"role": "assistant", "content": response["content"]})

        tool_results = []
        for block in tool_use_blocks:
            mcp_result = memory.call_mcp_tool(block["name"], block["input"])
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": json.dumps(mcp_result),
            })

        messages.append({"role": "user", "content": tool_results})
        response = bedrock_client.call_claude(messages, tools=tools, system=SYSTEM_PROMPT)

    final_text = "".join(b["text"] for b in response["content"] if b["type"] == "text")

    # Persistance de la mémoire : conversation + embedding
    memory.save_conversation(user_id, "user", user_message)
    memory.save_conversation(user_id, "assistant", final_text)

    embedding = bedrock_client.generate_embedding(f"{user_message}\n{final_text}")
    memory.save_memory_embedding(user_id, f"{user_message}\n{final_text}", embedding)

    return final_text, similar_memories


def _response(status: int, payload: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _handle_upload(body: dict) -> dict:
    user_name = body.get("user_name", "anonymous")
    filename = body.get("filename")
    file_b64 = body.get("file_base64")

    if not filename or not file_b64:
        return _response(400, {"error": "Les champs 'filename' et 'file_base64' sont requis."})
    if not filename.lower().endswith(ALLOWED_UPLOAD_EXTENSIONS):
        return _response(400, {"error": f"Extension non supportée. Formats acceptés : {', '.join(ALLOWED_UPLOAD_EXTENSIONS)}"})

    try:
        raw_bytes = base64.b64decode(file_b64)
    except Exception:
        return _response(400, {"error": "file_base64 invalide (décodage échoué)."})

    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        return _response(400, {"error": f"Fichier trop volumineux (max {MAX_UPLOAD_BYTES // (1024 * 1024)} Mo)."})

    user_id = memory.get_or_create_user(user_name)
    key = f"{user_id}/{uuid.uuid4()}_{filename}"
    ingest.upload_bytes_to_s3(raw_bytes, key)
    result = ingest.ingest_document_from_s3(key, user_id)
    result["filename"] = filename  # nom original, pas la clé S3 (préfixée user_id/uuid)

    return _response(200, {**result, "user_id": user_id})


def _handle_chat(body: dict) -> dict:
    user_name = body.get("user_name", "anonymous")
    user_message = body.get("message")
    if not user_message:
        return _response(400, {"error": "Le champ 'message' est requis."})

    user_id = memory.get_or_create_user(user_name)
    reply, similar_memories = run_agent_turn(user_id, user_message)

    memories_used = [
        {
            "content": m["content"][:280],
            "source_type": m["source_type"],
            "distance": round(float(m["distance"]), 4),
        }
        for m in similar_memories
    ]

    return _response(200, {"reply": reply, "user_id": user_id, "memories_used": memories_used})


def lambda_handler(event, context):
    http_method = event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod")
    if http_method == "OPTIONS":
        # Préflight CORS : API Gateway le transmet à la Lambda (route $default),
        # il faut répondre 200 sans passer par la validation métier ci-dessous.
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}

    try:
        body = json.loads(event.get("body", "{}")) if "body" in event else event
        if body.get("action") == "upload":
            return _handle_upload(body)
        return _handle_chat(body)
    except Exception as exc:  # noqa: BLE001 — toujours répondre proprement au client
        print(f"[ERROR] {exc}")  # capté par CloudWatch Logs
        return _response(500, {"error": "Une erreur interne est survenue. Réessaie dans un instant."})


if __name__ == "__main__":
    # Test local, sans Lambda : python -m agent.handler
    test_event = {"user_name": "stanley", "message": "Salut, tu te souviens de moi ?"}
    print(lambda_handler(test_event, None))
