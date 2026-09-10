import os
import json
import subprocess
import asyncio
import logging
import calendar
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Header, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import requests
import google.auth
from google.auth.transport.requests import Request as GoogleAuthRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gcp-cost-dashboard")

app = FastAPI(title="GCP Cost & Resource Manager")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PROJECT_ID = os.environ.get("GCP_PROJECT", "kdt6-507500")
DASHBOARD_PIN = os.environ.get("DASHBOARD_PIN", "7500")

COMPUTE_BASE = "https://compute.googleapis.com/compute/v1"
STORAGE_BASE = "https://storage.googleapis.com/storage/v1"

# Hourly and Monthly Rates (USD)
VM_HOURLY_RATES = {
    "e2-micro": 0.0084,
    "e2-small": 0.0168,
    "e2-medium": 0.0336,
    "e2-standard-2": 0.0671,
    "e2-standard-4": 0.1343,
    "n1-standard-1": 0.0332,
}
VM_MONTHLY_RATES = {
    "e2-micro": 6.13,
    "e2-small": 12.26,
    "e2-medium": 24.52,
    "e2-standard-2": 49.03,
    "e2-standard-4": 98.06,
    "n1-standard-1": 24.27,
}
DISK_RATES_PER_GB_MONTH = {
    "pd-ssd": 0.17,
    "pd-balanced": 0.10,
    "pd-standard": 0.04,
    "pd-extreme": 0.125,
}
IP_HOURLY_RATE = 0.005
IP_MONTHLY_RATE = 3.60  # $0.005 * 720 hours
USD_TO_KRW = 1350.0


def get_access_token() -> str:
    """Retrieve OAuth2 access token via ADC or local gcloud fallback."""
    try:
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials.refresh(GoogleAuthRequest())
        if credentials.token:
            return credentials.token
    except Exception as e:
        logger.debug(f"google.auth.default: {e}")

    for cmd in [["gcloud.cmd", "auth", "print-access-token"], ["gcloud", "auth", "print-access-token"]]:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True, shell=True)
            token = res.stdout.strip()
            if token:
                return token
        except Exception:
            continue

    raise RuntimeError("GCP 액세스 토큰을 획득하지 못했습니다. Cloud Run 실행 환경 또는 gcloud 로그인을 확인하세요.")


async def make_gcp_request(method: str, url: str, params: Optional[dict] = None, json_data: Optional[dict] = None):
    token = get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=35.0) as client:
        resp = await client.request(method, url, headers=headers, params=params, json=json_data)
        if resp.status_code >= 400:
            logger.error(f"GCP API Error {resp.status_code} on {url}: {resp.text}")
            try:
                err_data = resp.json()
                err_msg = err_data.get("error", {}).get("message", resp.text)
            except Exception:
                err_msg = resp.text
            raise HTTPException(status_code=resp.status_code, detail=err_msg)
        return resp.json() if resp.text else {}


