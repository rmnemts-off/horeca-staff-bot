#!/bin/sh
# Container entry point.
#
# Assembles DATABASE_URL when it was not supplied, applies `alembic upgrade head`, and
# then hands over to the process named by CMD.
#
# RUN_MIGRATIONS=0 turns the migration step off. docker-compose.yml sets it for `bot` and
# `scheduler`, because the one-shot `migrate` service has already brought the schema to
# head by the time they start: two processes racing `upgrade head` on an empty database is
# a genuine failure mode, not a theoretical one.

set -eu

# --------------------------------------------------------------------------------------
# DATABASE_URL
# --------------------------------------------------------------------------------------
# The credentials have exactly one definition -- the POSTGRES_* block of
# docker-compose.yml, which the database server reads from the same YAML nodes. Spelling
# the DSN out in compose as well would repeat the password literal in a second place, and
# two places drift. So the URL is assembled here instead.
#
# A DATABASE_URL that is already set always wins: that is how a deployment passes a
# managed database, and how it passes a password containing characters that would have to
# be percent-encoded (`@`, `/`, `:`) -- naive assembly cannot encode them.

if [ -z "${DATABASE_URL:-}" ]; then
    for name in POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB POSTGRES_HOST POSTGRES_PORT; do
        eval "value=\${${name}:-}"
        if [ -z "${value}" ]; then
            echo "entrypoint: neither DATABASE_URL nor ${name} is set" >&2
            exit 1
        fi
    done
    DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
    export DATABASE_URL
    # The password is never printed, here or anywhere else.
    echo "entrypoint: DATABASE_URL assembled for ${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"
fi

# --------------------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------------------
# `barpoint` is the fallback in docker-compose.yml, and it is there so that
# `docker compose up` works on a fresh clone without an .env. A convenience of the local
# stand that survives into production is how a database ends up reachable with a password
# anyone can read in the repository, so production refuses to start on it instead of
# coming up quietly (TZ 9: credentials come from the environment).
#
# Only APP_ENV=prod is refused. A developer machine, CI and the test stand keep working
# exactly as before -- a guard that fires everywhere gets switched off everywhere.

LOCAL_STAND_PASSWORD="barpoint"

if [ "${APP_ENV:-dev}" = "prod" ]; then
    if [ "${POSTGRES_PASSWORD:-}" = "${LOCAL_STAND_PASSWORD}" ]; then
        echo "entrypoint: APP_ENV=prod with POSTGRES_PASSWORD still set to the local-stand" >&2
        echo "entrypoint: default from docker-compose.yml. Set a real password in the" >&2
        echo "entrypoint: environment (POSTGRES_PASSWORD or a full DATABASE_URL)." >&2
        exit 1
    fi
    case "${DATABASE_URL:-}" in
        *":${LOCAL_STAND_PASSWORD}@"*)
            echo "entrypoint: APP_ENV=prod with the local-stand database password inside" >&2
            echo "entrypoint: DATABASE_URL. Set a real password in the environment." >&2
            exit 1
            ;;
    esac
fi

# --------------------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------------------

RUN_MIGRATIONS="${RUN_MIGRATIONS:-1}"

if [ "${RUN_MIGRATIONS}" != "0" ]; then
    echo "entrypoint: alembic upgrade head"
    alembic upgrade head
    echo "entrypoint: schema is at head"
else
    echo "entrypoint: RUN_MIGRATIONS=0, skipping migrations"
fi

if [ "$#" -eq 0 ]; then
    echo "entrypoint: no command given, exiting"
    exit 0
fi

echo "entrypoint: exec $*"
exec "$@"
