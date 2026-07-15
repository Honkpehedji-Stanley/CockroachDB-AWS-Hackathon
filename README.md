# AI Employee — Agentic Memory powered by CockroachDB & AWS

> Un agent IA de production doté d'une mémoire persistante, distribuée et résiliente — construit pour le **CockroachDB × AWS Hackathon: Build with Agentic Memory**.

## 🎯 Le problème

Un chatbot classique oublie tout entre deux sessions. Un agent de production, lui, doit se souvenir de ses conversations, de ses tâches, de son contexte utilisateur et de son état — et cette mémoire doit survivre aux redémarrages, aux pannes et aux changements de région.

Ce projet utilise **CockroachDB** comme cerveau à long terme d'un agent IA déployé sur **AWS**, avec un raisonnement propulsé par **Amazon Bedrock**.

## 🏗️ Architecture

```
Utilisateur → Frontend (chat) → API Gateway (HTTP API) → AWS Lambda (orchestrateur agent, image Docker)
                                                                  │
                                                                  ├──► Amazon Bedrock (Claude — raisonnement + tool use)
                                                                  ├──► Amazon Bedrock (Titan Embeddings G1 — 1536 dim)
                                                                  ├──► Amazon S3 (stockage brut des documents)
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
| Orchestration de l'agent | **AWS Lambda** (image Docker) exposée via **API Gateway** (HTTP API) |
| Modèle de langage | **Amazon Bedrock** — Claude Sonnet 4.5 (via inference profile) |
| Embeddings | **Amazon Titan Embeddings G1 - Text** (`amazon.titan-embed-text-v1`, 1536 dim) |
| Stockage de documents | **Amazon S3** |
| Frontend | HTML/JS statique (`frontend/index.html`), sans dépendance de build |

Outils CockroachDB utilisés (minimum 2 requis par le hackathon) :
- ✅ **CockroachDB Cloud Managed MCP Server** — appelé en autonomie par Claude (tool use) pour l'introspection du schéma et les requêtes de vérification
- ✅ **Distributed Vector Indexing** — recherche par similarité sur `memory_embeddings`, validée avec discrimination multi-documents
- ✅ **ccloud CLI** — provisioning et gestion du cluster
- ⬜ Agent Skills Repo *(à évaluer selon le temps disponible)*

Service AWS utilisé (minimum 1 requis) :
- ✅ **Amazon Bedrock** — Claude (raisonnement + tool use) et Titan (embeddings)
- ✅ **AWS Lambda** — orchestration de l'agent, déployée en production (image Docker)
- ✅ **Amazon API Gateway** (HTTP API) — endpoint public devant la Lambda
- ✅ **Amazon S3** — stockage brut des documents avant ingestion

## 📁 Structure du repo

```
.
├── agent/       # Logique de l'agent
│   ├── handler.py          # Point d'entrée Lambda + boucle agent (tool use MCP)
│   ├── bedrock_client.py   # Wrapper Bedrock : Claude + Titan Embeddings
│   ├── memory.py           # Accès CockroachDB (SQL direct + appel MCP Server)
│   ├── ingest.py            # Ingestion de documents PDF/texte (S3 → chunks → embeddings)
│   ├── test_phase3.py       # Test de recherche vectorielle sur documents
│   ├── test_discrimination.py  # Test multi-documents (classement par pertinence)
│   ├── sample_docs/         # Documents d'exemple pour les tests
│   ├── Dockerfile           # Image Lambda
│   ├── deploy.sh            # Script de déploiement (ECR + Lambda + Function URL)
│   ├── requirements.txt
│   └── .env.example
├── infra/       # Schéma SQL CockroachDB
│   └── schema.sql
├── frontend/    # Interface de chat
│   └── index.html
├── docs/        # Diagrammes d'architecture, notes de conception
└── README.md
```

## 🚀 Statut actuel

### ✅ Phase 0 — Setup
Cluster CockroachDB Cloud provisionné, MCP Server testé et validé.

### ✅ Phase 1 — Schéma de mémoire
Tables `user_context`, `conversations`, `tasks`, `memory_embeddings` créées, avec index vectoriel. Schéma versionné dans [`infra/schema.sql`](./infra/schema.sql).

### ✅ Phase 2 — Cœur de l'agent
Lambda handler fonctionnel (testé en local puis déployé), intégration Bedrock (Claude + Titan), boucle "tool use" MCP validée, persistance de la mémoire conversationnelle validée sur 2 tours.

### ✅ Phase 3 — RAG + mémoire vectorielle
Ingestion de documents PDF et texte validée. Recherche vectorielle testée avec **discrimination multi-documents** (plusieurs documents sans rapport en mémoire, chaque question retrouve le bon document en tête du classement). Pipeline S3 → extraction → chunking → embedding → CockroachDB validé de bout en bout.

### ✅ Phase 4 — Déploiement production
- Image Docker buildée et poussée sur **Amazon ECR**
- Fonction **AWS Lambda** créée (image container, 512 MB, timeout 30s), rôle IAM dédié (`AWSLambdaBasicExecutionRole`, `AmazonBedrockFullAccess`, `AmazonS3FullAccess`)
- Endpoint public : **API Gateway HTTP API** (`https://cuii8r8ija.execute-api.us-east-1.amazonaws.com`), intégration proxy avec la Lambda, CORS activé
- Frontend de chat fonctionnel (`frontend/index.html`), branché sur l'URL API Gateway
- Test bout en bout réussi : l'agent répond, se souvient d'une conversation antérieure, tool-use MCP observé en autonomie

