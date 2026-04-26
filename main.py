"""
Caspian Corridor Intelligence — Backend
FastAPI + PostGIS + WebSocket
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
    yield
    await pool.close()


app = FastAPI(title="Caspian Corridor Intelligence API", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def read_index():
    return FileResponse('static/index.html')

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # production-da dəqiq origin yazın
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

BAKU_PORT = (49.865, 40.342)   # (lon, lat)


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """İki GPS nöqtəsi arasındakı məsafəni km-lə hesab edir."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def calculate_eta(lon: float, lat: float, speed_knots: float, cargo_tons: float) -> ETAResponse:
    """
    Sadə ETA modeli:
      - Məsafə → Haversine
      - Sürət azalması: yük ağırlığına görə (+hava faktoru)
      - Confidence: sürət sabitliyinə görə
    """
    dist_km = haversine_km(lon, lat, BAKU_PORT[0], BAKU_PORT[1])
    dist_nm = dist_km / 1.852          # nautical mile

    # Hava faktoru: real sistemdə MeteoAz API-dan gələr
    weather_factor = round(random.uniform(0.88, 1.0), 2)

    # Yük ağırlığı sürəti azaldır (hər 100 ton = 0.05 knot)
    effective_speed = max(2.0, speed_knots * weather_factor - (cargo_tons / 100) * 0.05)

    hours = dist_nm / effective_speed if effective_speed > 0 else 99
    eta_dt = datetime.now(timezone.utc) + timedelta(hours=hours)

    # Confidence: sürət + məsafəyə görə
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
    """Bütün gəmiləri qaytarır. ?status=active ilə filter olunur."""
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
    """Gəminin Bakı limanına gəliş proqnozu."""
    row = await pool.fetchrow(
        "SELECT mmsi, ST_X(location) AS lon, ST_Y(location) AS lat, "
        "speed_knots, cargo_tons FROM vessels WHERE mmsi = $1",
        mmsi,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Gəmi tapılmadı")

    result = calculate_eta(row["lon"], row["lat"], row["speed_knots"], row["cargo_tons"])
    result.mmsi = mmsi

    # DB-ni yenilə
    await pool.execute(
        "UPDATE vessels SET eta = $1, eta_confidence = $2, updated_at = NOW() WHERE mmsi = $3",
        result.eta, result.confidence, mmsi,
    )
    return result


@app.get("/port/summary", response_model=PortSummary)
async def get_port_summary():
    """Bu gün Bakı limanına gəlməsi gözlənilən gəmilər + vaqon tələbatı."""
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
    # Hər 80 ton yük ≈ 1 vaqon (orta standart)
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
    """Bakı limanına {radius_km} km yaxın gəmilər."""
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
    """
    Demo rejimi: hər 5 saniyədə gəmilərin mövqeyini
    Bakı limanına doğru bir az irəlilədib broadcast edir.
    Real sistemdə AIS API-dan gəlir.
    """
    while True:
        await asyncio.sleep(5)
        if not manager.active or pool is None:
            continue
        try:
            rows = await pool.fetch(
                "SELECT mmsi, ST_X(location) AS lon, ST_Y(location) AS lat, "
                "speed_knots, course_deg, status FROM vessels WHERE status = 'active'"
            )
            updates = []
            for r in rows:
                # Bakı limanına doğru hər addımda ~0.002° irəlilə
                dlon = (BAKU_PORT[0] - r["lon"]) * 0.002
                dlat = (BAKU_PORT[1] - r["lat"]) * 0.002
                new_lon = r["lon"] + dlon + random.uniform(-0.001, 0.001)
                new_lat = r["lat"] + dlat + random.uniform(-0.001, 0.001)

                await pool.execute(
                    "UPDATE vessels SET location = ST_SetSRID(ST_MakePoint($1,$2),4326), "
                    "last_seen = NOW() WHERE mmsi = $3",
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
    """
    Frontend bu endpoint-ə qoşulur.
    Qoşulduqda mövcud gəmiləri göndərir,
    sonra hər 5 saniyədə yenilənmiş mövqeləri alır.
    """
    await manager.connect(ws)
    try:
        # Qoşulduqda bütün gəmiləri göndər
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

        # Bağlantını açıq saxla
        while True:
            await ws.receive_text()   # ping gözlə
    except WebSocketDisconnect:
        manager.disconnect(ws)


@app.on_event("startup")
async def start_simulation():
    asyncio.create_task(simulate_vessel_movement())