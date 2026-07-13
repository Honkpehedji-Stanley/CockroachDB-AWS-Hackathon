#!/usr/bin/env bash
# ============================================================
# Déploiement de l'agent sur AWS Lambda (image Docker) avec
# une Function URL publique (CORS activé pour le frontend).
#
# Usage : ./deploy.sh
# Pré-requis : AWS CLI configuré, Docker, un .env rempli.
# ============================================================
set -euo pipefail

# --- Configuration (ajuste si besoin) ---
AWS_REGION="${AWS_REGION:-us-east-1}"
FUNCTION_NAME="ai-employee-agent"
ECR_REPO_NAME="ai-employee-agent"
LAMBDA_ROLE_NAME="ai-employee-lambda-role"

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}"

echo "==> Compte AWS : ${ACCOUNT_ID} | Région : ${AWS_REGION}"

# --- 1. Créer le repo ECR si besoin ---
aws ecr describe-repositories --repository-names "${ECR_REPO_NAME}" --region "${AWS_REGION}" \
  || aws ecr create-repository --repository-name "${ECR_REPO_NAME}" --region "${AWS_REGION}"

# --- 2. Build & push de l'image Docker ---
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

docker build -t "${ECR_REPO_NAME}" .
docker tag "${ECR_REPO_NAME}:latest" "${ECR_URI}:latest"
docker push "${ECR_URI}:latest"

# --- 3. Créer le rôle IAM d'exécution Lambda si besoin ---
if ! aws iam get-role --role-name "${LAMBDA_ROLE_NAME}" >/dev/null 2>&1; then
  echo "==> Création du rôle IAM ${LAMBDA_ROLE_NAME}"
  aws iam create-role --role-name "${LAMBDA_ROLE_NAME}" \
    --assume-role-policy-document '{
      "Version": "2012-10-17",
      "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]
    }'
  aws iam attach-role-policy --role-name "${LAMBDA_ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  aws iam attach-role-policy --role-name "${LAMBDA_ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/AmazonBedrockFullAccess
  aws iam attach-role-policy --role-name "${LAMBDA_ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess
  echo "==> Attente de la propagation IAM (10s)..."
  sleep 10
fi
ROLE_ARN=$(aws iam get-role --role-name "${LAMBDA_ROLE_NAME}" --query 'Role.Arn' --output text)

# --- 4. Variables d'environnement passées à Lambda depuis .env ---
# On génère un fichier JSON plutôt que la syntaxe "shorthand" de l'AWS CLI :
# cette dernière casse dès qu'une valeur contient des caractères spéciaux
# (ce qui est le cas de DATABASE_URL, pleine de : / ? =).
python3 - <<'PYEOF'
import json

# Variables réservées par le runtime Lambda : impossible de les redéfinir,
# Lambda les fournit lui-même automatiquement.
RESERVED_KEYS = {
    "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_EXECUTION_ENV",
    "AWS_LAMBDA_FUNCTION_NAME", "AWS_LAMBDA_FUNCTION_MEMORY_SIZE",
    "AWS_LAMBDA_FUNCTION_VERSION", "AWS_LAMBDA_LOG_GROUP_NAME",
    "AWS_LAMBDA_LOG_STREAM_NAME", "_HANDLER", "_X_AMZN_TRACE_ID",
}

env = {}
with open(".env") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in RESERVED_KEYS:
            continue
        env[key] = value.strip()

with open("env.json", "w") as f:
    json.dump({"Variables": env}, f)
PYEOF

# --- 5. Créer ou mettre à jour la fonction Lambda ---
if aws lambda get-function --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}" >/dev/null 2>&1; then
  echo "==> Mise à jour de la fonction existante"
  aws lambda update-function-code --function-name "${FUNCTION_NAME}" \
    --image-uri "${ECR_URI}:latest" --region "${AWS_REGION}"
  aws lambda wait function-updated --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}"
  aws lambda update-function-configuration --function-name "${FUNCTION_NAME}" \
    --environment file://env.json --timeout 30 --memory-size 512 --region "${AWS_REGION}"
else
  echo "==> Création de la fonction"
  aws lambda create-function --function-name "${FUNCTION_NAME}" \
    --package-type Image \
    --code "ImageUri=${ECR_URI}:latest" \
    --role "${ROLE_ARN}" \
    --timeout 30 --memory-size 512 \
    --environment file://env.json \
    --region "${AWS_REGION}"
  aws lambda wait function-active --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}"
fi

# --- 6. Créer la Function URL (endpoint HTTPS public, CORS ouvert) si besoin ---
if ! aws lambda get-function-url-config --function-name "${FUNCTION_NAME}" --region "${AWS_REGION}" >/dev/null 2>&1; then
  aws lambda create-function-url-config --function-name "${FUNCTION_NAME}" \
    --auth-type NONE \
    --cors '{"AllowOrigins":["*"],"AllowMethods":["POST"],"AllowHeaders":["content-type"]}' \
    --region "${AWS_REGION}"
  aws lambda add-permission --function-name "${FUNCTION_NAME}" \
    --statement-id FunctionURLAllowPublicAccess \
    --action lambda:InvokeFunctionUrl \
    --principal "*" \
    --function-url-auth-type NONE \
    --region "${AWS_REGION}"
fi

FUNCTION_URL=$(aws lambda get-function-url-config --function-name "${FUNCTION_NAME}" \
  --region "${AWS_REGION}" --query 'FunctionUrl' --output text)

echo ""
echo "✅ Déploiement terminé."
echo "==> URL de l'agent : ${FUNCTION_URL}"
echo "Teste avec :"
echo "curl -X POST ${FUNCTION_URL} -H 'Content-Type: application/json' -d '{\"user_name\":\"stanley\",\"message\":\"Salut !\"}'"