"""
Point d'entrée Lambda. Reçoit une requête utilisateur, construit le
contexte depuis CockroachDB, appelle Claude (Bedrock), gère les éventuels
appels d'outils MCP décidés par le modèle, puis persiste la conversation
et son embedding.
"""
import base64
import json
import re
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
MIN_PASSWORD_LENGTH = 8

# Un message avec pièce jointe fait, dans la même requête HTTP : upload S3 +
# extraction + chunking/embeddings + appel Claude. Budget de chunks plus
# serré que l'upload seul (MAX_CHUNKS_SYNC dans ingest.py) pour laisser de
# la marge au reste du tour sous les 30s d'intégration API Gateway.
MAX_CHUNKS_WITH_MESSAGE = 24
DOCUMENT_CONTEXT_CHARS = 6000  # texte brut injecté directement dans le prompt, pas juste indexé

SYSTEM_PROMPT = """You are Continuum, an AI assistant with persistent memory stored in CockroachDB,
specialized in the laws of Benin. Your knowledge base ("Relevant memories") includes both the
user's own conversation history and a global corpus of ~1500 promulgated Benin laws (scraped from
sgg.gouv.bj, OCR'd where needed). Each memory below is labeled with its source — a law entry looks
like "[LAW N° <number> — <title>]" followed by the actual excerpt text; a past-conversation entry
is labeled "[Your past conversation with this user]".

Hard rule against fabrication: you may ONLY state a specific article number, section number, or
exact/quoted legal text if that exact number or text literally appears in the excerpt below it in
"Relevant memories". Never construct a plausible-sounding article number or quote that isn't
actually present in the retrieved text, even if it would make the answer more satisfying — a
fabricated citation is a worse outcome than admitting you don't have it. If none of the retrieved
law excerpts actually address the question (check this honestly — matching keywords isn't the same
as being on-topic), say plainly that you don't have a specific law on this in your database, rather
than guessing or generalizing from what similar laws elsewhere might say.
You are not a lawyer and this is not legal advice — for any decision with real consequences, say so
and recommend the user confirm with a qualified legal professional.
Use the provided context (recent history + relevant memories) to answer in a way that is
personalized and consistent with previous exchanges.
If a document is attached to the message (the "Document attached to this message" section),
base your answer primarily on its content rather than on memory search.
If you need to inspect the database schema or run a verification query, use the MCP tools
available to you."""


UNVERIFIED_CITATION_NOTE = (
    "\n\n⚠️ *One or more article numbers cited above could not be verified against the "
    "retrieved source text — this may be inaccurate. Please confirm with a qualified legal "
    "professional before relying on it.*"
)


CITATION_DISTANCE_THRESHOLD = 11.5  # au-delà, la loi la plus proche récupérée n'est probablement pas vraiment sur le sujet


def _flag_unverified_citations(reply_text: str, similar_memories: list[dict]) -> str:
    """Garde-fou contre les hallucinations de citations : Claude a montré qu'il peut citer
    un numéro d'article précis ('Article 293 stipule que...') qui n'apparaît nulle part dans
    le texte réellement récupéré — la seule instruction de prompt ne suffit pas à l'empêcher
    de façon fiable (reproduit plusieurs fois sur le même scénario en test).

    Une vérification texte-à-texte (le numéro cité apparaît-il dans les chunks récupérés ?) a
    été essayée et abandonnée : l'OCR déforme trop souvent les chiffres eux-mêmes (ex. "53"
    lu "S3"), ce qui produit des faux positifs sur des citations réellement bien ancrées. On
    se base à la place sur la distance vectorielle de la meilleure correspondance "law" : dans
    nos tests, les citations correctement ancrées venaient de correspondances à distance
    ~10.9-11.2, les citations fabriquées de correspondances à distance ~12.3+ (rien de vraiment
    pertinent trouvé). Seuil empirique, pas une preuve formelle — d'où un avertissement, pas un
    blocage de la réponse.
    """
    if not re.search(r"[Aa]rticles?\s+\d+", reply_text):
        return reply_text

    law_distances = [m["distance"] for m in similar_memories if m["source_type"] == "law"]
    if not law_distances or min(law_distances) > CITATION_DISTANCE_THRESHOLD:
        return reply_text + UNVERIFIED_CITATION_NOTE
    return reply_text


