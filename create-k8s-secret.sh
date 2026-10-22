#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
env_file="$project_dir/.env"
namespace_file="$project_dir/k8s/namespace.yaml"
if [[ ! -f "$namespace_file" ]]; then
  echo "Namespace manifest not found: $namespace_file" >&2
  exit 1
fi

manifest_namespace="$(awk '
  /^metadata:[[:space:]]*$/ { in_metadata = 1; next }
  in_metadata && /^[^[:space:]#][^:]*:/ { exit }
  in_metadata && /^[[:space:]]+name:[[:space:]]*/ {
    sub(/^[[:space:]]+name:[[:space:]]*/, "")
    sub(/[[:space:]]+#.*$/, "")
    gsub(/^["\047]|["\047]$/, "")
    print
    found = 1
    exit
  }
  END { if (!found) exit 1 }
' "$namespace_file")" || {
  echo "Could not read metadata.name from $namespace_file" >&2
  exit 1
}

if [[ -z "$manifest_namespace" ]]; then
  echo "Namespace manifest has an empty metadata.name: $namespace_file" >&2
  exit 1
fi

namespace="$manifest_namespace"
secret_name="kube-trader-secrets"

usage() {
  cat <<'EOF'
Create or update the Kubernetes Secret from selected entries in .env.

Defaults: .env and the namespace in k8s/namespace.yaml.
Usage: ./create-k8s-secret.sh [--env-file PATH] [--namespace NAME] [--secret-name NAME]
EOF
}

while (($#)); do
  case "$1" in
    --env-file)
      (($# >= 2)) || { echo "Missing value for --env-file" >&2; exit 2; }
      env_file="$2"
      shift 2
      ;;
    --namespace)
      (($# >= 2)) || { echo "Missing value for --namespace" >&2; exit 2; }
      namespace="$2"
      shift 2
      ;;
    --secret-name)
      (($# >= 2)) || { echo "Missing value for --secret-name" >&2; exit 2; }
      secret_name="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$env_file" ]]; then
  echo "Environment file not found: $env_file" >&2
  exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl is required but was not found in PATH." >&2
  exit 1
fi

umask 077
secret_env="$(mktemp)"
trap 'rm -f "$secret_env"' EXIT

awk '
  BEGIN {
    count = split("ALPACA_API_KEY ALPACA_SECRET_KEY FINNHUB_API_KEY POSTGRES_PASSWORD POSTGRES_DSN ALERT_WEBHOOK_URL", keys, " ")
    for (i = 1; i <= count; i++) allowed[keys[i]] = 1
    required["POSTGRES_PASSWORD"] = 1
    required["POSTGRES_DSN"] = 1
  }
  {
    line = $0
    sub(/\r$/, "", line)
    if (line ~ /^[[:space:]]*($|#)/) next

    separator = index(line, "=")
    if (!separator) next
    key = substr(line, 1, separator - 1)
    gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
    if (!(key in allowed)) next

    if (key in seen) {
      printf "Duplicate %s entry in environment file.\n", key > "/dev/stderr"
      failed = 1
      next
    }

    value = substr(line, separator + 1)
    if ((key in required) && value == "") {
      printf "%s must have a value in the environment file.\n", key > "/dev/stderr"
      failed = 1
      next
    }
    if (value ~ /^["\047]/) {
      printf "%s must use an unquoted KEY=value entry for Kubernetes env-file format.\n", key > "/dev/stderr"
      failed = 1
      next
    }

    print key "=" value
    seen[key] = 1
  }
  END {
    for (key in required) {
      if (!(key in seen)) {
        printf "Missing required %s entry in environment file.\n", key > "/dev/stderr"
        failed = 1
      }
    }
    if (failed) exit 1
  }
' "$env_file" > "$secret_env"

if [[ "$namespace" == "$manifest_namespace" ]]; then
  kubectl apply -f "$namespace_file"
else
  kubectl create namespace "$namespace" --dry-run=client -o yaml |
    kubectl apply -f -
fi

kubectl -n "$namespace" create secret generic "$secret_name" \
  --from-env-file="$secret_env" \
  --dry-run=client -o yaml |
  kubectl apply -f -
