"""
Caspian Corridor Intelligence — Backend
FastAPI + PostgreSQL + WebSocket (PostGIS-siz versiyon)
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator, Optional

import asyncpg
from ai_engine import ai_core
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# DB bağlantısı
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://caspian:12345@db:5432/caspian_db")

pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)

    # init.sql-i oxu və işlət
    init_sql_path = os.path.join(os.path.dirname(__file__), "init.sql")
    if os.path.exists(init_sql_path):
        with open(init_sql_path, "r") as f:
            sql = f.read()
        async with pool.acquire() as conn:
            try:
                await conn.execute(sql)
                print("✅ DB init tamamlandı")
            except Exception as e:
                print(f"⚠️ DB init xətası: {e}")

    yield
    await pool.close()


app = FastAPI(title="Caspian Corridor Intelligence API", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def read_index():
    return FileResponse('static/index.html')


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Modellər
# ---------------------------------------------------------------------------
class VesselOut(BaseModel):
    mmsi: str
    name: str
    vessel_type: str
    flag: str
    lon: float
    lat: float
    speed_knots: float
    course_deg: float
    status: str
    cargo_tons: float
    destination: Optional[str]
    eta: Optional[datetime]
    eta_confidence: Optional[float]
    last_seen: datetime


class AlertOut(BaseModel):
    id: int
    vessel_mmsi: str
    alert_type: str
    severity: str
    message: str
    created_at: datetime


class PortSummary(BaseModel):
    arrivals_today: int
    total_cargo_tons: float
    wagons_needed: int
    vessels: list[dict]


class ETAResponse(BaseModel):
    mmsi: str
    eta: datetime
    confidence: float
    distance_km: float
    speed_knots: float
    weather_factor: float


# ---------------------------------------------------------------------------
# Yardımçı funksiyalar
# ---------------------------------------------------------------------------
BAKU_PORT = (49.865, 40.342)  # (lon, lat)


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def calculate_eta(lon: float, lat: float, speed_knots: float, cargo_tons: float) -> ETAResponse:
    dist_km = haversine_km(lon, lat, BAKU_PORT[0], BAKU_PORT[1])
    dist_nm = dist_km / 1.852
    weather_factor = round(random.uniform(0.88, 1.0), 2)
    effective_speed = max(2.0, speed_knots * weather_factor - (cargo_tons / 100) * 0.05)
    hours = dist_nm / effective_speed if effective_speed > 0 else 99
    eta_dt = datetime.now(timezone.utc) + timedelta(hours=hours)
    confidence = round(max(0.4, min(0.97, 1.0 - (dist_km / 1500) * 0.4)), 2)
    return ETAResponse(
        mmsi="",
        eta=eta_dt,
        confidence=confidence,
        distance_km=round(dist_km, 1),
        speed_knots=round(effective_speed, 1),
        weather_factor=weather_factor,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "service": "caspian-corridor-intelligence"}


@app.get("/vessels", response_model=list[VesselOut])
async def get_vessels(status: Optional[str] = None):
    query = """
        SELECT mmsi, name, vessel_type, flag,
               lon, lat,
               speed_knots, course_deg, status, cargo_tons,
               destination, eta, eta_confidence, last_seen
        FROM vessels
        WHERE ($1::text IS NULL OR status = $1)
        ORDER BY last_seen DESC
    """
    rows = await pool.fetch(query, status)
    return [dict(r) for r in rows]


@app.get("/vessels/{mmsi}", response_model=VesselOut)
async def get_vessel(mmsi: str):
    query = """
        SELECT mmsi, name, vessel_type, flag,
               lon, lat,
               speed_knots, course_deg, status, cargo_tons,
               destination, eta, eta_confidence, last_seen
        FROM vessels WHERE mmsi = $1
    """
    row = await pool.fetchrow(query, mmsi)
    if not row:
        raise HTTPException(status_code=404, detail="Gəmi tapılmadı")
    return dict(row)


@app.get("/vessels/{mmsi}/eta", response_model=ETAResponse)
async def get_vessel_eta(mmsi: str):
    row = await pool.fetchrow(
        "SELECT mmsi, lon, lat, speed_knots, cargo_tons FROM vessels WHERE mmsi = $1",
        mmsi,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Gəmi tapılmadı")
    result = calculate_eta(row["lon"], row["lat"], row["speed_knots"], row["cargo_tons"])
    result.mmsi = mmsi
    await pool.execute(
        "UPDATE vessels SET eta = $1, eta_confidence = $2, updated_at = NOW() WHERE mmsi = $3",
        result.eta, result.confidence, mmsi,
    )
    return result


@app.get("/port/summary", response_model=PortSummary)
async def get_port_summary():
    query = """
        SELECT mmsi, name, vessel_type, cargo_tons, eta, eta_confidence, lon, lat
        FROM vessels
        WHERE destination = 'Bakı Limanı'
          AND eta BETWEEN NOW() AND NOW() + INTERVAL '24 hours'
        ORDER BY eta ASC
    """
    rows = await pool.fetch(query)
    total_cargo = sum(r["cargo_tons"] for r in rows)
    wagons = math.ceil(total_cargo / 80)
    return PortSummary(
        arrivals_today=len(rows),
        total_cargo_tons=round(total_cargo, 1),
        wagons_needed=wagons,
        vessels=[dict(r) for r in rows],
    )


@app.get("/alerts", response_model=list[AlertOut])
async def get_alerts(resolved: bool = False):
    rows = await pool.fetch(
        "SELECT id, vessel_mmsi, alert_type, severity, message, created_at "
        "FROM alerts WHERE resolved = $1 ORDER BY created_at DESC LIMIT 50",
        resolved,
    )
    return [dict(r) for r in rows]


@app.get("/vessels/nearby/port")
async def vessels_near_port(radius_km: float = 50):
    """Bakı limanına {radius_km} km yaxın gəmilər — Haversine ilə."""
    rows = await pool.fetch("SELECT mmsi, name, vessel_type, lon, lat FROM vessels")
    result = []
    for r in rows:
        dist = haversine_km(r["lon"], r["lat"], BAKU_PORT[0], BAKU_PORT[1])
        if dist <= radius_km:
            result.append({
                "mmsi": r["mmsi"],
                "name": r["name"],
                "vessel_type": r["vessel_type"],
                "lon": r["lon"],
                "lat": r["lat"],
                "dist_km": round(dist, 1),
            })
    result.sort(key=lambda x: x["dist_km"])
    return result


# ---------------------------------------------------------------------------
# Əskik olan 4 endpoint
# ---------------------------------------------------------------------------

@app.get("/api/reschedule")
async def api_reschedule():
    """Hava şəraitinə görə dəmir yolu sinxronizasiyası."""
    rows = await pool.fetch(
        """SELECT mmsi, name, cargo_tons,
                  ST_X(location) AS lon, ST_Y(location) AS lat,
                  speed_knots, eta
           FROM vessels
           WHERE destination = 'Bakı Limanı' AND status = 'active'
           ORDER BY eta ASC LIMIT 10"""
    )
    weather = ai_core.get_current_weather()
    result = []
    for r in rows:
        eta_hours = ai_core.calculate_eta(r["lat"], r["lon"], r["speed_knots"], weather)
        rail_time = ai_core.dynamic_rail_sync(eta_hours, weather)
        wagons = math.ceil((r["cargo_tons"] or 0) / 80)
        result.append({
            "mmsi": r["mmsi"],
            "name": r["name"],
            "cargo_tons": r["cargo_tons"],
            "eta_hours": eta_hours,
            "rail_ready_time": rail_time,
            "wagons_needed": wagons,
            "weather_status": weather["status"],
            "is_stormy": weather["is_stormy"],
        })
    return {"weather": weather, "schedule": result}


@app.get("/api/anomalies")
async def api_anomalies():
    """Anomaliya aşkarlanması — sürət, zona, fırtına."""
    rows = await pool.fetch(
        """SELECT mmsi, name,
                  ST_X(location) AS lon, ST_Y(location) AS lat,
                  speed_knots, status
           FROM vessels WHERE status = 'active'"""
    )
    weather = ai_core.get_current_weather()
    anomalies = []
    for r in rows:
        status = ai_core.detect_anomaly(
            r["speed_knots"], r["lat"], r["lon"], weather
        )
        if status != "✅ Stabil":
            anomalies.append({
                "mmsi": r["mmsi"],
                "name": r["name"],
                "anomaly": status,
                "speed_knots": r["speed_knots"],
                "lat": r["lat"],
                "lon": r["lon"],
            })
    # DB-dəki alertləri də əlavə et
    db_alerts = await pool.fetch(
        "SELECT vessel_mmsi, alert_type, severity, message, created_at "
        "FROM alerts WHERE resolved = FALSE ORDER BY created_at DESC LIMIT 20"
    )
    return {
        "weather": weather,
        "ai_anomalies": anomalies,
        "db_alerts": [dict(a) for a in db_alerts],
        "total": len(anomalies) + len(db_alerts),
    }


@app.get("/api/eta/all")
async def api_eta_all():
    """Bütün aktiv gəmilərin ETA hesablaması."""
    rows = await pool.fetch(
        """SELECT mmsi, name, vessel_type, flag, cargo_tons,
                  ST_X(location) AS lon, ST_Y(location) AS lat,
                  speed_knots, destination
           FROM vessels WHERE status = 'active'"""
    )
    weather = ai_core.get_current_weather()
    result = []
    for r in rows:
        eta_obj = calculate_eta(r["lon"], r["lat"], r["speed_knots"], r["cargo_tons"] or 0)
        eta_obj.mmsi = r["mmsi"]
        result.append({
            "mmsi": r["mmsi"],
            "name": r["name"],
            "vessel_type": r["vessel_type"],
            "flag": r["flag"],
            "destination": r["destination"],
            "eta": eta_obj.eta,
            "confidence": eta_obj.confidence,
            "distance_km": eta_obj.distance_km,
            "speed_knots": eta_obj.speed_knots,
            "weather_factor": eta_obj.weather_factor,
        })
    result.sort(key=lambda x: x["eta"])
    return {"weather": weather, "vessels": result, "count": len(result)}


@app.get("/api/satellite")
async def api_satellite():
    """Peyk görüntüsü məlumatı (Azersky simulasiyası)."""
    feed = ai_core.get_satellite_feed()
    vessel_count = await pool.fetchval("SELECT COUNT(*) FROM vessels WHERE status = 'active'")
    near_port = await pool.fetchval(
        """SELECT COUNT(*) FROM vessels
           WHERE ST_DWithin(
               location::geography,
               ST_SetSRID(ST_MakePoint($1,$2),4326)::geography,
               50000
           )""",
        BAKU_PORT[0], BAKU_PORT[1]
    )
    return {
        **feed,
        "active_vessels_db": vessel_count,
        "vessels_near_baku_port": near_port,
        "coverage_area": "Xəzər dənizi (36.5°N–47.1°N, 49°E–54.5°E)",
        "resolution_m": 1.5,
        "next_pass_minutes": random.randint(8, 45),
    }


# ---------------------------------------------------------------------------
# WebSocket — real vaxt gəmi hərəkəti
# ---------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, data: dict):
        msg = json.dumps(data, default=str)
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)


manager = ConnectionManager()


async def simulate_vessel_movement():
    while True:
        await asyncio.sleep(5)
        if not manager.active or pool is None:
            continue
        try:
            rows = await pool.fetch(
                "SELECT mmsi, lon, lat, speed_knots, course_deg, status "
                "FROM vessels WHERE status = 'active'"
            )
            updates = []
            for r in rows:
                dlon = (BAKU_PORT[0] - r["lon"]) * 0.002
                dlat = (BAKU_PORT[1] - r["lat"]) * 0.002
                new_lon = r["lon"] + dlon + random.uniform(-0.001, 0.001)
                new_lat = r["lat"] + dlat + random.uniform(-0.001, 0.001)
                await pool.execute(
                    "UPDATE vessels SET lon = $1, lat = $2, last_seen = NOW() WHERE mmsi = $3",
                    new_lon, new_lat, r["mmsi"],
                )
                updates.append({
                    "type": "vessel_update",
                    "mmsi": r["mmsi"],
                    "lon": round(new_lon, 5),
                    "lat": round(new_lat, 5),
                    "speed_knots": r["speed_knots"],
                    "course_deg": r["course_deg"],
                    "status": r["status"],
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
            if updates:
                await manager.broadcast({"vessels": updates})
        except Exception as e:
            print(f"Simulation error: {e}")


@app.websocket("/ws/vessels")
async def vessel_stream(ws: WebSocket):
    await manager.connect(ws)
    try:
        rows = await pool.fetch(
            "SELECT mmsi, name, vessel_type, flag, "
            "lon, lat, "
            "speed_knots, course_deg, status, cargo_tons, destination, eta "
            "FROM vessels ORDER BY last_seen DESC"
        )
        await ws.send_text(json.dumps({
            "type": "initial",
            "vessels": [dict(r) for r in rows],
        }, default=str))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


@app.on_event("startup")
async def start_simulation():
    asyncio.create_task(simulate_vessel_movement())
    # AIS puller-i də başlat (demo rejimində)
    from ais_puller import pull_loop
    asyncio.create_task(pull_loop(interval=60))