def build_prompt_context(user_id: str, thread_id: str, user_message: str, document_context: str | None = None) -> tuple[str, list[dict]]:
    """Assemble l'historique récent du fil courant + les souvenirs
    sémantiquement proches, tous fils confondus (+ le texte d'un document
    tout juste joint au message, le cas échéant).

    Retourne aussi la liste des souvenirs utilisés (pour affichage côté
    frontend — rend la recherche vectorielle visible, pas juste interne).
    """
    recent = memory.get_recent_conversations(user_id, thread_id, limit=10)
    history_text = "\n".join(f"[{r['role']}] {r['content']}" for r in recent)

    query_embedding = bedrock_client.generate_embedding(user_message)
    similar = memory.search_similar_memories(user_id, query_embedding, top_k=5)

    law_ids = [str(m["source_id"]) for m in similar if m["source_type"] == "law" and m["source_id"]]
    laws_meta = memory.get_laws_by_ids(law_ids) if law_ids else {}

    memory_lines = []
    for m in similar:
        law = laws_meta.get(str(m["source_id"])) if m["source_type"] == "law" else None
        if law:
            label = f"[LAW N° {law['law_number']} — {law['title']}]"
        elif m["source_type"] == "document":
            label = "[Excerpt from a document the user uploaded]"
        else:
            label = "[Your past conversation with this user]"
        memory_lines.append(f"- {label} {m['content']} (distance={m['distance']:.3f})")
    memories_text = "\n".join(memory_lines)

    document_block = f"## Document attached to this message\n{document_context}\n\n" if document_context else ""

    context_block = (
        f"{document_block}"
        f"## Recent history\n{history_text or '(none)'}\n\n"
        f"## Relevant memories\n{memories_text or '(none)'}\n\n"
        f"## Current message\n{user_message}"
    )
    return context_block, similar


def run_agent_turn(user_id: str, thread_id: str, user_message: str, document_context: str | None = None) -> tuple[str, list[dict], str, str]:
    context_block, similar_memories = build_prompt_context(user_id, thread_id, user_message, document_context)
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
    final_text = _flag_unverified_citations(final_text, similar_memories)

    # Persistance de la mémoire : conversation (scopée au fil) + embedding (global)
    user_conversation_id = memory.save_conversation(user_id, "user", user_message, thread_id)
    assistant_conversation_id = memory.save_conversation(user_id, "assistant", final_text, thread_id)
    memory.touch_thread(thread_id, title_candidate=user_message)

    embedding = bedrock_client.generate_embedding(f"{user_message}\n{final_text}")
    memory.save_memory_embedding(user_id, f"{user_message}\n{final_text}", embedding)

    return final_text, similar_memories, user_conversation_id, assistant_conversation_id


