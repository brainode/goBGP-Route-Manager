# MikroTik Deployment Guide

## Target Topology

This guide targets the following split deployment:

- VPS: `gobgpd` runs in a container from this repository
- MikroTik `hAP ax3`: `route-manager` runs as a RouterOS container
- Connectivity between MikroTik and VPS goes over an existing WireGuard tunnel
- The MikroTik already has a route to the VPS WireGuard IP

```text
LAN users
  -> Route Manager UI on MikroTik container
     -> MikroTik RouterOS
        -> WireGuard tunnel
           -> VPS
              -> gobgpd container
              -> BGP peers / upstreams
```

For the shortest operator-oriented sequence, see `DOCKER_DESKTOP_TO_MIKROTIK_CHEATSHEET.md`.

## Important Prerequisites

- Your MikroTik must already have container support working. If you already ran Transmission in a container, this prerequisite is already satisfied.
- The RouterOS container feature is officially available only on `arm`, `arm64` and `x86` architectures, and MikroTik recommends external storage instead of internal flash for container workloads.
- Build the Route Manager image for `linux/arm64`. The Dockerfiles in this repository were updated to support multi-arch GoBGP binaries.
- Keep goBGP gRPC (`TCP/50051`) reachable only across WireGuard, not from the public Internet.

## Cutover Plan

The safe migration order is:

1. Prepare the containerized `gobgpd` on the VPS, but do not cut over until config is verified
2. Build the `route-manager` image for MikroTik `arm64`
3. Pre-stage the Route Manager container on the MikroTik
4. Stop the system-installed goBGP on the VPS
5. Start the containerized `gobgpd` on the VPS
6. Verify BGP session recovery
7. Start Route Manager on the MikroTik and verify `/gobgp-status`

There will still be a brief BGP flap during cutover because port `179/tcp` cannot be owned by both the host daemon and the container at the same time.

## Part 1. Run goBGP Container on the VPS

### 1. Prepare Config

Edit `gobgp/gobgpd.toml` so that it matches the current host-installed daemon:

- same local ASN
- same router ID
- same neighbors
- same any policy or future capability settings you rely on

### 2. Stop the Host-Installed goBGP

Use the real service name from your VPS. Common examples:

```bash
sudo systemctl disable --now gobgpd
```

or

```bash
sudo systemctl disable --now gobgp
```

### 3. Start Containerized goBGP

You do not need to run the full prod profile on the VPS if Route Manager will live on the MikroTik. Start only the goBGP service:

```bash
export GOBGPD_API_HOSTS=<vps_wg_ip>
docker compose --profile prod up --build -d gobgp-prod
```

`GOBGPD_API_HOSTS` should be the VPS WireGuard IP. If you do not set it, `gobgp-prod` binds gRPC only to `127.0.0.1`, which is safer by default but will prevent the MikroTik from reaching `50051/tcp`.

### 4. Verify Listener State

On the VPS, confirm:

- BGP listener on `179/tcp`
- gRPC listener on `50051/tcp`

Example checks:

```bash
ss -ltnp | grep -E ':179|:50051'
docker logs gobgp-prod --tail 100
```

### 5. Restrict gRPC to WireGuard

Allow only the MikroTik WireGuard IP to reach `50051/tcp`.

Typical policy:

- `179/tcp`: public or peer-reachable as required by your topology
- `50051/tcp`: WireGuard-only

How you enforce this depends on your firewall stack:

- `nftables`
- `iptables`
- `ufw`
- cloud security groups

## Part 2. Build Route Manager for MikroTik ARM64

Build the image on your workstation:

```bash
docker buildx build --platform linux/arm64 -t gobgp-route-manager:arm64 -f Dockerfile --load .
docker save -o route-manager-arm64.tar gobgp-route-manager:arm64
```

If you build from Docker Desktop on Windows, the same commands work in PowerShell with Docker Buildx enabled.

## Part 3. Prepare MikroTik Storage and Networking

### 1. Storage

MikroTik officially recommends external storage for containers. Reuse the same USB storage you used for Transmission if it is already stable.

Example layout on the router:

- `disk1/route-manager-root`
- `disk1/route-manager-data`

### 2. Attach Container to the LAN Bridge

The cleanest setup is to give the container its own LAN IP through a `veth` interface and bridge port.

Example with placeholders:

```routeros
/interface/veth/add name=veth-route-manager address=192.168.88.250/24 gateway=192.168.88.1
/interface/bridge/port/add bridge=bridge-lan interface=veth-route-manager
```

Replace:

- `192.168.88.250` with a free LAN IP for the container
- `192.168.88.1` with the MikroTik LAN gateway
- `bridge-lan` with your actual bridge name

