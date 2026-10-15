# Pinned by tag only. A tag can be moved to a new image at any time;
# to pin exactly, replace it with a digest:
#   FROM python:3.12-slim@sha256:<digest>
# (get it with: docker pull python:3.12-slim && docker inspect --format '{{index .RepoDigests 0}}' python:3.12-slim)
FROM python:3.12-slim

# git is only needed for working inside the container; the experiment itself
# uses the Python standard library and nothing else.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m dev
USER dev
WORKDIR /workspaces/selection-and-mutation-testing
