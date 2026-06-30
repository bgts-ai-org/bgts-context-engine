# PostgreSQL 16 with pgvector (base image) + Apache AGE (built from source).
# Single-DB stack (P5): graph (AGE) + embeddings (pgvector) + relational + RLS all in one server.
#
# Note: the AGE branch below targets PG16. If you change the base Postgres major version, change the
# AGE branch/tag to match (Apache AGE maintains per-major-version branches).

FROM pgvector/pgvector:pg16

ARG AGE_BRANCH=PG16
# Secure by default. Set to "false" only in environments with a TLS-inspecting proxy where the
# corporate CA is not present inside the build container:
#   docker compose build --build-arg GIT_SSL_VERIFY=false
ARG GIT_SSL_VERIFY=true

USER root

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        git \
        flex \
        bison \
        postgresql-server-dev-16; \
    git -c http.sslVerify="${GIT_SSL_VERIFY}" clone --depth 1 --branch "${AGE_BRANCH}" https://github.com/apache/age.git /tmp/age; \
    cd /tmp/age; \
    make PG_CONFIG=/usr/bin/pg_config; \
    make PG_CONFIG=/usr/bin/pg_config install; \
    cd /; \
    rm -rf /tmp/age; \
    apt-get purge -y --auto-remove build-essential git flex bison postgresql-server-dev-16; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

USER postgres
