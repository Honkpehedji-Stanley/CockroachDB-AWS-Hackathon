"""
Test de discrimination Phase 3 : ingère plusieurs documents SANS RAPPORT
entre eux, puis vérifie que la recherche vectorielle retrouve bien le bon
document en premier pour chaque question — preuve que le classement par
similarité fonctionne réellement, pas juste "un seul doc donc ça matche".

Usage : python test_discrimination.py [chemin/vers/un.pdf]
"""
import sys

import bedrock_client
import memory
from ingest import ingest_local_document

USER_NAME = "stanley"


def ask(user_id: str, question: str, expected_keyword: str):
    query_embedding = bedrock_client.generate_embedding(question)
    results = memory.search_similar_memories(
        user_id, query_embedding, top_k=3, source_type="document"
    )
    print(f"\nQuestion : {question}")
    for r in results:
        print(f"  - (distance={r['distance']:.3f}) {r['content'][:100]}...")

    top = results[0] if results else None
    ok = top and expected_keyword.lower() in top["content"].lower()
    print(f"  {'✅ Bon document en tête' if ok else '❌ Mauvais document en tête'}")
    return ok


def main():
    user_id = memory.get_or_create_user(USER_NAME)

    # 1. Ingérer les deux documents sans rapport
    print(ingest_local_document("sample_docs/note_exemple.txt", user_id))
    print(ingest_local_document("sample_docs/note_cuisine.txt", user_id))

    # 2. Ingérer un PDF optionnel, passé en argument
    if len(sys.argv) > 1:
        print(ingest_local_document(sys.argv[1], user_id))

    # 3. Poser une question par document, vérifier le bon classement
    results = []
    results.append(ask(user_id, "Quelle est la politique de remboursement ?", "remboursement"))
    results.append(ask(user_id, "Comment prépare-t-on le poulet DG ?", "poulet"))

    if all(results):
        print("\n✅ La recherche vectorielle discrimine correctement entre documents.")
    else:
        print("\n⚠️ Le classement n'est pas encore fiable — vérifier le chunking ou le modèle d'embedding.")


if __name__ == "__main__":
    main()
