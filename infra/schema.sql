-- ============================================================
-- AI Employee — Schéma de mémoire agentique
-- Base de données : CockroachDB Cloud (Serverless, free tier)
-- Généré depuis les CREATE TABLE réels du cluster (via CockroachDB
-- Managed MCP Server, outil `get_table_schema`)
-- ============================================================
-- Ordre de création : user_context d'abord (table racine),
-- puis les tables qui la référencent en clé étrangère.
-- ============================================================

-- ------------------------------------------------------------
-- Table : user_context
-- Rôle : profil et préférences persistantes de chaque utilisateur
-- ------------------------------------------------------------
CREATE TABLE public.user_context (
    user_id       UUID NOT NULL DEFAULT gen_random_uuid(),
    name          STRING NULL,
    password_hash STRING NULL,
    preferences   JSONB NULL,
    created_at    TIMESTAMPTZ NULL DEFAULT now():::TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ NULL DEFAULT now():::TIMESTAMPTZ,
    CONSTRAINT user_context_pkey PRIMARY KEY (user_id ASC)
);
CREATE UNIQUE INDEX user_context_name_unique ON public.user_context (name);

-- ------------------------------------------------------------
-- Table : threads
-- Rôle : une conversation distincte (façon ChatGPT/Claude) — regroupe
-- les lignes de `conversations` qui partagent un même fil. Le titre est
-- dérivé du premier message utilisateur (voir memory.touch_thread).
-- ------------------------------------------------------------
CREATE TABLE public.threads (
    thread_id  UUID NOT NULL DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL,
    title      STRING NULL,
    created_at TIMESTAMPTZ NULL DEFAULT now():::TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NULL DEFAULT now():::TIMESTAMPTZ,
    CONSTRAINT threads_pkey PRIMARY KEY (thread_id ASC),
    CONSTRAINT threads_user_id_fkey FOREIGN KEY (user_id)
        REFERENCES public.user_context (user_id)
);

-- ------------------------------------------------------------
-- Table : conversations
-- Rôle : historique brut des échanges (rôle + contenu), par utilisateur,
-- rattaché à un fil (thread_id) — le contexte envoyé au modèle
-- (get_recent_conversations) est scopé au fil courant, alors que la
-- recherche vectorielle (memory_embeddings) reste globale à l'utilisateur :
-- l'agent peut donc rappeler un fait d'un tout autre fil via la mémoire
-- sémantique, même si le contexte conversationnel, lui, est cloisonné.
-- ------------------------------------------------------------
CREATE TABLE public.conversations (
    conversation_id UUID NOT NULL DEFAULT gen_random_uuid(),
    user_id         UUID NULL,
    thread_id       UUID NULL,
    "role"          STRING NOT NULL,
    content         STRING NOT NULL,
    created_at      TIMESTAMPTZ NULL DEFAULT now():::TIMESTAMPTZ,
    CONSTRAINT conversations_pkey PRIMARY KEY (conversation_id ASC),
    CONSTRAINT conversations_user_id_fkey FOREIGN KEY (user_id)
        REFERENCES public.user_context (user_id),
    CONSTRAINT conversations_thread_id_fkey FOREIGN KEY (thread_id)
        REFERENCES public.threads (thread_id)
);

-- ------------------------------------------------------------
-- Table : tasks
-- Rôle : état et suivi des tâches de l'agent, par utilisateur
-- ------------------------------------------------------------
CREATE TABLE public.tasks (
    task_id    UUID NOT NULL DEFAULT gen_random_uuid(),
    user_id    UUID NULL,
    title      STRING NOT NULL,
    status     STRING NULL DEFAULT 'pending':::STRING,
    metadata   JSONB NULL,
    created_at TIMESTAMPTZ NULL DEFAULT now():::TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NULL DEFAULT now():::TIMESTAMPTZ,
    CONSTRAINT tasks_pkey PRIMARY KEY (task_id ASC),
    CONSTRAINT tasks_user_id_fkey FOREIGN KEY (user_id)
        REFERENCES public.user_context (user_id)
);

-- ------------------------------------------------------------
-- Table : memory_embeddings
-- Rôle : mémoire vectorielle (RAG / recherche sémantique)
-- Stocke le texte source ET son embedding dans la même ligne,
-- transactionnellement cohérents (pas de store vectoriel séparé).
--
-- ⚠️ VECTOR(1536) correspond à des embeddings de dimension 1536
-- (ex. OpenAI ada-002 ou Titan Embeddings G1 - Text).
-- Si vous utilisez Titan Text Embeddings V2 en 1024 dims,
-- ajustez cette colonne en conséquence avant la Phase 2 :
--   ALTER TABLE memory_embeddings ALTER COLUMN embedding TYPE VECTOR(1024);
-- ------------------------------------------------------------
CREATE TABLE public.memory_embeddings (
    embedding_id UUID NOT NULL DEFAULT gen_random_uuid(),
    user_id      UUID NULL,
    source_type  STRING NULL,
    source_id    UUID NULL,
    content      STRING NOT NULL,
    embedding    VECTOR(1536) NULL,
    created_at   TIMESTAMPTZ NULL DEFAULT now():::TIMESTAMPTZ,
    CONSTRAINT memory_embeddings_pkey PRIMARY KEY (embedding_id ASC),
    CONSTRAINT memory_embeddings_user_id_fkey FOREIGN KEY (user_id)
        REFERENCES public.user_context (user_id),
    VECTOR INDEX idx_memory_embeddings (embedding vector_l2_ops)
);

-- ------------------------------------------------------------
-- Table : sessions
-- Rôle : jetons de session émis à la connexion (signup/login),
-- consommés par le chat/l'upload pour authentifier chaque requête.
-- ------------------------------------------------------------
CREATE TABLE public.sessions (
    session_token STRING NOT NULL,
    user_id       UUID NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NULL DEFAULT now():::TIMESTAMPTZ,
    CONSTRAINT sessions_pkey PRIMARY KEY (session_token ASC),
    CONSTRAINT sessions_user_id_fkey FOREIGN KEY (user_id)
        REFERENCES public.user_context (user_id)
);
