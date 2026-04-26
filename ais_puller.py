"""
ais_puller.py — AIS məlumatını çəkib DB-yə yazır.

İki rejim:
  - LIVE: MarineTraffic API açarı varsa real data
  - DEMO: açar yoxdursa fake Xəzər gəmiləri simulasiya edir

İstifadə:
    python ais_puller.py          # bir dəfə çəkir
    python ais_puller.py --loop   # hər 60 saniyədə təkrarlayır
"""

import argparse
import asyncio
import os
import random
from datetime import datetime, timezone

import asyncpg
import httpx

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://caspian:12345@localhost:5433/caspian_db")
MT_API_KEY   = os.getenv("MARINETRAFFIC_API_KEY", "")   # pulsuz tier: 50 req/gün

# Xəzər dənizi bounding box
CASPIAN_BOX = {"minlat": 36.5, "maxlat": 47.1, "minlon": 49.0, "maxlon": 54.5}


async def fetch_live_ais() -> list[dict]:
    """MarineTraffic GetVesselsInArea API — pulsuz açar kifayətdir."""
    url = "https://services.marinetraffic.com/api/getvessel/v:8"
    params = {
        "v": 8,
        "apikey": MT_API_KEY,
        "minlat": CASPIAN_BOX["minlat"],
        "maxlat": CASPIAN_BOX["maxlat"],
        "minlon": CASPIAN_BOX["minlon"],
        "maxlon": CASPIAN_BOX["maxlon"],
        "protocol": "jsono",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("DATA", [])


def generate_demo_vessels() -> list[dict]:
    """
    Real API yoxdursa hackathon demo üçün Xəzərdə 8-12 fake gəmi yaradır.
    Hər çağırışda mövqelər bir az dəyişir — "canlı" effekti verir.
    """
    templates = [
        ("423001001", "BAKU STAR",     "cargo",  "AZ", 50.2, 41.5),
        ("423001002", "XƏZƏR QIZILI",  "tanker", "AZ", 51.5, 42.1),
        ("423001003", "ABŞERON",       "ferry",  "AZ", 49.9, 44.0),
        ("436001001", "AKTAU EXPRESS", "cargo",  "KZ", 52.1, 43.8),
        ("436001002", "MANGISTAU",     "tanker", "KZ", 52.8, 42.3),
        ("438001001", "TURKMENBASHI",  "cargo",  "TM", 53.2, 39.8),
        ("423001004", "NEFTÇALA",      "cargo",  "AZ", 50.8, 40.1),
        ("436001003", "KAZAKH PRIDE",  "tanker", "KZ", 51.9, 44.5),
    ]
    vessels = []
    for mmsi, name, vtype, flag, base_lon, base_lat in templates:
        # Kiçik təsadüfi hərəkət — demo canlılığı üçün
        lon = base_lon + random.uniform(-0.05, 0.05)
        lat = base_lat + random.uniform(-0.05, 0.05)
        vessels.append({
            "MMSI":        mmsi,
            "SHIPNAME":    name,
            "SHIPTYPE":    vtype,
            "FLAG":        flag,
            "LON":         round(lon, 5),
            "LAT":         round(lat, 5),
            "SPEED":       round(random.uniform(4.0, 12.0), 1),
            "COURSE":      round(random.uniform(150, 270), 0),
            "DESTINATION": random.choice(["Bakı Limanı", "Aktau", "Türkmənbaşı"]),
            "CARGO_TONS":  round(random.uniform(200, 950), 0),
        })
    return vessels


async def upsert_vessels(pool: asyncpg.Pool, vessels: list[dict]):
    query = """
        INSERT INTO vessels
            (mmsi, name, vessel_type, flag, location,
             speed_knots, course_deg, cargo_tons, destination, last_seen)
        VALUES ($1,$2,$3,$4,
                ST_SetSRID(ST_MakePoint($5,$6),4326),
                $7,$8,$9,$10, NOW())
        ON CONFLICT (mmsi) DO UPDATE SET
            location    = EXCLUDED.location,
            speed_knots = EXCLUDED.speed_knots,
            course_deg  = EXCLUDED.course_deg,
            destination = EXCLUDED.destination,
            last_seen   = NOW(),
            updated_at  = NOW()
    """
    async with pool.acquire() as conn:
        for v in vessels:
            await conn.execute(
                query,
                str(v.get("MMSI")),
                v.get("SHIPNAME", "Unknown"),
                v.get("SHIPTYPE", "cargo"),
                v.get("FLAG", "??"),
                float(v.get("LON", 50.0)),
                float(v.get("LAT", 41.0)),
                float(v.get("SPEED", 0)),
                float(v.get("COURSE", 0)),
                float(v.get("CARGO_TONS", 0)),
                v.get("DESTINATION"),
            )
    print(f"[{datetime.now(timezone.utc).isoformat()}] {len(vessels)} gəmi DB-yə yazıldı.")


async def pull_once():
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    try:
        if MT_API_KEY:
            print("MarineTraffic API-dan canlı data çəkilir...")
            vessels = await fetch_live_ais()
        else:
            print("API açarı yoxdur — demo data istifadə edilir.")
            vessels = generate_demo_vessels()
        await upsert_vessels(pool, vessels)
    finally:
        await pool.close()


async def pull_loop(interval: int = 60):
    print(f"AIS puller başladı — hər {interval}s-də yenilənir.")
    while True:
        try:
            await pull_once()
        except Exception as e:
            print(f"Xəta: {e}")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Dövri rejim")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()

    if args.loop:
        asyncio.run(pull_loop(args.interval))
    else:
        asyncio.run(pull_once())