This lets LAN users access Route Manager directly at:

`http://192.168.88.250:8000`

and also lets the container reach the VPS WireGuard IP through normal router forwarding.

### 3. Add Persistent Mount

Route Manager stores SQLite under `/data`, so create a mount for that path:

```routeros
/container/mounts/add list=route-manager-data src=disk1/route-manager-data dst=/data
```

## Part 4. Configure Route Manager Environment on MikroTik

Create an env list for the container:

```routeros
/container/envs/add list=route-manager-envs key=APP_NAME value="goBGP Route Manager"
/container/envs/add list=route-manager-envs key=APP_HOST value="0.0.0.0"
/container/envs/add list=route-manager-envs key=APP_PORT value="8000"
/container/envs/add list=route-manager-envs key=DATABASE_URL value="sqlite:////data/route_manager.db"
/container/envs/add list=route-manager-envs key=GOBGP_ENABLED value="true"
/container/envs/add list=route-manager-envs key=GOBGP_HOST value="10.100.0.1"
/container/envs/add list=route-manager-envs key=GOBGP_PORT value="50051"
/container/envs/add list=route-manager-envs key=GOBGP_USE_GRPC value="true"
/container/envs/add list=route-manager-envs key=GOBGP_GRPC_FALLBACK_CLI value="true"
/container/envs/add list=route-manager-envs key=DISCOVERY_ENABLE_BGPVIEW value="false"
```

Replace `10.100.0.1` with the VPS WireGuard IP that exposes goBGP gRPC.

Optional tuning values you can also add:

- `DISCOVERY_MAX_IPS`
- `DISCOVERY_IP_LOOKUP_TIMEOUT`
- `DISCOVERY_PREFIX_LOOKUP_TIMEOUT`
- `DISCOVERY_HTTP_RETRIES`

## Part 5. Upload and Start the Route Manager Container

### 1. Upload the Image Archive

Upload `route-manager-arm64.tar` to the router `disk1/` using one of:

- WinBox Files
- SCP
- SFTP

### 2. Create the Container from File

```routeros
/container/add file=disk1/route-manager-arm64.tar interface=veth-route-manager root-dir=disk1/route-manager-root mountlists=route-manager-data envlist=route-manager-envs name=route-manager start-on-boot=yes logging=yes
```

Then start it:

```routeros
/container/start [find where name=route-manager]
```

## Part 6. Validation

### 1. Validate Container Networking

From a LAN host:

```bash
curl http://192.168.88.250:8000/health
```

Expected result:

```json
{"status":"ok"}
```

### 2. Validate goBGP Reachability

Open:

- `http://192.168.88.250:8000/gobgp-status`

The page should report:

- binary check: `ok`
- daemon check: `ok`
- can apply routes: `ok`

If `daemon check` fails, the usual causes are:

- wrong `GOBGP_HOST`
- `50051/tcp` not allowed over WireGuard
- VPS firewall still blocks the router
- the goBGP container is not listening yet

### 3. Validate a Real Route Operation

Use the UI to:

1. add a `next-hop`
2. create a manual site
3. add a single test prefix

Then verify on the VPS:

```bash
docker exec -it gobgp-prod gobgp global rib
```

You should see the newly advertised route in the goBGP RIB.

## Recommended Firewall Rules on MikroTik

If your forward policy is restrictive, explicitly allow the container IP to:

- reach the VPS WireGuard IP on `TCP/50051`
- resolve DNS
- access `80/tcp` and `443/tcp` for discovery providers

Example policy intent:

- `src=192.168.88.250 -> dst=<vps_wg_ip>:50051/tcp` allow
- `src=192.168.88.250 -> dst=<dns_server>:53/udp,tcp` allow
- `src=192.168.88.250 -> dst=internet:80,443/tcp` allow

## Operational Notes

- The container keeps desired state in SQLite on the MikroTik storage mount. Back up `disk1/route-manager-data`.
- If you already use the router for other containers, isolate Route Manager resources and monitor free RAM.
- Route Manager currently uses in-process background tasks, not a durable worker queue, so power loss during a large sync may leave desired state ahead of applied state until the next manual action.
- The current project has no authentication yet. Keep the UI inside trusted LAN/VPN segments.

## RouterOS Commands Used in This Guide

The MikroTik steps above are based on official RouterOS container primitives:

- `/container/add file=... interface=... root-dir=... mountlists=... envlist=...`
- `/container/mounts/add ...`
- `/container/envs/add ...`
- `/interface/veth/add ...`
- `/interface/bridge/port/add ...`

If your router is already successfully running another container workload, you can reuse the same storage and container enablement model.
