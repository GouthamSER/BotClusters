FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    supervisor \
    curl \
    procps \
    bash \
    build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/log/supervisor /etc/supervisor/conf.d

# Install Python dependencies FIRST so validate.py has access to them
COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

# Setup and run install script
COPY install.sh /usr/local/bin/
RUN sed -i 's/\r$//' /usr/local/bin/install.sh && chmod +x /usr/local/bin/install.sh

# Copy validation script and execute install
COPY validate.py ./
RUN /usr/local/bin/install.sh

# Copy the rest of the application files
COPY . .

EXPOSE 5000
CMD ["python3", "cluster.py"]
