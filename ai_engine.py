import math
import random
from datetime import datetime, timedelta


class AzconAIEngine:
    def __init__(self):
        self.port_coords = (40.342, 49.865)  # (lat, lon) — Bakı limanı

    def get_current_weather(self) -> dict:
        """Hava şəraiti (real layihədə OpenWeather API-dan gəlir)."""
        wind_speed = random.randint(5, 25)
        wave_height = round(random.uniform(0.3, 3.5), 1)
        visibility_km = random.randint(3, 20)
        is_stormy = wind_speed > 18 or wave_height > 2.5
        return {
            "wind_speed": wind_speed,
            "wind_unit": "m/s",
            "wave_height_m": wave_height,
            "visibility_km": visibility_km,
            "is_stormy": is_stormy,
            "status": "FIRTINA XƏBƏRDARLIĞI ⛈" if is_stormy else "Hava Normal ✅",
            "checked_at": datetime.now().strftime("%H:%M:%S"),
        }

    def calculate_eta(self, lat: float, lon: float, speed: float, weather: dict) -> float:
        """Hava nəzərə alınmış ETA (saat)."""
        if speed < 0.5:
            return 99.9
        dist = math.sqrt(
            (lat - self.port_coords[0]) ** 2 + (lon - self.port_coords[1]) ** 2
        ) * 111
        weather_factor = 1.4 if weather["is_stormy"] else 1.05
        eta_hours = (dist / speed) * weather_factor
        return round(eta_hours, 1)

    def dynamic_rail_sync(self, vessel_eta_hours: float, weather: dict) -> str:
        """Gəmi ETA-na görə dəmir yolu hazırlıq vaxtı."""
        buffer = 2.5
        if weather["is_stormy"]:
            buffer += 3.0
        ready_time = datetime.now() + timedelta(hours=vessel_eta_hours + buffer)
        return ready_time.strftime("%H:%M")

    def detect_anomaly(self, speed: float, lat: float, lon: float, weather: dict) -> str:
        """Anomaliya aşkarlanması."""
        if weather["is_stormy"] and speed > 15:
            return "⚠️ Təhlükəli Sürət (Fırtına)"
        if speed > 22:
            return "⚠️ Sürət Həddi Aşıldı"
        if speed < 0.3:
            return "⚠️ Gəmi Hərəkətsizdir"
        # Bakı limanına yaxın qapalı zona (nümunə)
        if 39.5 < lat < 40.5 and 49.5 < lon < 50.5:
            return "❗ Qapalı Zona Girişi"
        return "✅ Stabil"

    def get_satellite_feed(self) -> dict:
        """Azersky peyk simulasiyası."""
        return {
            "source": "Azersky-2 (Simulation)",
            "last_scan": datetime.now().strftime("%H:%M:%S"),
            "cloud_cover": f"{random.randint(5, 35)}%",
            "vessels_in_view": random.randint(40, 65),
            "sea_state": random.choice(["Sakit", "Orta", "Dalğalı"]),
            "image_quality": random.choice(["Yüksək", "Orta"]),
        }

    def predict_cargo_delay(self, cargo_tons: float, weather: dict) -> dict:
        """Yük boşaltma gecikmə proqnozu."""
        base_hours = cargo_tons / 150  # 150 ton/saat normal temp
        if weather["is_stormy"]:
            delay_factor = 1.6
            reason = "Fırtına — boşaltma yavaşlayır"
        else:
            delay_factor = 1.0
            reason = "Normal şərait"
        total_hours = round(base_hours * delay_factor, 1)
        return {
            "base_unload_hours": round(base_hours, 1),
            "delay_factor": delay_factor,
            "total_hours": total_hours,
            "reason": reason,
        }


ai_core = AzconAIEngine()