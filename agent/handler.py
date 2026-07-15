"""
Point d'entrée Lambda. Reçoit une requête utilisateur, construit le
contexte depuis CockroachDB, appelle Claude (Bedrock), gère les éventuels
appels d'outils MCP décidés par le modèle, puis persiste la conversation
et son embedding.
"""
import json

import bedrock_client
import memory

SYSTEM_PROMPT = """Tu es un assistant IA avec une mémoire persistante stockée dans CockroachDB.
Utilise le contexte fourni (historique récent + souvenirs pertinents) pour répondre de façon
personnalisée et cohérente avec les échanges précédents.
Si tu as besoin d'inspecter le schéma de la base ou d'exécuter une requête de vérification,
utilise les outils MCP mis à ta disposition."""


def build_prompt_context(user_id: str, user_message: str) -> str:
    """Assemble l'historique récent + les souvenirs sémantiquement proches."""
    recent = memory.get_recent_conversations(user_id, limit=10)
    history_text = "\n".join(f"[{r['role']}] {r['content']}" for r in recent)

    query_embedding = bedrock_client.generate_embedding(user_message)
    similar = memory.search_similar_memories(user_id, query_embedding, top_k=5)
    memories_text = "\n".join(f"- {m['content']} (distance={m['distance']:.3f})" for m in similar)

    return (
        f"## Historique récent\n{history_text or '(aucun)'}\n\n"
        f"## Souvenirs pertinents\n{memories_text or '(aucun)'}\n\n"
        f"## Message actuel\n{user_message}"
    )


def run_agent_turn(user_id: str, user_message: str) -> str:
    context_block = build_prompt_context(user_id, user_message)
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

    return final_text


def lambda_handler(event, context):
    http_method = event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod")
    if http_method == "OPTIONS":
        # Préflight CORS : API Gateway le transmet à la Lambda (route $default),
        # il faut répondre 200 sans passer par la validation métier ci-dessous.
        return {
            "statusCode": 200,
            "headers": {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "OPTIONS,POST",
                "Access-Control-Allow-Headers": "content-type",
            },
            "body": "",
        }

    try:
        body = json.loads(event.get("body", "{}")) if "body" in event else event
        user_name = body.get("user_name", "anonymous")
        user_message = body.get("message")
        if not user_message:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "Le champ 'message' est requis."}),
            }

        user_id = memory.get_or_create_user(user_name)
        reply = run_agent_turn(user_id, user_message)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"reply": reply, "user_id": user_id}),
        }
    except Exception as exc:  # noqa: BLE001 — toujours répondre proprement au client
        print(f"[ERROR] {exc}")  # capté par CloudWatch Logs
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "Une erreur interne est survenue. Réessaie dans un instant."}),
        }


if __name__ == "__main__":
    # Test local, sans Lambda : python -m agent.handler
    test_event = {"user_name": "stanley", "message": "Salut, tu te souviens de moi ?"}
    print(lambda_handler(test_event, None))
