"""
Caspian Corridor Intelligence — Backend
FastAPI + PostGIS + WebSocket
New panels: Dynamic Rescheduling · Azersky Satellite · Anomaly Detection · ETA Forecast
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator, List, Optional

import asyncpg
from ai_engine import ai_core
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://caspian:12345@db:5432/caspian_db"
)
pool: asyncpg.Pool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    yield
    await pool.close()


app = FastAPI(title="Caspian Corridor Intelligence API", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def read_index():
    return FileResponse("static/index.html")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BAKU_PORT = (49.865, 40.342)

# ---------------------------------------------------------------------------
# Pydantic modellər
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
# Yardımçı
# ---------------------------------------------------------------------------
def haversine_km(lon1, lat1, lon2, lat2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def calculate_eta(lon, lat, speed_knots, cargo_tons):
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
# Əsas Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "caspian-corridor-intelligence"}


@app.get("/vessels", response_model=list[VesselOut])
async def get_vessels(status: Optional[str] = None):
    query = """
        SELECT mmsi, name, vessel_type, flag,
               ST_X(location) AS lon, ST_Y(location) AS lat,
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
               ST_X(location) AS lon, ST_Y(location) AS lat,
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
        "SELECT mmsi, ST_X(location) AS lon, ST_Y(location) AS lat, "
        "speed_knots, cargo_tons FROM vessels WHERE mmsi = $1", mmsi
    )
    if not row:
        raise HTTPException(status_code=404, detail="Gəmi tapılmadı")
    result = calculate_eta(row["lon"], row["lat"], row["speed_knots"], row["cargo_tons"])
    result.mmsi = mmsi
    await pool.execute(
        "UPDATE vessels SET eta=$1, eta_confidence=$2, updated_at=NOW() WHERE mmsi=$3",
        result.eta, result.confidence, mmsi,
    )
    return result


