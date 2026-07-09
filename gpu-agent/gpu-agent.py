#!/usr/bin/env python3
"""
gpu-agent — schlanker Stats-Endpoint für den GPU-Host.

Kapselt die Host-Checks, die llmproxy früher lokal ausgeführt hat (GPU/CPU/RAM
via nvidia-smi/psutil, Gaming-Mode via `pgrep steam`), als JSON-HTTP-Endpoint.
Läuft auf dem GPU-Host selbst (Port 11436), damit der auf dem Proxy-Host
laufende llmproxy diese Werte per Netzwerk-Poll abrufen kann — Hardware-Monitor
und Gaming-Mode bleiben so funktionsfähig, obwohl der Proxy nicht auf dem
GPU-Host läuft.

GET /status → {"hw": {...gleiche Felder wie llmproxy._hw_stats...}, "gaming_mode": bool}
"""

import asyncio
import time

import psutil
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

LISTEN_PORT = 11436
POLL_INTERVAL_HW = 2
POLL_INTERVAL_GAMING = 10

app = FastAPI()

class GamingOverrideRequest(BaseModel):
    override: str  # "auto", "on", "off"

_hw_stats: dict = {}
_gaming_mode: bool = False
_gaming_override: str = "auto"

async def _poll_gaming_mode():
    global _gaming_mode, _gaming_override
    prev_mode = False
    while True:
        try:
            if _gaming_override == "on":
                current_mode = True
            elif _gaming_override == "off":
                current_mode = False
            else:
                proc = await asyncio.create_subprocess_exec(
                    "pgrep", "-x", "steam",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                current_mode = (proc.returncode == 0)

            if current_mode != prev_mode:
                if current_mode:
                    # Gaming mode turned on, stop services
                    await asyncio.create_subprocess_exec(
                        "systemctl", "stop", "ollama", "comfyui", "whisper",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                else:
                    # Gaming mode turned off, start services
                    await asyncio.create_subprocess_exec(
                        "systemctl", "start", "ollama", "comfyui", "whisper",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                prev_mode = current_mode

            _gaming_mode = current_mode
        except Exception:
            pass
        await asyncio.sleep(POLL_INTERVAL_GAMING)


async def _poll_hardware():
    global _hw_stats
    while True:
        try:
            cpu = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            gpus = []
            try:
                proc = await asyncio.create_subprocess_exec(
                    "nvidia-smi",
                    "--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,power.default_limit",
                    "--format=csv,noheader,nounits",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await proc.communicate()
                for line in stdout.decode().strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 5:
                        gpus.append({
                            "index":     int(parts[0]),
                            "load_pct":  float(parts[1]),
                            "ram_used":  int(parts[2]),
                            "ram_total": int(parts[3]),
                            "temp_c":    float(parts[4]),
                            "power_w":       float(parts[5]) if len(parts) > 5 and parts[5].replace('.', '', 1).isdigit() else None,
                            "power_limit":   float(parts[6]) if len(parts) > 6 and parts[6].replace('.', '', 1).isdigit() else None,
                            "power_default": float(parts[7]) if len(parts) > 7 and parts[7].replace('.', '', 1).isdigit() else None,
                        })
            except Exception:
                pass

            cpu_temp_c = None
            sys_temps = []
            nvme_temps = []
            try:
                sensors = psutil.sensors_temperatures()
                for entry in sensors.get("coretemp", []):
                    if "package" in entry.label.lower():
                        cpu_temp_c = entry.current
                        break
                if cpu_temp_c is None:
                    for entry in sensors.get("k10temp", []):
                        if entry.label.lower() in ("tdie", "tccd1", "tctl"):
                            cpu_temp_c = entry.current
                            break
                for entry in sensors.get("acpitz", []):
                    sys_temps.append(round(entry.current, 1))
                seen_nvme = set()
                for entry in sensors.get("nvme", []):
                    if entry.label == "Composite":
                        t = round(entry.current, 1)
                        if t not in seen_nvme:
                            nvme_temps.append(t)
                            seen_nvme.add(t)
            except Exception:
                pass

            _hw_stats = {
                "cpu_pct":    cpu,
                "cpu_temp_c": round(cpu_temp_c, 1) if cpu_temp_c is not None else None,
                "ram_used":   vm.used // (1024 * 1024),
                "ram_total":  vm.total // (1024 * 1024),
                "ram_pct":    vm.percent,
                "gpus":       gpus,
                "sys_temps":  sys_temps,
                "nvme_temps": nvme_temps,
                "ts":         time.time(),
            }
        except Exception:
            pass
        await asyncio.sleep(POLL_INTERVAL_HW)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_poll_hardware())
    asyncio.create_task(_poll_gaming_mode())


@app.get("/status")
async def status():
    return {"hw": _hw_stats, "gaming_mode": _gaming_mode, "gaming_override": _gaming_override}

@app.post("/gaming_override")
async def set_gaming_override(req: GamingOverrideRequest):
    global _gaming_override
    if req.override in ("auto", "on", "off"):
        _gaming_override = req.override
    return {"status": "ok", "override": _gaming_override}

@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    print(f"gpu-agent listening on :{LISTEN_PORT}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=LISTEN_PORT)
