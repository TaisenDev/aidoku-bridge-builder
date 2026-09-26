FROM python:3.12-slim
ARG BRIDGE_WASM_URL=https://github.com/TaisenDev/aidoku-bridge-template/releases/latest/download/bridge.wasm
WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists \
 && pip install --no-cache-dir -r requirements.txt \
 && mkdir -p /wasm \
 && curl -fsSL -o /wasm/bridge.wasm "$BRIDGE_WASM_URL"
COPY builder.py .
CMD ["python", "-u", "builder.py"]