⚠️ **Pourquoi API Gateway et pas une Lambda Function URL** : la Function URL retournait systématiquement `403 Forbidden` malgré une resource policy correcte (`lambda:InvokeFunctionUrl`, `AuthType=NONE`) — cause probablement liée à une restriction spécifique au compte AWS utilisé, non résolue avec certitude. API Gateway HTTP API fonctionne sans ce problème et est l'endpoint à utiliser.

⚠️ **Note pour qui relance `deploy.sh`** : le script provisionne encore une Function URL (legacy, étape 6) — l'API Gateway actuelle a été créée à la main via AWS CLI et n'est pas encore automatisée dans le script. Deux points d'attention si tu la recrées :
- `SourceArn` de la permission Lambda : `arn:aws:execute-api:{region}:{account}:{api-id}/*` (un seul wildcard — le format à 3 wildcards habituel en REST API v1 échoue silencieusement en HTTP API v2)
- Le certificat CA CockroachDB (`agent/cc-ca.crt`) doit être copié dans l'image Docker et référencé via `sslrootcert=/var/task/cc-ca.crt` dans `DATABASE_URL` — l'image `public.ecr.aws/lambda/python:3.12` n'a pas de magasin de certificats système complet, donc `sslrootcert=system` échoue

## 🔧 Installation et lancement

### Pré-requis
- Un compte [CockroachDB Cloud](https://cockroachlabs.cloud) (gratuit, sans CB)
- Un compte AWS avec accès à Bedrock activé pour Claude et Titan Embeddings (Console AWS → Bedrock → Model access)
- `ccloud` CLI installé ([instructions](https://www.cockroachlabs.com/docs/cockroachcloud/ccloud-get-started))
- AWS CLI v2 récent, Docker, Python 3.11+

### 1. Cloner le repo
```bash
git clone https://github.com/<ton-user>/<ton-repo>.git
cd <ton-repo>
```

### 2. Configurer la base de données
```bash
ccloud auth login
export DATABASE_URL="postgresql://<user>:<password>@<host>:26257/defaultdb?sslmode=verify-full"
cockroach sql --url "$DATABASE_URL" < infra/schema.sql
```

### 3. Configurer l'environnement de l'agent
```bash
cd agent
pip install -r requirements.txt
cp .env.example .env
# Éditer .env avec tes vraies valeurs
```

⚠️ **Modèle Bedrock** : les modèles Claude récents (Sonnet 4.5+) nécessitent un **inference profile**, préfixé par région :
```
BEDROCK_CLAUDE_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0
```

### 4. Tester l'agent en local
```bash
python -m dotenv run python handler.py
```

### 5. Tester l'ingestion de documents
```bash
python -m dotenv run python test_discrimination.py [chemin/vers/un.pdf optionnel]
```

### 6. Déployer sur AWS Lambda
```bash
chmod +x deploy.sh
./deploy.sh
```
Le script build et pousse l'image, puis crée/met à jour la fonction Lambda (il provisionne aussi une Function URL, non utilisée en production — voir note Phase 4 ci-dessus). Il faut ensuite exposer la Lambda via une **API Gateway HTTP API** (intégration proxy, route `$default`, CORS activé) — c'est cette URL-là qu'il faut utiliser en pratique. Teste-la avec :
```bash
curl -X POST <URL_API_GATEWAY> \
  -H 'Content-Type: application/json' \
  -d '{"user_name":"stanley","message":"Salut !"}'
```

### 7. Lancer le frontend
Ouvre simplement `frontend/index.html` dans un navigateur, colle l'URL API Gateway dans le champ prévu, et discute avec l'agent.

## 🎥 Démo

- Lien démo fonctionnelle : *à venir*
- Vidéo de présentation (< 3 min) : *à venir*

## 📜 Licence

Ce projet est distribué sous licence **GNU General Public License v3.0** — voir [`LICENSE`](./LICENSE).

## 🙌 Hackathon

Projet développé dans le cadre du [CockroachDB × AWS Hackathon — Build with Agentic Memory](https://cockroachdb-ai.devpost.com/).