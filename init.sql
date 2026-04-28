-- Gəmilər cədvəli (PostGIS-siz, sadə FLOAT koordinatlar)
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

-- Liman hadisələri
CREATE TABLE IF NOT EXISTS port_events (
    id SERIAL PRIMARY KEY,
    vessel_mmsi VARCHAR(9) REFERENCES vessels(mmsi),
    event_type VARCHAR(30),
    port VARCHAR(50),
    cargo_tons FLOAT,
    wagons_needed INT,
    occurred_at TIMESTAMPTZ DEFAULT NOW()
);

-- Anomaliya alertləri
CREATE TABLE IF NOT EXISTS alerts (
    id SERIAL PRIMARY KEY,
    vessel_mmsi VARCHAR(9),
    alert_type VARCHAR(50),
    severity VARCHAR(10),
    message TEXT,
    alert_lon FLOAT,
    alert_lat FLOAT,
    resolved BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS alerts_created_idx ON alerts (created_at DESC);

-- Demo data — ASCO gəmiləri
INSERT INTO vessels (mmsi, name, vessel_type, flag, length_m, width_m, lon, lat, speed_knots, course_deg, cargo_tons, destination, eta, eta_confidence)
VALUES
    ('423001001', 'BAKU STAR',     'cargo',  'AZ', 120, 18, 49.8, 40.4, 8.2,  185, 450, 'Bakı Limanı', NOW() + INTERVAL '4 hours',  0.87),
    ('423001002', 'XƏZƏR QIZILI', 'tanker', 'AZ', 145, 22, 51.2, 41.8, 6.5,  220, 820, 'Bakı Limanı', NOW() + INTERVAL '7 hours',  0.72),
    ('423001003', 'ABŞERON',       'ferry',  'AZ',  95, 16, 50.1, 43.2, 11.0, 195, 180, 'Aktau',       NOW() + INTERVAL '11 hours', 0.91),
    ('436001001', 'AKTAU EXPRESS', 'cargo',  'KZ', 110, 17, 51.8, 44.1, 7.8,  170, 560, 'Bakı Limanı', NOW() + INTERVAL '9 hours',  0.68),
    ('436001002', 'MANGISTAU',     'tanker', 'KZ', 138, 21, 52.4, 42.5, 5.2,  210, 910, 'Bakı Limanı', NOW() + INTERVAL '14 hours', 0.55),
    ('438001001', 'TURKMENBASHI',  'cargo',  'TM', 100, 16, 53.0, 40.0, 9.1,  270, 320, 'Türkmənbaşı', NOW() + INTERVAL '6 hours',  0.83)
ON CONFLICT (mmsi) DO NOTHING;

-- Demo alert
INSERT INTO alerts (vessel_mmsi, alert_type, severity, message, alert_lon, alert_lat)
VALUES (
    '436001002',
    'long_wait',
    'medium',
    'MANGISTAU gəmisi 3 saatdır hərəkətsizdir — texniki problem şübhəsi',
    52.4, 42.5
) ON CONFLICT DO NOTHING;