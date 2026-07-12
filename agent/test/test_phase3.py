"""
Test Phase 3 : vérifie que la recherche vectorielle retrouve le contenu
d'un document ingéré, indépendamment de l'historique de conversation.

Usage : python test_phase3.py
"""
import bedrock_client
import memory
from ingest import ingest_local_document

USER_NAME = "stanley"


def main():
    user_id = memory.get_or_create_user(USER_NAME)

    # 1. Ingérer un document de test (crée-le si besoin, voir bloc plus bas)
    result = ingest_local_document("sample_docs/note_exemple.txt", user_id)
    print(f"Document ingéré : {result}\n")

    # 2. Poser une question dont la réponse est dans le document,
    #    mais absente de l'historique de conversation.
    question = "Quelle est la politique de remboursement mentionnée dans mes notes ?"
    query_embedding = bedrock_client.generate_embedding(question)

    # 3. Recherche filtrée sur les documents uniquement
    results = memory.search_similar_memories(
        user_id, query_embedding, top_k=3, source_type="document"
    )

    print(f"Résultats pour : '{question}'\n")
    for r in results:
        print(f"- (distance={r['distance']:.3f}) {r['content'][:150]}...")

    if results:
        print("\n✅ La recherche vectorielle sur documents fonctionne.")
    else:
        print("\n❌ Aucun résultat — vérifier l'ingestion et le contenu du document.")


if __name__ == "__main__":
    main()
