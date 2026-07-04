# AI Employee — Agentic Memory powered by CockroachDB & AWS
 
> Un agent IA de production doté d'une mémoire persistante, distribuée et résiliente — construit pour le **CockroachDB × AWS Hackathon: Build with Agentic Memory**.
 
## Le problème
 
Un chatbot classique oublie tout entre deux sessions. Un agent de production, lui, doit se souvenir de ses conversations, de ses tâches, de son contexte utilisateur et de son état — et cette mémoire doit survivre aux redémarrages, aux pannes et aux changements de région.
 
Ce projet utilise **CockroachDB** comme cerveau à long terme d'un agent IA déployé sur **AWS**, avec un raisonnement propulsé par **Amazon Bedrock**.
 
## Architecture
 
```
Utilisateur → Frontend → API Gateway → AWS Lambda (orchestrateur agent)
                                              │
                                              ├──► Amazon Bedrock (Claude — raisonnement)
                                              ├──► Amazon Bedrock (Titan Embeddings)
                                              └──► CockroachDB Cloud
                                                     ├─ Mémoire structurée (conversations, tâches, contexte)
                                                     ├─ Mémoire vectorielle (VECTOR + recherche sémantique)
                                                     └─ MCP Server managé (accès agent en langage naturel)
```
 
Un diagramme détaillé sera ajouté dans `/docs`.
 
## Stack technique
 
| Composant | Technologie |
|---|---|
| Base de données / mémoire agentique | **CockroachDB Cloud** (Serverless, free tier) |
| Recherche vectorielle / RAG | CockroachDB `VECTOR` type + index distribué |
| Accès agent → base de données | **CockroachDB Cloud Managed MCP Server** |
| Automatisation infra | **ccloud CLI** |
| Orchestration de l'agent | **AWS Lambda** |
| Modèle de langage | **Amazon Bedrock** (Claude) |
| Embeddings | **Amazon Titan Embeddings** (via Bedrock) |
| Stockage de documents | **Amazon S3** |
| Frontend | À définir (React/Next.js — voir `/frontend`) |
 
Outils CockroachDB utilisés (minimum 2 requis par le hackathon) :
- **CockroachDB Cloud Managed MCP Server**
- **Distributed Vector Indexing**
- ccloud CLI *(utilisé pour l'infra, pas encore documenté ici)*
- Agent Skills Repo *(à évaluer)*
Service AWS utilisé (minimum 1 requis) :
- **Amazon Bedrock**
- **AWS Lambda**
## Structure du repo
 
```
.
├── agent/       # Logique de l'agent : orchestration, prompts, appels Bedrock, lecture/écriture mémoire
├── infra/       # Schéma SQL CockroachDB, scripts ccloud, configuration MCP
├── frontend/    # Interface utilisateur (chat)
├── docs/        # Diagrammes d'architecture, notes de conception
└── README.md
```
 
## Statut actuel — Phase 1 : Schéma de mémoire
 
Cluster CockroachDB Cloud provisionné (free tier)
Tables de mémoire créées :
  - `conversations` — historique des échanges
  - `tasks` / `state` — état et tâches de l'agent
  - `user_context` — profil et préférences utilisateur
Colonne `VECTOR` + index vectoriel créés pour les embeddings
Connexion MCP Server testée avec Claude Code / Cursor
Prochaine étape (Phase 2) : cœur de l'agent (Lambda + Bedrock)
Le schéma SQL complet est versionné dans [`infra/schema.sql`](./infra/schema.sql).
 
## Installation et lancement
 
### Pré-requis
- Un compte [CockroachDB Cloud](https://cockroachlabs.cloud) (gratuit, sans CB)
- Un compte AWS avec accès à Bedrock et Lambda activés
- `ccloud` CLI installé ([instructions](https://www.cockroachlabs.com/docs/cockroachcloud/ccloud-get-started))
- Node.js / Python (selon la stack finale de l'agent — à préciser en Phase 2)
### 1. Cloner le repo
```bash
git clone https://github.com/<ton-user>/<ton-repo>.git
cd <ton-repo>
```
 
### 2. Configurer la base de données
```bash
# Se connecter à CockroachDB Cloud
ccloud auth login
 
# Récupérer la chaîne de connexion depuis la Console Cloud (bouton "Connect")
export DATABASE_URL="postgresql://<user>:<password>@<host>:26257/defaultdb?sslmode=verify-full"
 
# Rejouer le schéma
cockroach sql --url "$DATABASE_URL" < infra/schema.sql
```
 
### 3. Configurer le MCP Server (optionnel, pour le développement assisté par IA)
Ajouter dans `.cursor/mcp.json` ou via `claude mcp add` :
```json
"mcpServers": {
  "cockroachdb-cloud": {
    "url": "https://cockroachlabs.cloud/mcp",
    "headers": {
      "mcp-cluster-id": "<ton-cluster-id>",
      "Authorization": "Bearer <ta-service-account-api-key>"
    }
  }
}
```
 
### 4. Variables d'environnement
Créer un fichier `.env` à la racine (voir `.env.example`) :
```
DATABASE_URL=
AWS_REGION=
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
BEDROCK_MODEL_ID=
```
 
### 5. Lancer l'agent
*(instructions détaillées à venir en Phase 2 — orchestrateur Lambda)*
 
## Démo
 
- Lien démo fonctionnelle : *à venir*
- Vidéo de présentation (< 3 min) : *à venir*
## Licence
 
Ce projet est distribué sous licence **GNU General Public License v3.0** — voir [`LICENSE`](./LICENSE).
 
## Hackathon
 
Projet développé dans le cadre du [CockroachDB × AWS Hackathon — Build with Agentic Memory](https://cockroachdb-ai.devpost.com/).