#!/usr/bin/env bash
set -euo pipefail

database_uri="${DATABASE_CONNECTION_URI:-${DATABASE_URL:-}}"
if [[ -z "${database_uri}" ]]; then
  echo "DATABASE_CONNECTION_URI ou DATABASE_URL deve ser configurada." >&2
  exit 1
fi

# Prisma migrations need Supabase's session pool instead of transaction mode.
database_uri="$(DATABASE_URI="${database_uri}" node <<'NODE'
const url = new URL(process.env.DATABASE_URI);
if (url.hostname.endsWith(".pooler.supabase.com") && url.port === "6543") {
  url.port = "5432";
}
if (!url.searchParams.has("schema")) {
  url.searchParams.set("schema", "evolution_api");
}
process.stdout.write(url.toString());
NODE
)"

export DATABASE_CONNECTION_URI="${database_uri}"
export SERVER_PORT="${PORT:-${SERVER_PORT:-8080}}"

if [[ -n "${RENDER_EXTERNAL_HOSTNAME:-}" ]]; then
  export SERVER_URL="https://${RENDER_EXTERNAL_HOSTNAME}"
fi

cd /evolution
npm run db:deploy
npm run db:generate
exec npm run start:prod
