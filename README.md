# AI Employee — Agentic Memory powered by CockroachDB & AWS

> Un agent IA de production doté d'une mémoire persistante, distribuée et résiliente — construit pour le **CockroachDB × AWS Hackathon: Build with Agentic Memory**.

## 🎯 Le problème

Un chatbot classique oublie tout entre deux sessions. Un agent de production, lui, doit se souvenir de ses conversations, de ses tâches, de son contexte utilisateur et de son état — et cette mémoire doit survivre aux redémarrages, aux pannes et aux changements de région.

Ce projet utilise **CockroachDB** comme cerveau à long terme d'un agent IA déployé sur **AWS**, avec un raisonnement propulsé par **Amazon Bedrock**.

## 🏗️ Architecture

```
Utilisateur → Frontend → API Gateway → AWS Lambda (orchestrateur agent)
                                              │
                                              ├──► Amazon Bedrock (Claude — raisonnement + tool use)
                                              ├──► Amazon Bedrock (Titan Embeddings G1 — 1536 dim)
                                              └──► CockroachDB Cloud
                                                     ├─ Mémoire structurée (conversations, tâches, contexte) — SQL direct
                                                     ├─ Mémoire vectorielle (VECTOR + recherche par similarité L2)
                                                     └─ MCP Server managé — appelé par Claude lui-même (tool use)
```

Deux canaux d'accès à CockroachDB, volontairement distincts :
- **SQL direct (psycopg2)** : écriture/lecture rapide de la mémoire, recherche vectorielle
- **MCP Server managé** : exposé comme un **outil que Claude appelle lui-même** pendant son raisonnement (introspection du schéma, requêtes de vérification en lecture seule)

Un diagramme visuel détaillé sera ajouté dans `/docs`.

## 🧰 Stack technique

| Composant | Technologie |
|---|---|
| Base de données / mémoire agentique | **CockroachDB Cloud** (Serverless, free tier) |
| Recherche vectorielle / RAG | CockroachDB `VECTOR(1536)` + index distribué (`vector_l2_ops`) |
| Accès agent → base de données (autonome) | **CockroachDB Cloud Managed MCP Server** (tool use côté Claude) |
| Automatisation infra | **ccloud CLI** |
| Orchestration de l'agent | **AWS Lambda** |
| Modèle de langage | **Amazon Bedrock** — Claude Sonnet 4.5 (via inference profile) |
| Embeddings | **Amazon Titan Embeddings G1 - Text** (`amazon.titan-embed-text-v1`, 1536 dim) |
| Stockage de documents | **Amazon S3** *(prévu Phase 3)* |
| Frontend | À définir (React/Next.js — voir `/frontend`) |

Outils CockroachDB utilisés (minimum 2 requis par le hackathon) :
- ✅ **CockroachDB Cloud Managed MCP Server** — appelé en autonomie par Claude (tool use) pour l'introspection du schéma et les requêtes de vérification
- ✅ **Distributed Vector Indexing** — recherche par similarité sur `memory_embeddings`
- ✅ **ccloud CLI** — provisioning et gestion du cluster
- ⬜ Agent Skills Repo *(à évaluer selon le temps disponible)*

Service AWS utilisé (minimum 1 requis) :
- ✅ **Amazon Bedrock** — Claude (raisonnement + tool use) et Titan (embeddings)
- ✅ **AWS Lambda** — orchestration de l'agent

## 📁 Structure du repo

```
.
├── agent/       # Logique de l'agent : orchestration, prompts, appels Bedrock, lecture/écriture mémoire
│   ├── handler.py          # Point d'entrée Lambda + boucle agent (tool use MCP)
│   ├── bedrock_client.py   # Wrapper Bedrock : Claude + Titan Embeddings
│   ├── memory.py           # Accès CockroachDB (SQL direct + appel MCP Server)
│   ├── requirements.txt
│   └── .env.example
├── infra/       # Schéma SQL CockroachDB, scripts ccloud, configuration MCP
│   └── schema.sql
├── frontend/    # Interface utilisateur (chat)
├── docs/        # Diagrammes d'architecture, notes de conception
└── README.md
```

## 🚀 Statut actuel

