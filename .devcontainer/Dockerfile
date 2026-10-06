# Dev container for the Databricks toolbox.
# Base: Python 3.12 - required by databricks-connect 17.x (DBR 17.3 LTS runs Python 3.12;
# Databricks Connect must match the cluster runtime, see requirements-dev.txt).
# CLIs that ship as devcontainer features (node, pwsh, az, aws, gh, terraform) are
# declared in devcontainer.json. Everything else is installed here with an explicit
# version ARG so bumps are one-line and reviewable. Never use "latest".
FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

ARG TARGETARCH
ARG DATABRICKS_CLI_VERSION=1.17.0
ARG GLAB_VERSION=1.118.0
ARG GCLOUD_VERSION=585.0.0-0
ARG OP_CLI_VERSION=2.39.0-1

USER root
ENV DEBIAN_FRONTEND=noninteractive

# ---- base packages -----------------------------------------------------------
# The base image ships a Yarn apt repo whose signing key has rotated; we don't use
# Yarn, so drop it before the first apt-get update.
RUN rm -f /etc/apt/sources.list.d/yarn.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      apt-transport-https ca-certificates curl gnupg jq less unzip zip \
 && rm -rf /var/lib/apt/lists/*

# ---- Databricks CLI (pinned release binary, not install.sh which floats) -----
RUN set -eux; \
    case "${TARGETARCH}" in amd64) arch=amd64 ;; arm64) arch=arm64 ;; *) echo "unsupported arch ${TARGETARCH}" && exit 1 ;; esac; \
    curl -fsSL -o /tmp/databricks.zip \
      "https://github.com/databricks/cli/releases/download/v${DATABRICKS_CLI_VERSION}/databricks_cli_${DATABRICKS_CLI_VERSION}_linux_${arch}.zip"; \
    unzip -q /tmp/databricks.zip -d /tmp/databricks; \
    install -m 0755 /tmp/databricks/databricks /usr/local/bin/databricks; \
    rm -rf /tmp/databricks /tmp/databricks.zip; \
    databricks --version

# ---- GitLab CLI (glab) -------------------------------------------------------
RUN set -eux; \
    case "${TARGETARCH}" in amd64) arch=amd64 ;; arm64) arch=arm64 ;; esac; \
    curl -fsSL -o /tmp/glab.deb \
      "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_${arch}.deb"; \
    apt-get install -y /tmp/glab.deb; \
    rm -f /tmp/glab.deb; \
    glab --version

# ---- Google Cloud CLI (pinned apt package) -----------------------------------
RUN set -eux; \
    curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
      | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg; \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
      > /etc/apt/sources.list.d/google-cloud-sdk.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends "google-cloud-cli=${GCLOUD_VERSION}"; \
    rm -rf /var/lib/apt/lists/*; \
    gcloud --version | head -1

# ---- 1Password CLI (op) - resolves op:// references in .devcontainer/.env ----
RUN set -eux; \
    case "${TARGETARCH}" in amd64) arch=amd64 ;; arm64) arch=arm64 ;; esac; \
    curl -fsSL https://downloads.1password.com/linux/keys/1password.asc \
      | gpg --dearmor -o /usr/share/keyrings/1password-archive-keyring.gpg; \
    echo "deb [arch=${arch} signed-by=/usr/share/keyrings/1password-archive-keyring.gpg] https://downloads.1password.com/linux/debian/${arch} stable main" \
      > /etc/apt/sources.list.d/1password.list; \
    mkdir -p /etc/debsig/policies/AC2D62742012EA22/ /usr/share/debsig/keyrings/AC2D62742012EA22; \
    curl -fsSL https://downloads.1password.com/linux/debian/debsig/1password.pol \
      -o /etc/debsig/policies/AC2D62742012EA22/1password.pol; \
    curl -fsSL https://downloads.1password.com/linux/keys/1password.asc \
      | gpg --dearmor -o /usr/share/debsig/keyrings/AC2D62742012EA22/debsig.gpg; \
    apt-get update; \
    apt-get install -y --no-install-recommends "1password-cli=${OP_CLI_VERSION}"; \
    rm -rf /var/lib/apt/lists/*; \
    op --version

# ---- shell hook: every interactive bash/zsh loads the single keys file -------
# The loader lives in the workspace so it is versioned with the repo; the hook only
# sources it if the workspace is mounted (it is a no-op otherwise).
RUN printf '%s\n' \
      '# devcontainer: put .devcontainer/bin on PATH and load .devcontainer/.env (literals + 1Password op:// refs)' \
      'for _w in /workspaces/*/.devcontainer; do' \
      '  [ -d "$_w/bin" ] && case ":$PATH:" in *":$_w/bin:"*) ;; *) PATH="$_w/bin:$PATH" ;; esac' \
      '  [ -r "$_w/secrets/load-env.sh" ] && . "$_w/secrets/load-env.sh"' \
      'done; unset _w; export PATH' \
      > /etc/profile.d/devcontainer-env.sh \
 && printf '%s\n' '[ -r /etc/profile.d/devcontainer-env.sh ] && . /etc/profile.d/devcontainer-env.sh' \
      >> /etc/bash.bashrc \
 && printf '%s\n' '[ -r /etc/profile.d/devcontainer-env.sh ] && . /etc/profile.d/devcontainer-env.sh' \
      >> /etc/zsh/zshrc

# Mount points must exist and be owned by the container user, otherwise Docker
# creates them as root on first bind.
RUN mkdir -p /home/vscode/.aws /home/vscode/.azure /home/vscode/.claude /home/vscode/.gemini \
             /home/vscode/.config/gh /home/vscode/.config/glab-cli /home/vscode/.config/gcloud /home/vscode/.config/op \
 && touch /home/vscode/.databrickscfg \
 && chown -R vscode:vscode /home/vscode

USER vscode
