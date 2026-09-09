# PostgreSQL 16 with pgvector (base image) + Apache AGE (built from source).
# Single-DB stack (P5): graph (AGE) + embeddings (pgvector) + relational + RLS all in one server.
#
# Note: the AGE branch below targets PG16. If you change the base Postgres major version, change the
# AGE branch/tag to match (Apache AGE maintains per-major-version branches).

FROM pgvector/pgvector:pg16

ARG AGE_BRANCH=PG16
# Last resort for a TLS-inspecting proxy whose CA cannot be obtained. Prefer dropping that CA
# into deploy/certs/ (see below) over turning verification off:
#   docker compose build --build-arg GIT_SSL_VERIFY=false
ARG GIT_SSL_VERIFY=true

USER root

# Trust anchors for the AGE clone. Empty in a normal checkout, in which case the public bundle
# from ca-certificates is used; behind a TLS-inspecting proxy, drop its root CA here as a PEM
# file with a .crt extension and it is trusted for the rest of the build.
COPY certs/ /usr/local/share/ca-certificates/

# ca-certificates is a recommendation of git, so --no-install-recommends leaves the container
# without any trust store and the clone below fails with "CAfile: none". It is installed
# explicitly, which also marks it manual and keeps --auto-remove from purging it later.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        build-essential \
        git \
        flex \
        bison \
        postgresql-server-dev-16; \
    update-ca-certificates; \
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
