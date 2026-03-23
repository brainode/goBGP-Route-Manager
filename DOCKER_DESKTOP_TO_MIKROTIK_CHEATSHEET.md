# Docker Desktop to MikroTik Cheat Sheet

## Scope

This is the shortest practical flow for your target topology:

- Windows workstation with Docker Desktop builds the `route-manager` image
- VPS runs `gobgpd` from this repository
- MikroTik `hAP ax3` runs the `route-manager` container
- VPS and MikroTik already have a working WireGuard tunnel

Use `MIKROTIK_DEPLOYMENT.md` if you need the full explanation and validation steps.

## Variables You Replace

- `<VPS_SSH>`: SSH target of the VPS, for example `root@203.0.113.10`
- `<MT_SSH>`: SSH target of the MikroTik, for example `admin@192.168.88.1`
- `<VPS_WG_IP>`: WireGuard IP of the VPS reachable from MikroTik, for example `10.100.0.1`
- `<MT_CONTAINER_IP>`: LAN IP for the container on MikroTik, for example `192.168.88.250/24`
- `<MT_LAN_GW>`: MikroTik LAN gateway, for example `192.168.88.1`
- `<MT_BRIDGE>`: your LAN bridge name, for example `bridge`

## 1. Prepare goBGP Config on the VPS

Edit `gobgp/gobgpd.toml` so it matches the current host-installed daemon:

- local ASN
- router ID
- neighbors
- any policy settings you rely on

## 2. Stop the Host-Installed goBGP on the VPS

```bash
sudo systemctl disable --now gobgpd || sudo systemctl disable --now gobgp
```

## 3. Start Containerized goBGP on the VPS

```bash
git pull
export GOBGPD_API_HOSTS=<VPS_WG_IP>
docker compose --profile prod up --build -d gobgp-prod
ss -ltnp | grep -E ':179|:50051'
docker logs gobgp-prod --tail 100
```

Expected:

- `179/tcp` is listening for BGP peers
- `50051/tcp` is listening for goBGP gRPC on `<VPS_WG_IP>` only

## 4. Build Route Manager for MikroTik ARM64 on Windows

```powershell
docker buildx build --platform linux/arm64 -t gobgp-route-manager:arm64 -f Dockerfile --output type=docker,dest=route-manager-arm64.tar,compression=uncompressed,force-compression=true,oci-mediatypes=false .
python .\convert_routeros_image.py .\route-manager-arm64.tar .\route-manager-arm64-routeros.tar
```

Optional checksum:

```powershell
Get-FileHash .\route-manager-arm64-routeros.tar -Algorithm SHA256
```

## 5. Upload the Archive to MikroTik

Using SCP from PowerShell:

```powershell
scp .\route-manager-arm64-routeros.tar <MT_SSH>:/disk1/route-manager-arm64-routeros.tar
```

You can also upload it through WinBox Files.

## 6. Create Networking and Storage on MikroTik

Run on MikroTik:

```routeros
/interface/veth/add name=veth-route-manager address=<MT_CONTAINER_IP> gateway=<MT_LAN_GW>
/interface/bridge/port/add bridge=<MT_BRIDGE> interface=veth-route-manager
/container/mounts/add name=route-manager-data src=disk1/route-manager-data dst=/data
```

Example:

```routeros
/interface/veth/add name=veth-route-manager address=192.168.88.250/24 gateway=192.168.88.1
/interface/bridge/port/add bridge=bridge interface=veth-route-manager
/container/mounts/add list=route-manager-data src=disk1/route-manager-data dst=/data
```

## 7. Create Route Manager Environment on MikroTik

```routeros
/container/envs/add list=route-manager-envs key=APP_NAME value="goBGP Route Manager"
/container/envs/add list=route-manager-envs key=APP_HOST value="0.0.0.0"
/container/envs/add list=route-manager-envs key=APP_PORT value="8000"
/container/envs/add list=route-manager-envs key=DATABASE_URL value="sqlite:////data/route_manager.db"
/container/envs/add list=route-manager-envs key=GOBGP_ENABLED value="true"
/container/envs/add list=route-manager-envs key=GOBGP_BIN value="gobgp"
/container/envs/add list=route-manager-envs key=GOBGP_TIMEOUT value="10"
/container/envs/add list=route-manager-envs key=GOBGP_HOST value="<VPS_WG_IP>"
/container/envs/add list=route-manager-envs key=GOBGP_PORT value="50051"
/container/envs/add list=route-manager-envs key=GOBGP_USE_GRPC value="true"
/container/envs/add list=route-manager-envs key=GOBGP_GRPC_TIMEOUT value="10"
/container/envs/add list=route-manager-envs key=GOBGP_GRPC_FALLBACK_CLI value="true"
/container/envs/add list=route-manager-envs key=ROUTE_APPLY_WORKERS value="8"
/container/envs/add list=route-manager-envs key=IPINFO_TOKEN value=""
/container/envs/add list=route-manager-envs key=DISCOVERY_MAX_IPS value="12"
/container/envs/add list=route-manager-envs key=DISCOVERY_DNS_ATTEMPTS value="4"
/container/envs/add list=route-manager-envs key=DISCOVERY_DNS_DELAY_MS value="250"
/container/envs/add list=route-manager-envs key=DISCOVERY_IP_LOOKUP_TIMEOUT value="2"
/container/envs/add list=route-manager-envs key=DISCOVERY_PREFIX_LOOKUP_TIMEOUT value="6"
/container/envs/add list=route-manager-envs key=DISCOVERY_HTTP_RETRIES value="2"
/container/envs/add list=route-manager-envs key=DISCOVERY_RIPESTAT_TIMEOUT value="10"
/container/envs/add list=route-manager-envs key=DISCOVERY_ENABLE_BGPVIEW value="false"
```

## 8. Create and Start the MikroTik Container

```routeros
/container/add file=disk1/route-manager-arm64-routeros.tar interface=veth-route-manager root-dir=disk1/route-manager-root mountlists=route-manager-data envlist=route-manager-envs name=route-manager start-on-boot=yes logging=yes
/container/print
/container/start [find where name=route-manager]
```

Wait until extraction finishes and the container reaches `status=running`.

## 9. Validate End-to-End

From a LAN host:

```bash
curl http://192.168.88.250:8000/health
```

Open in browser:

- `http://192.168.88.250:8000/`
- `http://192.168.88.250:8000/gobgp-status`

Expected on `/gobgp-status`:

- binary check: `ok`
- daemon check: `ok`
- can apply routes: `ok`

Validate on the VPS:

```bash
docker exec -it gobgp-prod gobgp global rib
```

## 10. Firewall Minimum

Allow the container IP to:

- reach `<VPS_WG_IP>:50051/tcp`
- resolve DNS
- access `80/tcp` and `443/tcp` for discovery providers

Do not expose `50051/tcp` to the public Internet.

## 11. Rollback

If cutover fails:

On VPS:

```bash
docker compose --profile prod stop gobgp-prod
sudo systemctl start gobgpd || sudo systemctl start gobgp
```

On MikroTik:

```routeros
/container/stop [find where name=route-manager]
```

## 12. Practical Notes

- `hAP ax3` should use external storage for container data, not internal flash.
- The Route Manager database lives in `disk1/route-manager-data`.
- The image must be built for `linux/arm64`.
- If your current Transmission deployment already uses a working container network and storage model, reuse it instead of inventing a second pattern.
