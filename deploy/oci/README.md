# Oracle VPS deployment

This deployment keeps Suwayomi and both bridge services on loopback. Caddy is
the only public listener and serves the Aidoku catalog at
`https://your-domain.example/index.min.json`.

## Before starting the containers

1. Publish a GitHub release tag so the repository workflow builds and pushes
   `ghcr.io/taisendev/aidoku-bridge-builder:<version>` for ARM64 and AMD64.
   Use that version tag in `compose.yaml`; do not rely on `latest` for a
   controlled deployment.
2. In Cloudflare, keep the hostname configured in `PUBLIC_DOMAIN` proxied and use SSL/TLS mode
   **Full (strict)**.
3. In the OCI subnet security list/network security group, allow TCP 80 and
   443 **only from Cloudflare's current proxy IP ranges**. Keep SSH restricted
   to your own address. Cloudflare publishes the authoritative ranges at
   <https://www.cloudflare.com/ips/>. Update the rules when Cloudflare changes
   those ranges.
4. The VPS host firewall must also permit TCP 80/443 from the same Cloudflare
   ranges. OCI rules alone do not override a host firewall. Keep direct public
   access to the origin blocked; forwarded client-IP headers are trusted for
   the per-IP abuse limits only under this condition.

Before bringing up the bridge, ensure Suwayomi's host listener is bound to
`127.0.0.1:4567`. For a native install, set `server.ip = "127.0.0.1"` in
Suwayomi's `server.conf`. For a container, publish the port as
`127.0.0.1:4567:4567`, never as `4567:4567`. Check with
`sudo ss -lntp | grep ':4567'`; the host-facing address must be loopback.
Do not add port 4567 to OCI ingress or the host firewall.

## Prepare the persistent directories

```sh
sudo mkdir -p /opt/suwayomi/aidoku/repo \
  /opt/suwayomi/aidoku/caddy-data \
  /opt/suwayomi/aidoku/caddy-config
sudo chown -R 1001:1001 /opt/suwayomi/aidoku
```

Copy this directory to `/opt/suwayomi/aidoku/deploy/oci`, edit the builder
image in `compose.yaml` to the release tag, then create the private token file
without printing the token:

```sh
cd /opt/suwayomi/aidoku/deploy/oci
umask 077
printf 'PUBLIC_DOMAIN=%s\nBRIDGE_TOKEN=%s\n' 'your-domain.example' "$(openssl rand -hex 32)" > .env
```

The same random token is embedded into generated WASM and required by the
gateway. The WASM is public, so this token is not a secret against someone who
can download a package; keep it out of logs and Git anyway.

Start the bridge services and Caddy:

```sh
docker compose pull
docker compose up -d
docker compose logs --tail=80 builder gateway caddy
```

After Caddy obtains a certificate, verify that `https://$PUBLIC_DOMAIN/index.min.json` loads and
shows catalog entries. Add that index URL in Aidoku. Aidoku's package download
installs only the requested catalog entry; the builder permits six package
downloads per client IP and 30 total per hour, and runs one package build at a
time with a 16-thread handler cap.

## Public routes

- `/index.min.json` and `/icons/*`: static catalog metadata.
- `/install/<catalog-package>.aix`: rate-limited on-demand installation and
  package generation for one listed extension.
- `/api/graphql`: token-authenticated bridge operations, limited to the
  approved read-only fetch/resolution mutations and source filter metadata.
- `/api/v1/bridge/media/<signed-handle>`: token-authenticated images and pages;
  all other Suwayomi routes are rejected by the gateway.

Suwayomi itself remains at `127.0.0.1:4567`; do not add an OCI ingress rule
for port 4567. The generated Aidoku index is separate from the iPad's Ikisaku
repository format; this deployment does not publish an Ikisaku index.
