"""
Azcon-Caspian — AI Engine
Dynamic Rescheduling · Azersky Satellite Feed · Anomaly Detection · ETA Forecast
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from typing import Optional


# ---------------------------------------------------------------------------
# Sabitlər
# ---------------------------------------------------------------------------
BAKU_PORT = (49.865, 40.342)          # (lon, lat)
CASPIAN_ZONES = {
    "restricted": [(50.5, 41.5), (51.0, 42.0)],   # qapalı zona
    "shallow":    [(53.5, 40.8), (54.0, 41.2)],   # dayaz su
    "storm_belt": [(50.0, 43.0), (52.0, 45.0)],   # tipik fırtına qurşağı
}
MAX_SAFE_SPEED_STORM = 12.0   # fırtınada maksimum təhlükəsiz sürət (knot)
WAGON_TONS = 80               # bir vaqon üçün orta yük (ton)


# ---------------------------------------------------------------------------
# Yardımçı
# ---------------------------------------------------------------------------
def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    dp = math.radians(lat2 - lat1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _point_in_zone(lon: float, lat: float, zone_corners: list[tuple]) -> bool:
    """Sadə BBOX yoxlaması (2 corner: min, max)."""
    (lon1, lat1), (lon2, lat2) = zone_corners
    return min(lon1, lon2) <= lon <= max(lon1, lon2) and \
           min(lat1, lat2) <= lat <= max(lat1, lat2)


# ---------------------------------------------------------------------------
# Ana sinif
# ---------------------------------------------------------------------------
class AzconAIEngine:

    def __init__(self):
        self.port_coords = BAKU_PORT

    # ------------------------------------------------------------------
    # 1 · Hava Şəraiti (MeteoAz / OpenWeather stub)
    # ------------------------------------------------------------------
    def get_current_weather(self, lat: float = 40.5, lon: float = 50.5) -> dict:
        """
        Real sistemdə MeteoAz API-ya qoşulur.
        Demo üçün Xəzərin real seasonal amplitudu ilə simulyasiya edilir.
        """
        wind_speed = random.randint(4, 28)
        wave_height = round(random.uniform(0.3, 3.8), 1)
        visibility_km = random.randint(2, 20)
        is_stormy = wind_speed > 18 or wave_height > 2.5

        severity = "NORMAL"
        if wind_speed > 22 or wave_height > 3.0:
            severity = "KRİTİK"
        elif is_stormy:
            severity = "XƏBƏRDARLIQ"

        return {
            "wind_speed_ms": wind_speed,
            "wave_height_m": wave_height,
            "visibility_km": visibility_km,
            "is_stormy": is_stormy,
            "severity": severity,
            "status": f"{'🔴 FIRTINA — ' if severity == 'KRİTİK' else '🟡 ' if is_stormy else '🟢 '}{severity}",
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }

    # ------------------------------------------------------------------
    # 2 · ETA Proqnozu (Hava + Yük + Məsafə)
    # ------------------------------------------------------------------
    def calculate_eta(
        self,
        lon: float,
        lat: float,
        speed_knots: float,
        cargo_tons: float = 0.0,
        weather: Optional[dict] = None,
    ) -> dict:
        """
        Çox faktorlu ETA modeli:
          - Haversine məsafə (km → nautical mile)
          - Yük ağırlığı sürəti azaldır
          - Hava faktoru (fırtına → +20-40% vaxt)
          - Confidence: məsafə + sürət sabitliyinə görə
        """
        if weather is None:
            weather = self.get_current_weather(lat, lon)

        dist_km = _haversine_km(lon, lat, *BAKU_PORT)
        dist_nm = dist_km / 1.852

        # Hava faktoru
        weather_factor = 1.0
        if weather["is_stormy"]:
            weather_factor = 1.35 if weather["severity"] == "KRİTİK" else 1.20

        # Yük ağırlığı effekti (hər 100 ton → 0.05 knot azalma)
        cargo_penalty = (cargo_tons / 100) * 0.05
        effective_speed = max(2.0, (speed_knots - cargo_penalty) * (1 / weather_factor))

        hours = dist_nm / effective_speed if effective_speed > 0 else 999.0
        eta_dt = datetime.utcnow() + timedelta(hours=hours)

        # Confidence hesabı
        conf_base = 0.97
        conf_dist = (dist_km / 1500) * 0.35   # uzaq olarsa qeyri-müəyyənlik artar
        conf_weather = 0.12 if weather["is_stormy"] else 0.0
        confidence = round(max(0.38, min(0.97, conf_base - conf_dist - conf_weather)), 2)

        return {
            "eta_utc": eta_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "eta_display": eta_dt.strftime("%d %b %H:%M"),
            "hours_remaining": round(hours, 1),
            "distance_km": round(dist_km, 1),
            "distance_nm": round(dist_nm, 1),
            "effective_speed_knots": round(effective_speed, 1),
            "weather_factor": round(weather_factor, 2),
            "cargo_penalty_knots": round(cargo_penalty, 2),
            "confidence": confidence,
            "confidence_pct": f"{int(confidence * 100)}%",
        }

    # ------------------------------------------------------------------
    # 3 · Anomaliya Aşkarlanması
    # ------------------------------------------------------------------
    def detect_anomaly(
        self,
        mmsi: str,
        lon: float,
        lat: float,
        speed_knots: float,
        course_deg: float,
        status: str,
        cargo_tons: float,
        weather: Optional[dict] = None,
        history: Optional[list[dict]] = None,
    ) -> dict:
        """
        Çox kriteriyalı anomaliya detektoru.
        Qaytarır: {anomaly: bool, severity, type, message, action}
        """
        if weather is None:
            weather = self.get_current_weather(lat, lon)

        anomalies = []

        # A) Sürət anomaliyaları
        if weather["is_stormy"] and speed_knots > MAX_SAFE_SPEED_STORM:
            anomalies.append({
                "type": "STORM_OVERSPEED",
                "severity": "HIGH",
                "message": f"Fırtınada {speed_knots:.1f} kt — təhlükəli sürət (limit: {MAX_SAFE_SPEED_STORM} kt)",
                "action": "Sürəti dərhal azaldın, liman xəbərdar edilsin",
            })
        if speed_knots > 25:
            anomalies.append({
                "type": "OVERSPEED",
                "severity": "MEDIUM",
                "message": f"Sürət həddi aşıldı: {speed_knots:.1f} kt",
                "action": "Gəmi kapitanı ilə əlaqə saxla",
            })

        # B) Coğrafi anomaliyalar
        for zone_name, corners in CASPIAN_ZONES.items():
            if _point_in_zone(lon, lat, corners):
                sev = "HIGH" if zone_name == "restricted" else "MEDIUM"
                anomalies.append({
                    "type": f"ZONE_{zone_name.upper()}",
                    "severity": sev,
                    "message": f"Gəmi '{zone_name}' zonasındadır",
                    "action": "Marşrutu dəyişdir, səlahiyyətlilərə məlumat ver",
                })

        # C) Dark vessel (status=dark)
        if status == "dark":
            anomalies.append({
                "type": "DARK_VESSEL",
                "severity": "HIGH",
                "message": "AIS siqnalı kəsildi — dark vessel şübhəsi",
                "action": "ASCO Nəzarət Mərkəzini dərhal xəbərdar et",
            })

        # D) Hərəkətsizlik (speed < 0.5 və status aktiv)
        if speed_knots < 0.5 and status == "active":
            anomalies.append({
                "type": "STATIONARY",
                "severity": "MEDIUM",
                "message": "Gəmi hərəkətsizdir, texniki problem şübhəsi",
                "action": "Gəmi ilə radio əlaqəsi qurun",
            })

        # E) Kurs anomaliyası (history varsa)
        if history and len(history) >= 2:
            expected_course = history[-1].get("course_deg", course_deg)
            deviation = abs(course_deg - expected_course)
            if deviation > 45 and deviation < 315:   # 315 = 360-45
                anomalies.append({
                    "type": "COURSE_DEVIATION",
                    "severity": "LOW",
                    "message": f"Kurs sapması: {deviation:.0f}° (gözlənilən: {expected_course:.0f}°)",
                    "action": "Marşrut planı ilə müqayisə et",
                })

        # Ən yüksək severity-ni seç
        if not anomalies:
            return {
                "anomaly": False,
                "severity": "NONE",
                "type": "OK",
                "message": "✅ Stabil — heç bir anomaliya yoxdur",
                "action": None,
                "details": [],
            }

        sev_order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
        top = max(anomalies, key=lambda x: sev_order.get(x["severity"], 0))
        return {
            "anomaly": True,
            "severity": top["severity"],
            "type": top["type"],
            "message": top["message"],
            "action": top["action"],
            "details": anomalies,
            "count": len(anomalies),
        }

    # ------------------------------------------------------------------
    # 4 · Azersky Peyk Məlumatı
    # ------------------------------------------------------------------
    def get_satellite_feed(self) -> dict:
        """
        Azersky-1 (AzerSpace-1/2) peyk kanalından gəlir.
        Real sistemdə AzerCosmos API-ya qoşulur.
        Demo: realistic stub.
        """
        cloud_cover = random.randint(5, 35)
        vessels_in_view = random.randint(38, 65)
        resolution_m = 5.0   # AzerSpace-2 optik rezolüsiya (m)
        swath_km = 640        # AzerSpace-2 görüş genişliyi (km)
        orbit_altitude_km = 35786   # GEO orbit

        # SAR (Synthetic Aperture Radar) — bulud altından görür
        sar_active = cloud_cover > 20
        scan_quality = "ƏMSAL" if cloud_cover < 15 else ("ORTA" if cloud_cover < 28 else "SAR_MOD")

        return {
            "source": "Azersky (AzerSpace-2 GEO)",
            "satellite_id": "AZERSPACE-2",
            "orbit_type": "Geostationar (GEO)",
            "altitude_km": orbit_altitude_km,
            "resolution_m": resolution_m,
            "swath_km": swath_km,
            "coverage_area": "Xəzər dənizi + Qafqaz",
            "last_scan": datetime.utcnow().strftime("%H:%M:%S UTC"),
            "next_scan_min": random.randint(8, 15),
            "cloud_cover_pct": cloud_cover,
            "scan_quality": scan_quality,
            "sar_mode_active": sar_active,
            "vessels_detected": vessels_in_view,
            "dark_vessels_flagged": random.randint(0, 3),
            "uplink_status": "AKTIV ✅",
            "signal_strength_db": round(random.uniform(-92.0, -78.0), 1),
        }

    # ------------------------------------------------------------------
    # 5 · Dynamic Rescheduling (Dəmiryol + Liman Sinxronizasiyası)
    # ------------------------------------------------------------------
    def dynamic_rail_sync(
        self,
        vessels: list[dict],
        weather: Optional[dict] = None,
    ) -> dict:
        """
        Gəmilərin ETA-larını real hava şəraitinə görə yenidən hesablayır,
        liman boşaltma sırası və vaqon tələbatını planlaşdırır.

        vessels: [{mmsi, name, lon, lat, speed_knots, cargo_tons, eta_hours}]
        """
        if weather is None:
            weather = self.get_current_weather()

        schedule = []
        total_cargo = 0.0
        total_wagons = 0
        base_buffer_h = 2.5   # liman proseduru buferi
        storm_buffer_h = 3.0  # fırtına əlavə buferi

        buffer = base_buffer_h + (storm_buffer_h if weather["is_stormy"] else 0.0)

        for v in vessels:
            eta_info = self.calculate_eta(
                v["lon"], v["lat"],
                v["speed_knots"], v.get("cargo_tons", 0.0), weather
            )
            hours = eta_info["hours_remaining"] + buffer
            arrival_dt = datetime.utcnow() + timedelta(hours=hours)
            unload_end_dt = arrival_dt + timedelta(hours=max(1.0, v.get("cargo_tons", 0) / 200))

            wagons = math.ceil(v.get("cargo_tons", 0) / WAGON_TONS)
            total_cargo += v.get("cargo_tons", 0)
            total_wagons += wagons

            anom = self.detect_anomaly(
                v.get("mmsi", ""),
                v["lon"], v["lat"],
                v["speed_knots"], v.get("course_deg", 0),
                v.get("status", "active"), v.get("cargo_tons", 0),
                weather,
            )

            schedule.append({
                "mmsi": v.get("mmsi"),
                "name": v.get("name", "N/A"),
                "cargo_tons": v.get("cargo_tons", 0),
                "wagons_needed": wagons,
                "eta_adjusted": arrival_dt.strftime("%d %b %H:%M UTC"),
                "eta_hours": round(hours, 1),
                "unload_end": unload_end_dt.strftime("%d %b %H:%M UTC"),
                "confidence": eta_info["confidence_pct"],
                "anomaly": anom["anomaly"],
                "anomaly_msg": anom["message"] if anom["anomaly"] else None,
                "priority": "HIGH" if anom["severity"] == "HIGH" else
                            ("MEDIUM" if anom["severity"] == "MEDIUM" else "NORMAL"),
            })

        # ETA-ya görə sırala
        schedule.sort(key=lambda x: x["eta_hours"])

        # Kumulyativ vaqon cədvəli (növbə planlaması)
        cumulative_wagons = 0
        for s in schedule:
            cumulative_wagons += s["wagons_needed"]
            s["wagons_cumulative"] = cumulative_wagons

        return {
            "weather": weather,
            "storm_buffer_applied": weather["is_stormy"],
            "buffer_hours": buffer,
            "total_vessels": len(schedule),
            "total_cargo_tons": round(total_cargo, 1),
            "total_wagons": total_wagons,
            "schedule": schedule,
            "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
ai_core = AzconAIEngine()