### ✅ Phase 0 — Setup
- Cluster CockroachDB Cloud provisionné (free tier)
- Connexion MCP Server testée et validée (`tools/list`, `list_tables`, `get_table_schema`)

### ✅ Phase 1 — Schéma de mémoire
- Tables créées : `user_context`, `conversations`, `tasks`, `memory_embeddings`
- Colonne `VECTOR(1536)` + index vectoriel (`vector_l2_ops`) sur `memory_embeddings`
- Schéma versionné dans [`infra/schema.sql`](./infra/schema.sql), reconstitué via le MCP Server (`get_table_schema`)

### ✅ Phase 2 — Cœur de l'agent
- Lambda handler fonctionnel, testé en local (`python -m dotenv run python handler.py`)
- Intégration Bedrock (Claude Sonnet 4.5 + Titan Embeddings G1)
- Logique complète : lecture du contexte (historique + mémoire vectorielle) → prompt → appel LLM → écriture en mémoire
- **Boucle "tool use" MCP validée** : Claude appelle lui-même le MCP Server pour consulter la base (comportement observé en test : l'agent a restitué une donnée non fournie dans le prompt en interrogeant `user_context` de façon autonome)
- **Test de persistance à 2 tours validé** : une information donnée au tour 1 (plat préféré) est correctement restituée au tour 2, confirmant que la mémoire conversationnelle fonctionne de bout en bout

### 🔜 Phase 3 — RAG + mémoire vectorielle long terme
- Ingestion de documents (S3 → embeddings → CockroachDB)
- Test de la recherche vectorielle sur mémoire longue durée (au-delà de la fenêtre d'historique récent)

## 🔧 Installation et lancement

### Pré-requis
- Un compte [CockroachDB Cloud](https://cockroachlabs.cloud) (gratuit, sans CB)
- Un compte AWS avec accès à Bedrock activé pour Claude et Titan Embeddings (Console AWS → Bedrock → Model access)
- `ccloud` CLI installé ([instructions](https://www.cockroachlabs.com/docs/cockroachcloud/ccloud-get-started))
- Python 3.11+

### 1. Cloner le repo
```bash
git clone https://github.com/<ton-user>/<ton-repo>.git
cd <ton-repo>
```

### 2. Configurer la base de données
```bash
ccloud auth login

# Récupérer la chaîne de connexion depuis la Console Cloud (bouton "Connect")
export DATABASE_URL="postgresql://<user>:<password>@<host>:26257/defaultdb?sslmode=verify-full"

# Rejouer le schéma
cockroach sql --url "$DATABASE_URL" < infra/schema.sql
```

### 3. Configurer l'environnement de l'agent
```bash
cd agent
pip install -r requirements.txt
cp .env.example .env
# Éditer .env avec tes vraies valeurs (DATABASE_URL, clés CockroachDB, région AWS)
```

⚠️ **Important — modèle Bedrock** : les modèles Claude récents (Sonnet 4.5 et plus) ne sont plus invocables via leur ID de modèle brut sur Bedrock (erreur `on-demand throughput isn't supported`). Il faut utiliser un **inference profile**, préfixé par région :
```
BEDROCK_CLAUDE_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0
```
(remplacer `us.` par `eu.`, `apac.` ou `global.` selon la région Bedrock utilisée). Pour lister les profils disponibles sur ton compte :
```bash
aws bedrock list-inference-profiles --region us-east-1 \
  --query "inferenceProfileSummaries[?contains(inferenceProfileName, 'Sonnet')].[inferenceProfileId,inferenceProfileName]" \
  --output table
```

### 4. Tester l'agent en local
```bash
python -m dotenv run python handler.py
```
Le fichier `handler.py` contient un test à deux tours de conversation qui valide la persistance de la mémoire (voir bloc `if __name__ == "__main__":`).

### 5. Déploiement Lambda
*(à venir — Phase 2 finalisation / packaging)*

## 🎥 Démo

- Lien démo fonctionnelle : *à venir*
- Vidéo de présentation (< 3 min) : *à venir*

## 📜 Licence

Ce projet est distribué sous licence **GNU General Public License v3.0** — voir [`LICENSE`](./LICENSE).

## 🙌 Hackathon

Projet développé dans le cadre du [CockroachDB × AWS Hackathon — Build with Agentic Memory](https://cockroachdb-ai.devpost.com/).