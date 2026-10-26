# Compile the architecture-independent WASM on BuildKit's native platform.
# Otherwise the x86_64 Rust toolchain runs under QEMU on the ARM runner.
FROM --platform=$BUILDPLATFORM rust:1-bookworm AS forge
RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates python3 \
  && rm -rf /var/lib/apt/lists \
  && rustup target add wasm32-unknown-unknown
COPY template/ /src/
WORKDIR /src
RUN cargo build --release --target wasm32-unknown-unknown \
  && python3 -c "d=open('target/wasm32-unknown-unknown/release/suwayomi_bridge.wasm','rb').read(); assert d[:4]==b'\0asm' and len(d)>10000, 'bad wasm'" \
  && cp target/wasm32-unknown-unknown/release/suwayomi_bridge.wasm /bridge.wasm

FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=forge /bridge.wasm /wasm-stock/bridge.wasm
COPY builder.py gateway.py ./
CMD ["python", "-u", "builder.py"]
