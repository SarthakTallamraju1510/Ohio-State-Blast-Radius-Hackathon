# The scorer builds this on linux/amd64 and runs it with no network.
# The digest is the image the challenge labels its commits with, so Python
# versions and test selection semantics stay stable.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ARG BASE_COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa

# Keep the exact base checkout in the build image.  The selector does not
# import it at runtime (the maps are precomputed), but retaining it documents
# the provenance of every map and supports local reproduction.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --quiet https://github.com/tobymao/sqlglot.git /opt/sqlglot \
    && git -C /opt/sqlglot checkout --quiet --detach "$BASE_COMMIT" \
    && test "$(git -C /opt/sqlglot rev-parse HEAD)" = "$BASE_COMMIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MAPS_DIR=/app/maps \
    DATA_DIR=/data

WORKDIR /app
COPY selector.py /app/selector.py
COPY maps /app/maps
RUN chmod 0555 /app/selector.py && chmod 0444 /app/maps/*

# The harness also passes --user 65534:65534; setting it here keeps local
# docker runs under the same identity.
USER 65534:65534

CMD ["python", "/app/selector.py"]
