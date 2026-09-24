FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install system dependencies (git, supervisor, procps, bash, curl)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    supervisor \
    curl \
    procps \
    bash \
    build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/log/supervisor /etc/supervisor/conf.d

COPY install.sh /usr/local/bin/
RUN sed -i 's/\r$//' /usr/local/bin/install.sh && chmod +x /usr/local/bin/install.sh

# Copy the validation script before install.sh runs
COPY validate.py ./

RUN /usr/local/bin/install.sh

COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy the rest of the application files
COPY . .

EXPOSE 5000
CMD ["python3", "cluster.py"]
