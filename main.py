"""
Caspian Corridor Intelligence — Backend
FastAPI + PostgreSQL (PostGIS-siz) + WebSocket
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

    async with pool.acquire() as conn:
        # PostGIS yoxdur — lon/lat FLOAT istifadə edirik
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS vessels (
                id SERIAL PRIMARY KEY,
                mmsi VARCHAR(9) UNIQUE NOT NULL,
                name VARCHAR(100),
                vessel_type VARCHAR(50),
                flag VARCHAR(3),
                length_m FLOAT,
                width_m FLOAT,
                lon FLOAT,
                lat FLOAT,
                speed_knots FLOAT,
                course_deg FLOAT,
                status VARCHAR(20) DEFAULT 'active',
                cargo_tons FLOAT,
                destination VARCHAR(100),
                eta TIMESTAMPTZ,
                eta_confidence FLOAT,
                last_seen TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id SERIAL PRIMARY KEY,
                vessel_mmsi VARCHAR(9),
                alert_type VARCHAR(50),
                severity VARCHAR(10),
                message TEXT,
                lon FLOAT,
                lat FLOAT,
                resolved BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS port_events (
                id SERIAL PRIMARY KEY,
                vessel_mmsi VARCHAR(9),
                event_type VARCHAR(30),
                port VARCHAR(50),
                cargo_tons FLOAT,
                wagons_needed INT,
                occurred_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)

        existing = await conn.fetchval("SELECT COUNT(*) FROM vessels")
        if existing == 0:
            await conn.execute("""
                INSERT INTO vessels (mmsi, name, vessel_type, flag, lon, lat,
                    speed_knots, course_deg, cargo_tons, destination, eta, eta_confidence)
                VALUES
                ('423001001','BAKU STAR','cargo','AZ',49.8,40.4,8.2,185,450,'Bakı Limanı',NOW()+INTERVAL '4 hours',0.87),
                ('423001002','XƏZƏR QIZILI','tanker','AZ',51.2,41.8,6.5,220,820,'Bakı Limanı',NOW()+INTERVAL '7 hours',0.72),
                ('423001003','ABŞERON','ferry','AZ',50.1,43.2,11.0,195,180,'Aktau',NOW()+INTERVAL '11 hours',0.91),
                ('436001001','AKTAU EXPRESS','cargo','KZ',51.8,44.1,7.8,170,560,'Bakı Limanı',NOW()+INTERVAL '9 hours',0.68),
                ('436001002','MANGISTAU','tanker','KZ',52.4,42.5,5.2,210,910,'Bakı Limanı',NOW()+INTERVAL '14 hours',0.55),
                ('438001001','TURKMENBASHI','cargo','TM',53.0,40.0,9.1,270,320,'Türkmənbaşı',NOW()+INTERVAL '6 hours',0.83),
                ('423001004','NEFTÇALA','cargo','AZ',50.8,40.1,7.3,200,300,'Bakı Limanı',NOW()+INTERVAL '5 hours',0.79),
                ('436001003','KAZAKH PRIDE','tanker','KZ',51.9,44.5,6.1,215,750,'Bakı Limanı',NOW()+INTERVAL '12 hours',0.61)
                ON CONFLICT (mmsi) DO NOTHING;
            """)
            await conn.execute("""
                INSERT INTO alerts (vessel_mmsi, alert_type, severity, message, lon, lat)
                VALUES
                ('436001002','long_wait','medium','MANGISTAU gəmisi 3 saatdır hərəkətsizdir — texniki problem şübhəsi',52.4,42.5),
                ('423001002','storm_zone','high','XƏZƏR QIZILI fırtına zonasına yaxınlaşır',51.2,41.8)
                ON CONFLICT DO NOTHING;
            """)

        print("✅ DB init tamamlandı")

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
# Əsas Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "caspian-corridor-intelligence"}


@app.get("/vessels", response_model=list[VesselOut])
async def get_vessels(status: Optional[str] = None):
    query = """
        SELECT mmsi, name, vessel_type, flag,
               lon, lat, speed_knots, course_deg, status, cargo_tons,
               destination, eta, eta_confidence, last_seen
        FROM vessels
        WHERE ($1::text IS NULL OR status = $1)
        ORDER BY last_seen DESC
    """
    rows = await pool.fetch(query, status)
    return [dict(r) for r in rows]


@app.get("/vessels/{mmsi}", response_model=VesselOut)
async def get_vessel(mmsi: str):
    row = await pool.fetchrow(
        """SELECT mmsi, name, vessel_type, flag,
                  lon, lat, speed_knots, course_deg, status, cargo_tons,
                  destination, eta, eta_confidence, last_seen
           FROM vessels WHERE mmsi = $1""",
        mmsi,
    )
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
        "UPDATE vessels SET eta=$1, eta_confidence=$2, updated_at=NOW() WHERE mmsi=$3",
        result.eta, result.confidence, mmsi,
    )
    return result


@app.get("/port/summary", response_model=PortSummary)
async def get_port_summary():
    rows = await pool.fetch(
        """SELECT mmsi, name, vessel_type, cargo_tons, eta, eta_confidence, lon, lat
           FROM vessels
           WHERE destination = 'Bakı Limanı'
             AND eta BETWEEN NOW() AND NOW() + INTERVAL '24 hours'
           ORDER BY eta ASC"""
    )
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
        """SELECT id, vessel_mmsi, alert_type, severity, message, created_at
           FROM alerts WHERE resolved=$1 ORDER BY created_at DESC LIMIT 50""",
        resolved,
    )
    return [dict(r) for r in rows]


@app.get("/vessels/nearby/port")
async def vessels_near_port(radius_km: float = 50):
    rows = await pool.fetch(
        "SELECT mmsi, name, vessel_type, lon, lat FROM vessels"
    )
    result = []
    for r in rows:
        dist = haversine_km(r["lon"], r["lat"], BAKU_PORT[0], BAKU_PORT[1])
        if dist <= radius_km:
            result.append({**dict(r), "dist_km": round(dist, 1)})
    result.sort(key=lambda x: x["dist_km"])
    return result

# ---------------------------------------------------------------------------
# AI Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/reschedule")
async def api_reschedule():
    rows = await pool.fetch(
        """SELECT mmsi, name, cargo_tons, lon, lat, speed_knots, eta
           FROM vessels
           WHERE destination = 'Bakı Limanı' AND status = 'active'
           ORDER BY eta ASC LIMIT 15"""
    )
    weather = ai_core.get_current_weather()
    result = []
    cumulative = 0
    for r in rows:
        eta_info = ai_core.calculate_eta(
            lon=r["lon"], lat=r["lat"],
            speed=r["speed_knots"] or 0,
            weather=weather,
        )
        hours = eta_info["hours_remaining"]
        rail_time = ai_core.dynamic_rail_sync(hours, weather)
        wagons = math.ceil((r["cargo_tons"] or 0) / 80)
        cumulative += wagons
        priority = "HIGH" if hours < 5 else "MEDIUM" if hours < 10 else "NORMAL"
        result.append({
            "mmsi": r["mmsi"],
            "name": r["name"],
            "cargo_tons": r["cargo_tons"],
            "eta_adjusted": rail_time,
            "wagons_needed": wagons,
            "wagons_cumulative": cumulative,
            "priority": priority,
            "anomaly": False,
        })

    total_cargo = sum(r["cargo_tons"] or 0 for r in rows)
    buffer = 2.5 + (3.0 if weather["is_stormy"] else 0)
    return {
        "weather": weather,
        "schedule": result,
        "total_vessels": len(result),
        "total_cargo_tons": round(total_cargo, 1),
        "total_wagons": cumulative,
        "buffer_hours": buffer,
        "storm_buffer_applied": weather["is_stormy"],
    }

@app.get("/api/anomalies")
async def api_anomalies():
    rows = await pool.fetch(
        "SELECT mmsi, name, lon, lat, speed_knots, course_deg, status, cargo_tons FROM vessels WHERE status != 'inactive'"
    )
    weather = ai_core.get_current_weather()
    results = []

    for r in rows:
        anomaly_msg = ai_core.detect_anomaly(
            r["speed_knots"] or 0,
            r["lat"],
            r["lon"],
            weather,
        )
        if "Təhlükəli" in anomaly_msg or "Həddi" in anomaly_msg:
            severity = "HIGH"
        elif "Zona" in anomaly_msg:
            severity = "MEDIUM"
        elif anomaly_msg != "✅ Stabil":
            severity = "LOW"
        else:
            severity = "NONE"

        results.append({
            "mmsi": r["mmsi"],
            "name": r["name"],
            "severity": severity,
            "message": anomaly_msg,
            "action": "Gəmi ilə əlaqə saxlayın" if severity == "HIGH" else None,
            "count": 1,
        })

    db_alerts = await pool.fetch(
        "SELECT vessel_mmsi, alert_type, severity, message FROM alerts WHERE resolved=FALSE ORDER BY created_at DESC LIMIT 20"
    )
    for a in db_alerts:
        results.append({
            "mmsi": a["vessel_mmsi"],
            "name": a["vessel_mmsi"],
            "severity": a["severity"].upper(),
            "message": a["message"],
            "action": None,
            "count": 1,
        })

    anomaly_count = sum(1 for r in results if r["severity"] != "NONE")
    return {
        "weather": weather,
        "results": results,
        "anomaly_count": anomaly_count,
        "total_scanned": len(rows),
    }


@app.get("/api/eta/all")
async def api_eta_all():
    rows = await pool.fetch(
        """SELECT mmsi, name, vessel_type, flag, cargo_tons, lon, lat,
                  speed_knots, destination
           FROM vessels WHERE status = 'active'"""
    )
    weather = ai_core.get_current_weather()
    result = []
    for r in rows:
        eta_obj = calculate_eta(r["lon"], r["lat"], r["speed_knots"], r["cargo_tons"] or 0)
        eta_obj.mmsi = r["mmsi"]
        hours = round((eta_obj.eta - datetime.now(timezone.utc)).total_seconds() / 3600, 1)
        result.append({
            "mmsi": r["mmsi"],
            "name": r["name"],
            "destination": r["destination"],
            "eta_display": eta_obj.eta.strftime("%d %b %H:%M"),
            "hours_remaining": max(0, hours),
            "distance_km": eta_obj.distance_km,
            "effective_speed_knots": eta_obj.speed_knots,
            "confidence_pct": f"{int(eta_obj.confidence * 100)}%",
        })
    result.sort(key=lambda x: x["hours_remaining"])
    return {"weather": weather, "eta_list": result, "count": len(result)}


@app.get("/api/satellite")
async def api_satellite():
    """Peyk görüntüsü məlumatı (Azersky simulasiyası)."""
    feed = ai_core.get_satellite_feed()
    vessel_count = await pool.fetchval("SELECT COUNT(*) FROM vessels WHERE status = 'active'")
    all_vessels = await pool.fetch("SELECT lon, lat FROM vessels WHERE status = 'active'")
    near_port = sum(
        1 for r in all_vessels
        if haversine_km(r["lon"], r["lat"], BAKU_PORT[0], BAKU_PORT[1]) <= 50
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
            if ws in self.active:
                self.active.remove(ws)


manager = ConnectionManager()


async def simulate_vessel_movement():
    while True:
        await asyncio.sleep(5)
        if not manager.active or pool is None:
            continue
        try:
            rows = await pool.fetch(
                "SELECT mmsi, lon, lat, speed_knots, course_deg, status FROM vessels WHERE status = 'active'"
            )
            updates = []
            for r in rows:
                dlon = (BAKU_PORT[0] - r["lon"]) * 0.002
                dlat = (BAKU_PORT[1] - r["lat"]) * 0.002
                new_lon = r["lon"] + dlon + random.uniform(-0.001, 0.001)
                new_lat = r["lat"] + dlat + random.uniform(-0.001, 0.001)
                await pool.execute(
                    "UPDATE vessels SET lon=$1, lat=$2, last_seen=NOW() WHERE mmsi=$3",
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
            """SELECT mmsi, name, vessel_type, flag,
                      lon, lat, speed_knots, course_deg,
                      status, cargo_tons, destination, eta
               FROM vessels ORDER BY last_seen DESC"""
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