FROM python:3.12-slim
ARG TEMPLATE_RELEASE=v0.3.0
WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
  && apt-get install -y --no-install-recommends curl ca-certificates \
  && rm -rf /var/lib/apt/lists \
  && pip install --no-cache-dir -r requirements.txt \
  && mkdir -p /wasm \
  && curl -fsSL -o /wasm/bridge.wasm "https://github.com/TaisenDev/aidoku-bridge-template/releases/download/${TEMPLATE_RELEASE}/bridge.wasm" \
  && python3 -c "d=open('/wasm/bridge.wasm','rb').read(); assert d[:4]==b'\0asm' and len(d)>10000, 'bad bridge.wasm'"
COPY builder.py .
CMD ["python", "-u", "builder.py"]
