import math
import random
from datetime import datetime, timedelta


class AzconAIEngine:
    def __init__(self):
        self.port_coords = (40.02, 51.01)

    # 1. Hava Şəraiti Modulu
    def get_current_weather(self):
        # Real layihədə bura API (məs: OpenWeather) bağlana bilər
        # Xəzər üçün kritik külək sürəti 15 m/s+ hesab olunur
        wind_speed = random.randint(5, 25)
        is_stormy = wind_speed > 18
        return {
            "wind_speed": wind_speed,
            "is_stormy": is_stormy,
            "status": "FIRTINA XƏBƏRDARLIĞI" if is_stormy else "Hava Normal"
        }

    # 2. Hava Nəzərə Alınmış ETA
    def calculate_eta(self, lat, lon, speed, weather):
        if speed < 0.5: return 99.9

        dist = math.sqrt((lat - self.port_coords[0]) ** 2 + (lon - self.port_coords[1]) ** 2) * 111

        # AI Weather Adjustment: Külək gəmini 20-40% ləngidə bilər
        weather_factor = 1.4 if weather['is_stormy'] else 1.05

        eta_hours = (dist / speed) * weather_factor
        return round(eta_hours, 1)

    # 3. Dynamic Rescheduling (Hava Təsirli)
    def dynamic_rail_sync(self, vessel_eta, weather):
        # Əgər fırtınadırsa, limanda boşaltma işləri də ləngiyir (+3 saat əlavə)
        buffer = 2.5
        if weather['is_stormy']:
            buffer += 3.0

        ready_time = datetime.now() + timedelta(hours=vessel_eta + buffer)
        return ready_time.strftime("%H:%M")

    def detect_anomaly(self, speed, lat, lon, weather):
        # Fırtınalı havada yüksək sürət böyük riskdir!
        if weather['is_stormy'] and speed > 15:
            return "⚠️ Təhlükəli Sürət (Fırtına)"
        if speed > 22:
            return "⚠️ Sürət Həddi Aşıldı"
        if 39.5 < lat < 40.5 and 49.5 < lon < 50.5: return "❗ Qapalı Zona Girişi"
        return "✅ Stabil"

    def get_satellite_feed(self):
        return {
            "source": "Azersky (Satellite)",
            "last_scan": datetime.now().strftime("%H:%M:%S"),
            "cloud_cover": f"{random.randint(5, 20)}%",
            "vessels_in_view": random.randint(40, 60)
        }

ai_core = AzconAIEngine()