def _response(status: int, payload: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _handle_signup(body: dict) -> dict:
    name = (body.get("name") or "").strip()
    password = body.get("password") or ""
    if len(name) < 2:
        return _response(400, {"error": "Name must be at least 2 characters."})
    if len(password) < MIN_PASSWORD_LENGTH:
        return _response(400, {"error": f"Password must be at least {MIN_PASSWORD_LENGTH} characters."})

    try:
        user_id = memory.create_account(name, password)
    except memory.AuthError as exc:
        return _response(409, {"error": str(exc)})

    token = memory.create_session(user_id)
    return _response(200, {"user_id": user_id, "name": name, "session_token": token})


def _handle_login(body: dict) -> dict:
    name = (body.get("name") or "").strip()
    password = body.get("password") or ""

    try:
        user_id = memory.verify_credentials(name, password)
    except memory.AuthError as exc:
        return _response(401, {"error": str(exc)})

    token = memory.create_session(user_id)
    return _response(200, {"user_id": user_id, "name": name, "session_token": token})


def _handle_new_thread(body: dict) -> dict:
    try:
        user_id = memory.get_user_from_session(body.get("session_token"))
    except memory.AuthError as exc:
        return _response(401, {"error": str(exc)})

    thread_id = memory.create_thread(user_id)
    return _response(200, {"thread_id": thread_id})


def _handle_list_threads(body: dict) -> dict:
    try:
        user_id = memory.get_user_from_session(body.get("session_token"))
    except memory.AuthError as exc:
        return _response(401, {"error": str(exc)})

    threads = memory.list_threads(user_id)
    return _response(200, {
        "threads": [
            {
                "thread_id": str(t["thread_id"]),
                "title": t["title"],
                "created_at": t["created_at"].isoformat(),
                "updated_at": t["updated_at"].isoformat(),
            }
            for t in threads
        ]
    })


def _handle_thread_messages(body: dict) -> dict:
    try:
        user_id = memory.get_user_from_session(body.get("session_token"))
    except memory.AuthError as exc:
        return _response(401, {"error": str(exc)})

    thread_id = body.get("thread_id")
    if not thread_id or not memory.verify_thread_owner(thread_id, user_id):
        return _response(403, {"error": "This conversation does not exist or does not belong to you."})

    messages = memory.get_thread_messages(user_id, thread_id)
    return _response(200, {
        "messages": [
            {
                "conversation_id": str(m["conversation_id"]),
                "role": m["role"],
                "content": m["content"],
                "created_at": m["created_at"].isoformat(),
            }
            for m in messages
        ]
    })


def _handle_upload(body: dict) -> dict:
    try:
        user_id = memory.get_user_from_session(body.get("session_token"))
    except memory.AuthError as exc:
        return _response(401, {"error": str(exc)})

    filename = body.get("filename")
    file_b64 = body.get("file_base64")

    if not filename or not file_b64:
        return _response(400, {"error": "The 'filename' and 'file_base64' fields are required."})
    if not filename.lower().endswith(ALLOWED_UPLOAD_EXTENSIONS):
        return _response(400, {"error": f"Unsupported extension. Accepted formats: {', '.join(ALLOWED_UPLOAD_EXTENSIONS)}"})

    try:
        raw_bytes = base64.b64decode(file_b64)
    except Exception:
        return _response(400, {"error": "Invalid file_base64 (decoding failed)."})

    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        return _response(400, {"error": f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)."})

    key = f"{user_id}/{uuid.uuid4()}_{filename}"
    ingest.upload_bytes_to_s3(raw_bytes, key)
    result = ingest.ingest_document_from_s3(key, user_id)
    result["filename"] = filename  # nom original, pas la clé S3 (préfixée user_id/uuid)

    return _response(200, {**result, "user_id": user_id})


def _ingest_attachment_for_chat(user_id: str, attachment: dict) -> tuple[str, dict]:
    """Upload + indexe une pièce jointe envoyée avec un message de chat, et
    retourne son texte brut (pour l'injecter directement dans le prompt de
    cette réponse) + les métadonnées d'indexation.
    """
    filename = attachment.get("filename")
    file_b64 = attachment.get("file_base64")

    if not filename or not file_b64:
        raise ValueError("The attachment's 'filename' and 'file_base64' fields are required.")
    if not filename.lower().endswith(ALLOWED_UPLOAD_EXTENSIONS):
        raise ValueError(f"Unsupported extension. Accepted formats: {', '.join(ALLOWED_UPLOAD_EXTENSIONS)}")

    try:
        raw_bytes = base64.b64decode(file_b64)
    except Exception:
        raise ValueError("Invalid file_base64 (decoding failed).")

    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError(f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")

    key = f"{user_id}/{uuid.uuid4()}_{filename}"
    ingest.upload_bytes_to_s3(raw_bytes, key)
    result = ingest.ingest_bytes(raw_bytes, user_id, filename, max_chunks=MAX_CHUNKS_WITH_MESSAGE)

    document_context = result["extracted_text"][:DOCUMENT_CONTEXT_CHARS]
    attachment_info = {
        "filename": filename,
        "document_id": result["document_id"],
        "chunks_stored": result["chunks_stored"],
        "truncated": result["truncated"],
    }
    return document_context, attachment_info


def _run_chat_turn(user_id: str, thread_id: str, body: dict) -> dict:
    user_message = body.get("message")
    attachment = body.get("attachment")
    if not user_message:
        return _response(400, {"error": "The 'message' field is required."})

    document_context = None
    attachment_info = None
    if attachment:
        try:
            document_context, attachment_info = _ingest_attachment_for_chat(user_id, attachment)
        except ValueError as exc:
            return _response(400, {"error": str(exc)})

    reply, similar_memories, user_message_id, assistant_message_id = run_agent_turn(
        user_id, thread_id, user_message, document_context
    )

    law_ids = [str(m["source_id"]) for m in similar_memories if m["source_type"] == "law" and m["source_id"]]
    laws_meta = memory.get_laws_by_ids(law_ids) if law_ids else {}

    memories_used = []
    for m in similar_memories:
        entry = {
            "content": m["content"][:280],
            "source_type": m["source_type"],
            "distance": round(float(m["distance"]), 4),
        }
        law = laws_meta.get(str(m["source_id"])) if m["source_type"] == "law" else None
        if law:
            entry["law_number"] = law["law_number"]
            entry["law_title"] = law["title"]
        memories_used.append(entry)

    payload = {
        "reply": reply,
        "user_id": user_id,
        "thread_id": thread_id,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "memories_used": memories_used,
    }
    if attachment_info:
        payload["attachment_info"] = attachment_info
    return _response(200, payload)


def _handle_chat(body: dict) -> dict:
    try:
        user_id = memory.get_user_from_session(body.get("session_token"))
    except memory.AuthError as exc:
        return _response(401, {"error": str(exc)})

    thread_id = body.get("thread_id")
    if thread_id:
        if not memory.verify_thread_owner(thread_id, user_id):
            return _response(403, {"error": "This conversation does not exist or does not belong to you."})
    else:
        # Pas de fil fourni (ex. tout premier message) : on en ouvre un.
        thread_id = memory.create_thread(user_id)

    return _run_chat_turn(user_id, thread_id, body)


def _handle_edit_message(body: dict) -> dict:
    try:
        user_id = memory.get_user_from_session(body.get("session_token"))
    except memory.AuthError as exc:
        return _response(401, {"error": str(exc)})

    thread_id = body.get("thread_id")
    conversation_id = body.get("conversation_id")
    if not thread_id or not conversation_id or not body.get("message"):
        return _response(400, {"error": "The 'thread_id', 'conversation_id' and 'message' fields are required."})
    if not memory.verify_thread_owner(thread_id, user_id):
        return _response(403, {"error": "This conversation does not exist or does not belong to you."})

    # Édition d'un message déjà envoyé : on retire cet échange et tout ce
    # qui suit dans le fil, puis on rejoue depuis là avec le texte modifié.
    memory.delete_messages_from(user_id, thread_id, conversation_id)
    return _run_chat_turn(user_id, thread_id, body)


def lambda_handler(event, context):
    http_method = event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod")
    if http_method == "OPTIONS":
        # Préflight CORS : API Gateway le transmet à la Lambda (route $default),
        # il faut répondre 200 sans passer par la validation métier ci-dessous.
        return {"statusCode": 200, "headers": CORS_HEADERS, "body": ""}

    try:
        body = json.loads(event.get("body", "{}")) if "body" in event else event
        action = body.get("action")
        if action == "signup":
            return _handle_signup(body)
        if action == "login":
            return _handle_login(body)
        if action == "new_thread":
            return _handle_new_thread(body)
        if action == "list_threads":
            return _handle_list_threads(body)
        if action == "thread_messages":
            return _handle_thread_messages(body)
        if action == "edit_message":
            return _handle_edit_message(body)
        if action == "upload":
            return _handle_upload(body)
        return _handle_chat(body)
    except Exception as exc:  # noqa: BLE001 — toujours répondre proprement au client
        print(f"[ERROR] {exc}")  # capté par CloudWatch Logs
        return _response(500, {"error": "An internal error occurred. Please try again in a moment."})


if __name__ == "__main__":
    # Test local, sans Lambda : python -m agent.handler
    signup = lambda_handler({"action": "signup", "name": "stanley", "password": "test1234"}, None)
    print(signup)
    token = json.loads(signup["body"]).get("session_token")
    chat1 = lambda_handler({"session_token": token, "message": "Salut, tu te souviens de moi ?"}, None)
    print(chat1)
    thread_id = json.loads(chat1["body"]).get("thread_id")
    print(lambda_handler({"session_token": token, "thread_id": thread_id, "message": "Et là ?"}, None))
    print(lambda_handler({"action": "list_threads", "session_token": token}, None))