def calculate_uptime_and_accrued(creation_str: Optional[str], hourly_rate: float, is_running: bool = True):
    if not creation_str:
        return 0.0, 0.0
    try:
        dt = datetime.fromisoformat(creation_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff_hours = max(0.0, (now - dt).total_seconds() / 3600.0)
        accrued = (diff_hours * hourly_rate) if is_running else 0.0
        return round(diff_hours, 1), round(accrued, 3)
    except Exception:
        return 0.0, 0.0


def verify_pin(x_dashboard_pin: Optional[str] = Header(None)):
    if not x_dashboard_pin or x_dashboard_pin.strip() != DASHBOARD_PIN:
        raise HTTPException(status_code=401, detail="접속 PIN 번호가 올바르지 않습니다.")
    return True


@app.post("/api/verify-pin")
async def check_pin(payload: dict):
    pin = payload.get("pin", "")
    if str(pin).strip() == str(DASHBOARD_PIN).strip():
        return {"valid": True, "project": PROJECT_ID}
    return JSONResponse(status_code=401, content={"valid": False, "message": "잘못된 PIN 번호입니다."})


@app.get("/api/resources")
async def list_resources(_: bool = Depends(verify_pin)):
    try:
        # 1. Compute Instances
        inst_url = f"{COMPUTE_BASE}/projects/{PROJECT_ID}/aggregated/instances"
        instances_raw = await make_gcp_request("GET", inst_url)
        instances = []

        total_active_hourly_burn = 0.0

        if "items" in instances_raw:
            for zone_key, zone_data in instances_raw["items"].items():
                if "instances" in zone_data:
                    for item in zone_data["instances"]:
                        name = item.get("name")
                        zone = zone_key.replace("zones/", "")
                        status = item.get("status")
                        m_type = item.get("machineType", "").split("/")[-1]
                        creation_time = item.get("creationTimestamp")
                        
                        ext_ips = []
                        int_ips = []
                        for net_if in item.get("networkInterfaces", []):
                            if "networkIP" in net_if:
                                int_ips.append(net_if["networkIP"])
                            for ac in net_if.get("accessConfigs", []):
                                if "natIP" in ac:
                                    ext_ips.append(ac["natIP"])

                        hourly_vm = VM_HOURLY_RATES.get(m_type, 0.0336)
                        hourly_ip = len(ext_ips) * IP_HOURLY_RATE
                        total_hourly = (hourly_vm + hourly_ip) if status == "RUNNING" else 0.0
                        
                        uptime_hours, accrued_vm = calculate_uptime_and_accrued(creation_time, total_hourly, is_running=(status == "RUNNING"))

                        monthly_compute = VM_MONTHLY_RATES.get(m_type, 24.52) if status == "RUNNING" else 0.0
                        monthly_ip = len(ext_ips) * IP_MONTHLY_RATE if status == "RUNNING" else 0.0
                        total_monthly = monthly_compute + monthly_ip

                        total_active_hourly_burn += total_hourly

                        instances.append({
                            "name": name,
                            "zone": zone,
                            "machineType": m_type,
                            "status": status,
                            "creationTimestamp": creation_time,
                            "uptimeHours": uptime_hours,
                            "internalIps": int_ips,
                            "externalIps": ext_ips,
                            "hourlyBurnRate": round(total_hourly, 4),
                            "accruedCost": round(accrued_vm, 2),
                            "monthlyComputeCost": round(monthly_compute, 2),
                            "monthlyIpCost": round(monthly_ip, 2),
                            "totalMonthlyCost": round(total_monthly, 2),
                            "isBillable": total_monthly > 0,
                        })

        # 2. Compute Disks
        disks_url = f"{COMPUTE_BASE}/projects/{PROJECT_ID}/aggregated/disks"
        disks_raw = await make_gcp_request("GET", disks_url)
        disks = []
        if "items" in disks_raw:
            for zone_key, zone_data in disks_raw["items"].items():
                if "disks" in zone_data:
                    for item in zone_data["disks"]:
                        name = item.get("name")
                        zone = zone_key.replace("zones/", "")
                        size_gb = int(item.get("sizeGb", 0))
                        dtype = item.get("type", "").split("/")[-1]
                        creation_time = item.get("creationTimestamp")
                        users = [u.split("/")[-1] for u in item.get("users", [])]
                        
                        rate = DISK_RATES_PER_GB_MONTH.get(dtype, 0.10)
                        monthly_cost = round(size_gb * rate, 2)
                        hourly_rate = monthly_cost / 720.0

                        uptime_hours, accrued_disk = calculate_uptime_and_accrued(creation_time, hourly_rate, is_running=True)

                        total_active_hourly_burn += hourly_rate

                        disks.append({
                            "name": name,
                            "zone": zone,
                            "sizeGb": size_gb,
                            "type": dtype,
                            "isSsd": "ssd" in dtype,
                            "attachedTo": users,
                            "isAttached": len(users) > 0,
                            "creationTimestamp": creation_time,
                            "uptimeHours": uptime_hours,
                            "accruedCost": round(accrued_disk, 2),
                            "hourlyRate": round(hourly_rate, 4),
                            "monthlyCost": monthly_cost,
                            "isBillable": monthly_cost > 0,
                        })

        # 3. External IP Addresses (Reserved)
        addr_url = f"{COMPUTE_BASE}/projects/{PROJECT_ID}/aggregated/addresses"
        addresses_raw = await make_gcp_request("GET", addr_url)
        static_ips = []
        static_ip_strings = set()
        if "items" in addresses_raw:
            for reg_key, reg_data in addresses_raw["items"].items():
                if "addresses" in reg_data:
                    for item in reg_data["addresses"]:
                        name = item.get("name")
                        region = reg_key.replace("regions/", "")
                        address = item.get("address")
                        status = item.get("status")
                        creation_time = item.get("creationTimestamp")
                        users = [u.split("/")[-1] for u in item.get("users", [])]
                        static_ip_strings.add(address)

                        uptime_hours, accrued_ip = calculate_uptime_and_accrued(creation_time, IP_HOURLY_RATE, is_running=True)
                        
                        if not users:
                            total_active_hourly_burn += IP_HOURLY_RATE

                        static_ips.append({
                            "name": name,
                            "region": region,
                            "address": address,
                            "status": status,
                            "users": users,
                            "creationTimestamp": creation_time,
                            "uptimeHours": uptime_hours,
                            "accruedCost": round(accrued_ip, 2),
                            "monthlyCost": IP_MONTHLY_RATE,
                            "isBillable": True,
                        })

        # 4. Cloud Storage Buckets
        storage_url = f"{STORAGE_BASE}/b?project={PROJECT_ID}"
        buckets_raw = await make_gcp_request("GET", storage_url)
        buckets = []
        for b in buckets_raw.get("items", []):
            b_name = b.get("name", "")
            is_system = any(prefix in b_name for prefix in ["cloudbuild", "run-sources", "blueprint-config"])
            buckets.append({
                "name": b_name,
                "location": b.get("location"),
                "storageClass": b.get("storageClass"),
                "timeCreated": b.get("timeCreated"),
                "isSystem": is_system,
                "isBillable": False,
                "monthlyCost": 0.0,
            })

        # Projected totals for remaining active resources
        total_vm_cost = sum(i["monthlyComputeCost"] for i in instances)
        total_disk_cost = sum(d["monthlyCost"] for d in disks)
        total_ip_cost = (len(static_ips) + sum(len([ip for ip in i["externalIps"] if ip not in static_ip_strings]) for i in instances if i["status"] == "RUNNING")) * IP_MONTHLY_RATE
        grand_total = round(total_vm_cost + total_disk_cost + total_ip_cost, 2)

        # Baseline original monthly cost was $69.81
        saved_monthly = round(max(0.0, 69.81 - grand_total), 2)
        saved_krw = round(saved_monthly * USD_TO_KRW, 0)

        # Total accrued cost to date calculated directly from GCP resource uptime
        total_accrued = round(
            sum(i["accruedCost"] for i in instances) +
            sum(d["accruedCost"] for d in disks) +
            sum(ip["accruedCost"] for ip in static_ips),
            2
        )

        base_billed_krw = float(os.environ.get("BASE_MONTH_BILLED_KRW", "1488.0"))
        base_billed_usd = float(os.environ.get("BASE_MONTH_BILLED_USD", "1.102"))
        month_total_usd = round(base_billed_usd + total_accrued, 2)
        month_total_krw = round(base_billed_krw + (total_accrued * USD_TO_KRW), 0)

        return {
            "project": PROJECT_ID,
            "summary": {
                "currentMonth": datetime.now(timezone.utc).month,
                "monthTotalKrw": month_total_krw,
                "monthTotalUsd": month_total_usd,
                "baseBilledKrw": base_billed_krw,
                "baseBilledUsd": base_billed_usd,
                "liveAccruedUsd": total_accrued,
                "liveAccruedKrw": round(total_accrued * USD_TO_KRW, 0),
                "accruedCostToDate": total_accrued,
                "accruedCostToDateKrw": round(total_accrued * USD_TO_KRW, 0),
                "hourlyBurnRate": round(total_active_hourly_burn, 3),
                "hourlyBurnRateKrw": round(total_active_hourly_burn * USD_TO_KRW, 1),
                "grandTotalMonthly": grand_total,
                "grandTotalMonthlyKrw": round(grand_total * USD_TO_KRW, 0),
                "savedMonthlyUsd": saved_monthly,
                "savedMonthlyKrw": saved_krw,
                "vmCost": round(total_vm_cost, 2),
                "diskCost": round(total_disk_cost, 2),
                "ipCost": round(total_ip_cost, 2),
                "runningVmsCount": len([i for i in instances if i["status"] == "RUNNING"]),
                "totalVmsCount": len(instances),
                "disksCount": len(disks),
                "disksTotalGb": sum(d["sizeGb"] for d in disks),
                "staticIpsCount": len(static_ips),
                "bucketsCount": len(buckets),
                "billableResourcesCount": len([i for i in instances if i["isBillable"]]) + len([d for d in disks if d["isBillable"]]) + len(static_ips),
            },
            "instances": instances,
            "disks": disks,
            "staticIps": static_ips,
            "buckets": buckets,
        }
    except Exception as e:
        logger.exception("Error fetching resources")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/instances/{zone}/{name}/stop")
async def stop_instance(zone: str, name: str, _: bool = Depends(verify_pin)):
    url = f"{COMPUTE_BASE}/projects/{PROJECT_ID}/zones/{zone}/instances/{name}/stop"
    res = await make_gcp_request("POST", url)
    return {"status": "success", "message": f"VM '{name}' 중지 요청을 전송했습니다.", "result": res}


@app.post("/api/instances/{zone}/{name}/start")
async def start_instance(zone: str, name: str, _: bool = Depends(verify_pin)):
    url = f"{COMPUTE_BASE}/projects/{PROJECT_ID}/zones/{zone}/instances/{name}/start"
    res = await make_gcp_request("POST", url)
    return {"status": "success", "message": f"VM '{name}' 시작 요청을 전송했습니다.", "result": res}


@app.delete("/api/instances/{zone}/{name}")
async def delete_instance(zone: str, name: str, _: bool = Depends(verify_pin)):
    url = f"{COMPUTE_BASE}/projects/{PROJECT_ID}/zones/{zone}/instances/{name}"
    res = await make_gcp_request("DELETE", url)
    return {"status": "success", "message": f"VM '{name}' 삭제 요청을 전송했습니다.", "result": res}


@app.delete("/api/disks/{zone}/{name}")
async def delete_disk(zone: str, name: str, _: bool = Depends(verify_pin)):
    url = f"{COMPUTE_BASE}/projects/{PROJECT_ID}/zones/{zone}/disks/{name}"
    res = await make_gcp_request("DELETE", url)
    return {"status": "success", "message": f"디스크 '{name}' 삭제 요청을 전송했습니다.", "result": res}


@app.delete("/api/addresses/{region}/{name}")
async def delete_address(region: str, name: str, _: bool = Depends(verify_pin)):
    url = f"{COMPUTE_BASE}/projects/{PROJECT_ID}/regions/{region}/addresses/{name}"
    res = await make_gcp_request("DELETE", url)
    return {"status": "success", "message": f"고정 IP '{name}' 해제 요청을 전송했습니다.", "result": res}


@app.delete("/api/buckets/{name}")
async def delete_bucket(name: str, _: bool = Depends(verify_pin)):
    url = f"{STORAGE_BASE}/b/{name}"
    res = await make_gcp_request("DELETE", url)
    return {"status": "success", "message": f"버킷 '{name}' 삭제 요청을 전송했습니다.", "result": res}


@app.post("/api/actions/stop-all-vms")
async def stop_all_vms(_: bool = Depends(verify_pin)):
    inst_url = f"{COMPUTE_BASE}/projects/{PROJECT_ID}/aggregated/instances"
    instances_raw = await make_gcp_request("GET", inst_url)
    stopped = []
    errors = []

    if "items" in instances_raw:
        for zone_key, zone_data in instances_raw["items"].items():
            if "instances" in zone_data:
                for item in zone_data["instances"]:
                    if item.get("status") == "RUNNING":
                        name = item.get("name")
                        zone = zone_key.replace("zones/", "")
                        try:
                            stop_url = f"{COMPUTE_BASE}/projects/{PROJECT_ID}/zones/{zone}/instances/{name}/stop"
                            await make_gcp_request("POST", stop_url)
                            stopped.append(name)
                        except Exception as e:
                            errors.append(f"{name}: {str(e)}")

    return {
        "status": "success",
        "stopped": stopped,
        "errors": errors,
        "message": f"총 {len(stopped)}대의 실행 중인 VM에 중지 명령을 내렸습니다.",
    }


@app.post("/api/actions/cleanup-billable-only")
async def cleanup_billable_only(_: bool = Depends(verify_pin)):
    """Delete ONLY cost-incurring resources: VMs, detached disks (e.g. minecraft-disk), and static IPs.
    Leaves Cloud Run, Cloud Build, and system buckets completely intact!
    """
    deleted_vms = []
    deleted_disks = []
    deleted_ips = []
    errors = []

    # 1. Delete all VM instances
    inst_url = f"{COMPUTE_BASE}/projects/{PROJECT_ID}/aggregated/instances"
    instances_raw = await make_gcp_request("GET", inst_url)
    if "items" in instances_raw:
        for zone_key, zone_data in instances_raw["items"].items():
            if "instances" in zone_data:
                for item in zone_data["instances"]:
                    name = item.get("name")
                    zone = zone_key.replace("zones/", "")
                    try:
                        del_url = f"{COMPUTE_BASE}/projects/{PROJECT_ID}/zones/{zone}/instances/{name}"
                        await make_gcp_request("DELETE", del_url)
                        deleted_vms.append(name)
                    except Exception as e:
                        errors.append(f"VM {name}: {str(e)}")

    # 2. Delete static IP addresses
    addr_url = f"{COMPUTE_BASE}/projects/{PROJECT_ID}/aggregated/addresses"
    addresses_raw = await make_gcp_request("GET", addr_url)
    if "items" in addresses_raw:
        for reg_key, reg_data in addresses_raw["items"].items():
            if "addresses" in reg_data:
                for item in reg_data["addresses"]:
                    name = item.get("name")
                    region = reg_key.replace("regions/", "")
                    try:
                        del_addr = f"{COMPUTE_BASE}/projects/{PROJECT_ID}/regions/{region}/addresses/{name}"
                        await make_gcp_request("DELETE", del_addr)
                        deleted_ips.append(name)
                    except Exception as e:
                        errors.append(f"IP {name}: {str(e)}")

    # 3. Detached Disks (e.g. minecraft-disk 50GB SSD)
    await asyncio.sleep(1.5)
    disks_url = f"{COMPUTE_BASE}/projects/{PROJECT_ID}/aggregated/disks"
    disks_raw = await make_gcp_request("GET", disks_url)
    if "items" in disks_raw:
        for zone_key, zone_data in disks_raw["items"].items():
            if "disks" in zone_data:
                for item in zone_data["disks"]:
                    name = item.get("name")
                    zone = zone_key.replace("zones/", "")
                    users = item.get("users", [])
                    if not users:
                        try:
                            del_disk = f"{COMPUTE_BASE}/projects/{PROJECT_ID}/zones/{zone}/disks/{name}"
                            await make_gcp_request("DELETE", del_disk)
                            deleted_disks.append(name)
                        except Exception as e:
                            errors.append(f"디스크 {name}: {str(e)}")

    return {
        "status": "success",
        "deletedVms": deleted_vms,
        "deletedIps": deleted_ips,
        "deletedDisks": deleted_disks,
        "errors": errors,
        "message": f"비용 발생 리소스 정리 완료: VM {len(deleted_vms)}대, 디스크 {len(deleted_disks)}개, IP {len(deleted_ips)}개",
    }


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>대시보드 템플릿 파일을 찾을 수 없습니다.</h1>")