@app.get("/port/summary", response_model=PortSummary)
async def get_port_summary():
    query = """
        SELECT mmsi, name, vessel_type, cargo_tons, eta, eta_confidence,
               ST_X(location) AS lon, ST_Y(location) AS lat
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
        "FROM alerts WHERE resolved=$1 ORDER BY created_at DESC LIMIT 50",
        resolved,
    )
    return [dict(r) for r in rows]


@app.get("/vessels/nearby/port")
async def vessels_near_port(radius_km: float = 50):
    query = """
        SELECT mmsi, name, vessel_type,
               ST_X(location) AS lon, ST_Y(location) AS lat,
               ROUND(ST_Distance(
                   location::geography,
                   ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography
               ) / 1000) AS dist_km
        FROM vessels
        WHERE ST_DWithin(
            location::geography,
            ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography,
            $3 * 1000
        )
        ORDER BY dist_km ASC
    """
    rows = await pool.fetch(query, BAKU_PORT[0], BAKU_PORT[1], radius_km)
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# YENİ: Dynamic Rescheduling  /api/reschedule
# ---------------------------------------------------------------------------
@app.get("/api/reschedule")
async def api_reschedule():
    """
    Bütün aktiv gəmiləri götürüb dynamic rescheduling hesabı edir.
    Hava + ETA + Vaqon planı qaytarır.
    """
    rows = await pool.fetch(
        "SELECT mmsi, name, ST_X(location) AS lon, ST_Y(location) AS lat, "
        "speed_knots, course_deg, cargo_tons, status "
        "FROM vessels WHERE status = 'active'"
    )
    vessels = [dict(r) for r in rows]
    result = ai_core.dynamic_rail_sync(vessels)
    return result


# ---------------------------------------------------------------------------
# YENİ: Azersky Satellite Feed  /api/satellite
# ---------------------------------------------------------------------------
@app.get("/api/satellite")
async def api_satellite():
    """Azersky peyk məlumatları (AzerSpace-2 stub)."""
    feed = ai_core.get_satellite_feed()
    # Aktiv gəmilərin sayını DB-dən əlavə et
    count = await pool.fetchval("SELECT COUNT(*) FROM vessels WHERE status='active'")
    feed["db_active_vessels"] = count
    return feed


# ---------------------------------------------------------------------------
# YENİ: Anomaliya Aşkarlanması  /api/anomalies
# ---------------------------------------------------------------------------
@app.get("/api/anomalies")
async def api_anomalies():
    """Bütün aktiv gəmilər üçün anomaliya skanı."""
    rows = await pool.fetch(
        "SELECT mmsi, name, ST_X(location) AS lon, ST_Y(location) AS lat, "
        "speed_knots, course_deg, cargo_tons, status "
        "FROM vessels"
    )
    weather = ai_core.get_current_weather()
    results = []
    for r in rows:
        anom = ai_core.detect_anomaly(
            r["mmsi"], r["lon"], r["lat"],
            r["speed_knots"], r["course_deg"],
            r["status"], r["cargo_tons"], weather
        )
        results.append({
            "mmsi": r["mmsi"],
            "name": r["name"],
            **anom,
        })
    anomaly_count = sum(1 for x in results if x["anomaly"])
    return {
        "weather": weather,
        "total_scanned": len(results),
        "anomaly_count": anomaly_count,
        "results": results,
        "scanned_at": datetime.utcnow().isoformat() + "Z",
    }


# ---------------------------------------------------------------------------
# YENİ: ETA Proqnozu (bütün gəmilər)  /api/eta/all
# ---------------------------------------------------------------------------
@app.get("/api/eta/all")
async def api_eta_all():
    """Bütün gəmilər üçün AI-based ETA proqnozu."""
    rows = await pool.fetch(
        "SELECT mmsi, name, vessel_type, flag, "
        "ST_X(location) AS lon, ST_Y(location) AS lat, "
        "speed_knots, cargo_tons, destination, status "
        "FROM vessels ORDER BY mmsi"
    )
    weather = ai_core.get_current_weather()
    results = []
    for r in rows:
        eta_info = ai_core.calculate_eta(
            r["lon"], r["lat"], r["speed_knots"], r["cargo_tons"], weather
        )
        results.append({
            "mmsi": r["mmsi"],
            "name": r["name"],
            "vessel_type": r["vessel_type"],
            "flag": r["flag"],
            "destination": r["destination"],
            "status": r["status"],
            **eta_info,
        })
    results.sort(key=lambda x: x["hours_remaining"])
    return {
        "weather": weather,
        "vessel_count": len(results),
        "eta_list": results,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }


# ---------------------------------------------------------------------------
# YENİ: Tək gəmi üçün AI ETA  /api/eta/{mmsi}
# ---------------------------------------------------------------------------
@app.get("/api/eta/{mmsi}")
async def api_eta_vessel(mmsi: str):
    row = await pool.fetchrow(
        "SELECT mmsi, name, ST_X(location) AS lon, ST_Y(location) AS lat, "
        "speed_knots, cargo_tons, destination FROM vessels WHERE mmsi=$1", mmsi
    )
    if not row:
        raise HTTPException(404, "Gəmi tapılmadı")
    weather = ai_core.get_current_weather(row["lat"], row["lon"])
    eta_info = ai_core.calculate_eta(
        row["lon"], row["lat"], row["speed_knots"], row["cargo_tons"], weather
    )
    return {"mmsi": mmsi, "name": row["name"], "weather": weather, **eta_info}


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
        if ws in self.active:
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
    """
    Sürətli simulyasiya:
      - Hər 0.8 saniyədə yenilənir (əvvəl 5s idi)
      - Addım ölçüsü 0.06° (əvvəl 0.002° idi — 9× sürətli)
      - Kurs dinamik hesablanır (atan2)
      - Sürət dalğalanır ±0.3 kt
      - Limana 0.05° yaxınlaşanda 'anchored' statusuna keçir
    """
    while True:
        await asyncio.sleep(0.8)
        if not manager.active or pool is None:
            continue
        try:
            rows = await pool.fetch(
                "SELECT mmsi, ST_X(location) AS lon, ST_Y(location) AS lat, "
                "speed_knots, course_deg, status FROM vessels WHERE status = 'active'"
            )
            updates = []
            for r in rows:
                dx = BAKU_PORT[0] - r["lon"]
                dy = BAKU_PORT[1] - r["lat"]
                dist = math.sqrt(dx*dx + dy*dy)

                # Limana çatdısa anchored et
                if dist < 0.05:
                    await pool.execute(
                        "UPDATE vessels SET status='anchored', last_seen=NOW() WHERE mmsi=$1",
                        r["mmsi"]
                    )
                    updates.append({
                        "type": "vessel_update",
                        "mmsi": r["mmsi"],
                        "lon": round(r["lon"], 5),
                        "lat": round(r["lat"], 5),
                        "speed_knots": 0.0,
                        "course_deg": r["course_deg"],
                        "status": "anchored",
                        "ts": datetime.now(timezone.utc).isoformat(),
                    })
                    continue

                # Hərəkət addımı — 0.018° (≈2 km/addım)
                step = 0.06
                norm = dist if dist > 0 else 1
                dlon = (dx / norm) * step + random.uniform(-0.002, 0.002)
                dlat = (dy / norm) * step + random.uniform(-0.002, 0.002)

                new_lon = r["lon"] + dlon
                new_lat = r["lat"] + dlat

                # Dinamik kurs hesabı (atan2, şimal = 0°)
                course_rad = math.atan2(dx, dy)
                new_course = (math.degrees(course_rad) + 360) % 360

                # Sürət dalğalanması
                new_speed = max(2.0, r["speed_knots"] + random.uniform(-0.3, 0.3))

                await pool.execute(
                    "UPDATE vessels SET "
                    "location=ST_SetSRID(ST_MakePoint($1,$2),4326), "
                    "course_deg=$3, speed_knots=$4, last_seen=NOW() "
                    "WHERE mmsi=$5",
                    new_lon, new_lat, round(new_course, 1), round(new_speed, 2), r["mmsi"],
                )
                updates.append({
                    "type": "vessel_update",
                    "mmsi": r["mmsi"],
                    "lon": round(new_lon, 5),
                    "lat": round(new_lat, 5),
                    "speed_knots": round(new_speed, 1),
                    "course_deg": round(new_course, 1),
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
            "ST_X(location) AS lon, ST_Y(location) AS lat, "
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