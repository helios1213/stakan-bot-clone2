import asyncio
import socket
import time
from curl_cffi import requests

SUBDOMAINS = [
    "api.mexc.com", "contract.mexc.com", "www.mexc.com", "futures.mexc.com",
    "api-pro.mexc.com", "pro.mexc.com", "vip.mexc.com", "mm.mexc.com",
    "institutional.mexc.com", "institution.mexc.com", "premium.mexc.com",
    "ws-fapi.mexc.com", "ws-api.mexc.com", "ws-trade.mexc.com", "ws-private.mexc.com",
    "fapi.mexc.com", "fstream.mexc.com",
    "gateway.mexc.com", "gw.mexc.com", "mobile-api.mexc.com", "m-api.mexc.com",
    "app-api.mexc.com", "mobile.mexc.com", "app.mexc.com",
    "edge.mexc.com", "matching.mexc.com", "match.mexc.com", "engine.mexc.com",
    "fast.mexc.com", "trade.mexc.com", "private.mexc.com",
    "api.mocortech.com", "mocortech.com", "futures-v3.mocortech.com",
    "kr.api.mexc.com", "jp.api.mexc.com", "hk.api.mexc.com",
    "sg.api.mexc.com", "asia.api.mexc.com",
    "365huo.xyz", "api.365huo.xyz", "flytogarden.com", "api.flytogarden.com",
]

PORTS = [443, 8443, 9443, 80, 8080]

PATHS = [
    "/", "/api/v1/contract/ping",
    "/api/v1/private/order/submit", "/api/v1/private/order/create",
    "/api/v1/private/order/place", "/api/v3/order", "/fapi/v1/order",
    "/order/place", "/order/submit", "/order/create",
    "/v1/order/submit", "/v1/order/create", "/v1/order/place",
    "/grpc", "/sse", "/stream", "/ws", "/ws-fapi/v1",
    "/edge", "/edge/trade", "/edge/order",
    "/private/order/submit", "/private/order/create", "/private/order/place",
    "/futures/order", "/futures/trade", "/contract/order", "/contract/trade",
    "/trade/order", "/trade/submit", "/trade/create",
    "/mexc.futures.v1.OrderService/SubmitOrder",
    "/mexc.contract.v1.OrderService/SubmitOrder",
]


async def check_dns(host):
    try:
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: socket.gethostbyname_ex(host)[2]
        )
    except Exception:
        return None


async def check_port(host, port, timeout=2):
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def check_path(session, host, port, path, timeout=3):
    proto = "https" if port in (443, 8443, 9443) else "http"
    url = f"{proto}://{host}:{port}{path}"
    try:
        t0 = time.perf_counter()
        r = await session.head(url, allow_redirects=False, timeout=timeout)
        dt = (time.perf_counter() - t0) * 1000
        return (r.status_code, dt, dict(r.headers))
    except Exception:
        return None


async def scan_subdomain(host, session):
    ips = await check_dns(host)
    if not ips:
        return None

    results = {"host": host, "ips": ips, "ports": [], "endpoints": []}

    port_tasks = [check_port(host, p) for p in PORTS]
    port_results = await asyncio.gather(*port_tasks)
    open_ports = [p for p, ok in zip(PORTS, port_results) if ok]
    results["ports"] = open_ports

    for port in open_ports:
        path_tasks = [check_path(session, host, port, path) for path in PATHS]
        path_results = await asyncio.gather(*path_tasks)
        for path, res in zip(PATHS, path_results):
            if res:
                status, dt, headers = res
                interesting = (
                    status in (200, 401, 403, 405, 400, 409, 422)
                    or "alt-svc" in headers
                    or "grpc" in str(headers).lower()
                    or "x-mexc" in str(headers).lower()
                )
                if interesting:
                    results["endpoints"].append({
                        "port": port, "path": path, "status": status,
                        "latency_ms": int(dt),
                        "server": headers.get("server", ""),
                        "alt_svc": headers.get("alt-svc", ""),
                        "all_headers": {k: v for k, v in headers.items()
                                        if k.lower() not in ("date", "set-cookie")},
                    })
    return results


async def main():
    print(f"Scanning {len(SUBDOMAINS)} subdomains, {len(PORTS)} ports, {len(PATHS)} paths")
    print()

    session = requests.AsyncSession(impersonate="chrome136", timeout=5)
    try:
        tasks = [scan_subdomain(host, session) for host in SUBDOMAINS]
        results = await asyncio.gather(*tasks)
    finally:
        await session.close()

    dns_ok = [r for r in results if r]
    print(f"=== DNS RESOLVED: {len(dns_ok)}/{len(SUBDOMAINS)} ===")
    for r in dns_ok:
        ips_str = ", ".join(r["ips"][:3])
        ports_str = f" [ports: {r['ports']}]" if r["ports"] else ""
        print(f"  {r['host']:50s} -> {ips_str}{ports_str}")

    with_endpoints = [r for r in dns_ok if r["endpoints"]]
    print(f"\n=== INTERESTING ENDPOINTS: {len(with_endpoints)} hosts ===\n")

    for r in with_endpoints:
        print(f"\n{'=' * 60}")
        print(f"HOST: {r['host']}  IPs: {r['ips']}  Ports: {r['ports']}")
        for ep in r["endpoints"]:
            alt = f" [alt-svc: {ep['alt_svc']}]" if ep["alt_svc"] else ""
            grpc = " [GRPC]" if "grpc" in str(ep["all_headers"]).lower() else ""
            mexc_hdr = " [X-MEXC]" if any("x-mexc" in k.lower() for k in ep["all_headers"]) else ""
            print(f"  :{ep['port']:5d} {ep['path']:50s} -> {ep['status']} ({ep['latency_ms']:4d}ms) {ep['server']}{alt}{grpc}{mexc_hdr}")


asyncio.run(main())
