FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    N_PREFIX=/usr/local/n

ENV PATH="/usr/local/n/bin:${PATH}"

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    supervisor \
    curl \
    procps \
    bash \
    build-essential \
    ca-certificates \
    xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/log/supervisor /etc/supervisor/conf.d

# Install uv for fast multi-version Python management (3.10 to 3.13)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN uv python install 3.10 3.11 3.12 3.13 \
    && ln -sf "$(uv python find 3.10)" /usr/local/bin/python3.10 \
    && ln -sf "$(uv python find 3.11)" /usr/local/bin/python3.11 \
    && ln -sf "$(uv python find 3.12)" /usr/local/bin/python3.12 \
    && ln -sf "$(uv python find 3.13)" /usr/local/bin/python3.13 \
    && find /opt/python -name "EXTERNALLY-MANAGED" -delete \
    && chmod -R 755 /opt/python

# Install Node.js version manager 'n' and Node.js 20 (LTS) & 22 (Current LTS/Latest)
RUN curl -fsSL https://raw.githubusercontent.com/tj/n/master/bin/n -o /usr/local/bin/n \
    && chmod +x /usr/local/bin/n \
    && n 20 \
    && n 22 \
    && ln -sf "$(n which 20)" /usr/local/bin/node20 \
    && ln -sf "$(n which 22)" /usr/local/bin/node22 \
    && ln -sf "$(n which 20 | sed 's#/node$#/npm#')" /usr/local/bin/npm20 \
    && ln -sf "$(n which 22 | sed 's#/node$#/npm#')" /usr/local/bin/npm22 \
    && ln -sf "$(n which 20 | sed 's#/node$#/npx#')" /usr/local/bin/npx20 \
    && ln -sf "$(n which 22 | sed 's#/node$#/npx#')" /usr/local/bin/npx22 \
    && n 22

COPY install.sh /usr/local/bin/
RUN sed -i 's/\r$//' /usr/local/bin/install.sh && chmod +x /usr/local/bin/install.sh
RUN /usr/local/bin/install.sh

COPY requirements.txt ./
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt
COPY . .

EXPOSE 5000
CMD ["python3", "cluster.py"]
