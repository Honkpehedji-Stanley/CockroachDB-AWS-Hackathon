# Architecture — AI Employee

Deux diagrammes : la vue d'ensemble des composants, puis le déroulé détaillé
d'un tour de conversation (c'est là que la mémoire agentique CockroachDB
intervient concrètement).

## Vue d'ensemble

```mermaid
flowchart TD
    U["Utilisateur (navigateur)"] -->|HTTPS| FE["Frontend statique — index.html"]
    FE -->|"fetch JSON (POST)"| AG["API Gateway — HTTP API"]
    AG -->|intégration proxy| L["AWS Lambda — handler.py (image Docker)"]

    subgraph AWS["AWS"]
        AG
        L
        S3[("Amazon S3 — documents bruts")]
        BR1["Amazon Bedrock — Claude Sonnet 4.5"]
        BR2["Amazon Bedrock — Titan Embeddings G1"]
    end

    subgraph CRDB["CockroachDB Cloud"]
        SQL[("SQL direct (psycopg2)\nuser_context · conversations · tasks\nmemory_embeddings VECTOR(1536)")]
        MCP["MCP Server managé"]
    end

    L -->|"contexte : historique + recherche vectorielle"| SQL
    L -->|"embedding de la requête"| BR2
    L -->|"raisonnement + tool use"| BR1
    L -->|"exécute l'outil demandé par Claude\n(list_tables / get_table_schema / select_query)"| MCP
    MCP -->|"introspection lecture seule"| SQL
    L -->|"persistance : conversation + embedding"| SQL
    L -->|"pièce jointe : upload brut"| S3
    L -->|"embedding des chunks du document"| BR2
```

Deux canaux d'accès à CockroachDB, volontairement distincts :
- **SQL direct (psycopg2)** — écriture/lecture rapide de la mémoire structurée, recherche vectorielle (`vector_l2_ops`).
- **MCP Server managé** — exposé comme un outil que Claude peut décider d'appeler lui-même pendant son raisonnement (introspection du schéma, requêtes de vérification en lecture seule). C'est le modèle qui décide *si* et *quand* s'en servir ; la Lambda exécute l'appel HTTP en son nom et lui renvoie le résultat.

## Déroulé d'un tour de conversation

Ce que fait concrètement l'agent entre la question de l'utilisateur et sa
réponse — la mémoire n'est pas un détail d'implémentation, c'est ce qui
détermine le contenu de la réponse.

```mermaid
sequenceDiagram
    participant User as Utilisateur
    participant Lambda as Lambda (handler.py)
    participant CRDB as CockroachDB
    participant Titan as Bedrock Titan
    participant Claude as Bedrock Claude
    participant MCP as MCP Server

    User->>Lambda: message (+ pièce jointe optionnelle)
    opt Pièce jointe présente
        Lambda->>CRDB: chunking + embeddings du document
        Note over Lambda,CRDB: texte extrait injecté directement<br/>dans le prompt de cette réponse
    end
    Lambda->>CRDB: SELECT historique récent (conversations)
    Lambda->>Titan: embedding du message
    Titan-->>Lambda: vecteur 1536-dim
    Lambda->>CRDB: recherche vectorielle (distance L2, top-5)
    CRDB-->>Lambda: souvenirs les plus proches
    Lambda->>Claude: contexte complet + outils MCP disponibles
    opt Claude demande un outil
        Claude-->>Lambda: tool_use (ex. select_query)
        Lambda->>MCP: exécute la requête (lecture seule)
        MCP-->>Lambda: résultat
        Lambda->>Claude: tool_result
    end
    Claude-->>Lambda: réponse finale
    Lambda->>CRDB: INSERT conversation (user + assistant)
    Lambda->>Titan: embedding de l'échange
    Lambda->>CRDB: INSERT memory_embeddings
    Lambda-->>User: réponse + souvenirs mobilisés
```

Les "souvenirs mobilisés" (contenu, source, distance) sont renvoyés au
frontend et affichés dans le panneau "Mémoire mobilisée" — la recherche
vectorielle est visible pour l'utilisateur, pas juste un détail interne.
