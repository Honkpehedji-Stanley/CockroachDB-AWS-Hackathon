"""
Client Bedrock : wrapper autour de boto3 pour appeler Claude (raisonnement)
et Titan Embeddings G1 (génération de vecteurs 1536-dim).
"""
import json
import os
import boto3

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
CLAUDE_MODEL_ID = os.environ.get(
    "BEDROCK_CLAUDE_MODEL_ID",
    "anthropic.claude-3-5-sonnet-20241022-v2:0",  # ajuste selon le modèle activé dans ton compte
)
TITAN_EMBED_MODEL_ID = "amazon.titan-embed-text-v1"

_bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)


def generate_embedding(text: str) -> list[float]:
    """Génère un embedding 1536-dim avec Titan G1, pour stockage/recherche CockroachDB."""
    body = json.dumps({"inputText": text})
    response = _bedrock.invoke_model(
        modelId=TITAN_EMBED_MODEL_ID,
        body=body,
        accept="application/json",
        contentType="application/json",
    )
    result = json.loads(response["body"].read())
    return result["embedding"]


def call_claude(messages: list[dict], tools: list[dict] | None = None, system: str = "") -> dict:
    """
    Appelle Claude via Bedrock (API Messages / Converse-compatible).
    `messages` suit le format Anthropic standard : [{"role": "user", "content": "..."}]
    `tools` (optionnel) permet au modèle d'appeler des outils MCP CockroachDB.
    """
    body: dict = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "messages": messages,
    }
    if system:
        body["system"] = system
    if tools:
        body["tools"] = tools

    response = _bedrock.invoke_model(
        modelId=CLAUDE_MODEL_ID,
        body=json.dumps(body),
        accept="application/json",
        contentType="application/json",
    )
    return json.loads(response["body"].read())
