"""An IoT fleet: devices, sensors, readings at three grains, alarms, firmware.

Telemetry schemas break selection in their own way. The same measurement
exists as raw readings, one-minute rollups and hourly rollups, and the right
one depends on the question's time horizon. Alarms are raised, acknowledged
and cleared in three different tables. Devices belong to sites through a
history table because they get moved. And there is a partition table per
month, which a naive catalogue treats as twelve different tables.

  * readings at three grains: tel_reading_raw, tel_reading_1m, tel_reading_1h
  * monthly partition tables tel_reading_raw_2025_01 .. _2025_06, which are
    the same table six times
  * alarm lifecycle across tel_alarm_raised, tel_alarm_ack, tel_alarm_cleared
  * device placement as a history (dev_device_site_history) and a current
    view
  * firmware rollout tracking, calibration records, maintenance windows
  * an audit table of who changed device config, restricted

Synthetic and invented. Not derived from any real system.
"""
from __future__ import annotations

DDL = """
-- ============ assets ============

CREATE TABLE dev_site (
  site_id INTEGER PRIMARY KEY, site_code TEXT, name TEXT, region TEXT,
  timezone TEXT, latitude REAL, longitude REAL);

CREATE TABLE dev_device_model (
  model_id INTEGER PRIMARY KEY, manufacturer TEXT, model_name TEXT,
  protocol TEXT, sample_rate_hz REAL);

CREATE TABLE dev_device (
  device_id INTEGER PRIMARY KEY, serial_number TEXT,
  model_id INTEGER REFERENCES dev_device_model(model_id),
  commissioned_on TEXT, status TEXT, firmware_version TEXT, last_seen_at TEXT);

CREATE TABLE dev_device_site_history (
  history_id INTEGER PRIMARY KEY,
  device_id INTEGER REFERENCES dev_device(device_id),
  site_id INTEGER REFERENCES dev_site(site_id),
  installed_at TEXT, removed_at TEXT);

CREATE TABLE dev_sensor (
  sensor_id INTEGER PRIMARY KEY,
  device_id INTEGER REFERENCES dev_device(device_id),
  channel INTEGER, measurement TEXT, unit TEXT, min_valid REAL, max_valid REAL);

CREATE TABLE dev_gateway (
  gateway_id INTEGER PRIMARY KEY, gateway_code TEXT,
  site_id INTEGER REFERENCES dev_site(site_id),
  ip_address TEXT, uplink TEXT, status TEXT);

CREATE TABLE dev_device_gateway (
  device_id INTEGER REFERENCES dev_device(device_id),
  gateway_id INTEGER REFERENCES dev_gateway(gateway_id),
  rssi_dbm REAL, PRIMARY KEY (device_id, gateway_id));

-- ============ readings, three grains ============

CREATE TABLE tel_reading_raw (
  reading_id INTEGER PRIMARY KEY,
  sensor_id INTEGER REFERENCES dev_sensor(sensor_id),
  ts TEXT, value REAL, quality INTEGER);

CREATE TABLE tel_reading_1m (
  bucket_id INTEGER PRIMARY KEY,
  sensor_id INTEGER REFERENCES dev_sensor(sensor_id),
  bucket_start TEXT, avg_value REAL, min_value REAL, max_value REAL,
  sample_count INTEGER);

CREATE TABLE tel_reading_1h (
  bucket_id INTEGER PRIMARY KEY,
  sensor_id INTEGER REFERENCES dev_sensor(sensor_id),
  bucket_start TEXT, avg_value REAL, min_value REAL, max_value REAL,
  p95_value REAL, sample_count INTEGER);

CREATE TABLE tel_reading_raw_2025_01 (reading_id INTEGER PRIMARY KEY, sensor_id INTEGER, ts TEXT, value REAL, quality INTEGER);
CREATE TABLE tel_reading_raw_2025_02 (reading_id INTEGER PRIMARY KEY, sensor_id INTEGER, ts TEXT, value REAL, quality INTEGER);
CREATE TABLE tel_reading_raw_2025_03 (reading_id INTEGER PRIMARY KEY, sensor_id INTEGER, ts TEXT, value REAL, quality INTEGER);
CREATE TABLE tel_reading_raw_2025_04 (reading_id INTEGER PRIMARY KEY, sensor_id INTEGER, ts TEXT, value REAL, quality INTEGER);
CREATE TABLE tel_reading_raw_2025_05 (reading_id INTEGER PRIMARY KEY, sensor_id INTEGER, ts TEXT, value REAL, quality INTEGER);
CREATE TABLE tel_reading_raw_2025_06 (reading_id INTEGER PRIMARY KEY, sensor_id INTEGER, ts TEXT, value REAL, quality INTEGER);

CREATE TABLE tel_heartbeat (
  heartbeat_id INTEGER PRIMARY KEY,
  device_id INTEGER REFERENCES dev_device(device_id),
  ts TEXT, uptime_seconds INTEGER, battery_pct REAL, signal_dbm REAL);

-- ============ alarm lifecycle ============

CREATE TABLE tel_threshold (
  threshold_id INTEGER PRIMARY KEY,
  sensor_id INTEGER REFERENCES dev_sensor(sensor_id),
  severity TEXT, lower_bound REAL, upper_bound REAL, hysteresis REAL,
  is_active INTEGER);

CREATE TABLE tel_alarm_raised (
  alarm_id INTEGER PRIMARY KEY,
  sensor_id INTEGER REFERENCES dev_sensor(sensor_id),
  threshold_id INTEGER REFERENCES tel_threshold(threshold_id),
  raised_at TEXT, severity TEXT, trigger_value REAL);

CREATE TABLE tel_alarm_ack (
  ack_id INTEGER PRIMARY KEY,
  alarm_id INTEGER REFERENCES tel_alarm_raised(alarm_id),
  acked_at TEXT, acked_by TEXT, note TEXT);

CREATE TABLE tel_alarm_cleared (
  clear_id INTEGER PRIMARY KEY,
  alarm_id INTEGER REFERENCES tel_alarm_raised(alarm_id),
  cleared_at TEXT, cleared_by TEXT, resolution TEXT);

-- ============ operations ============

CREATE TABLE ops_firmware_build (
  build_id INTEGER PRIMARY KEY, version TEXT, released_on TEXT,
  model_id INTEGER REFERENCES dev_device_model(model_id), changelog TEXT);

CREATE TABLE ops_firmware_rollout (
  rollout_id INTEGER PRIMARY KEY,
  build_id INTEGER REFERENCES ops_firmware_build(build_id),
  device_id INTEGER REFERENCES dev_device(device_id),
  scheduled_at TEXT, applied_at TEXT, status TEXT, error TEXT);

CREATE TABLE ops_calibration (
  calibration_id INTEGER PRIMARY KEY,
  sensor_id INTEGER REFERENCES dev_sensor(sensor_id),
  performed_on TEXT, technician TEXT, offset_value REAL, gain REAL,
  next_due_on TEXT);

CREATE TABLE ops_maintenance_window (
  window_id INTEGER PRIMARY KEY,
  site_id INTEGER REFERENCES dev_site(site_id),
  starts_at TEXT, ends_at TEXT, reason TEXT, suppress_alarms INTEGER);

CREATE TABLE ops_work_order (
  work_order_id INTEGER PRIMARY KEY, work_order_number TEXT,
  device_id INTEGER REFERENCES dev_device(device_id),
  opened_on TEXT, closed_on TEXT, category TEXT, technician TEXT, status TEXT);

CREATE TABLE ops_config_audit (
  audit_id INTEGER PRIMARY KEY,
  device_id INTEGER REFERENCES dev_device(device_id),
  changed_at TEXT, changed_by TEXT, parameter TEXT, old_value TEXT,
  new_value TEXT);

-- ============ the accumulated junk ============

CREATE TABLE tel_reading_1h_bkp (
  bucket_id INTEGER PRIMARY KEY, sensor_id INTEGER, bucket_start TEXT,
  avg_value REAL);

CREATE TABLE tel_alarm_raised_old (
  alarm_id INTEGER PRIMARY KEY, sensor_id INTEGER, raised_at TEXT,
  severity TEXT);

CREATE TABLE dev_device_v2 (
  device_id INTEGER PRIMARY KEY, serial_number TEXT, model_id INTEGER,
  status TEXT, migration_batch TEXT);

CREATE TABLE stg_reading (
  serial_number TEXT, channel INTEGER, ts TEXT, value REAL, load_batch TEXT);

CREATE TABLE stg_device (
  serial_number TEXT, model_name TEXT, site_code TEXT, load_batch TEXT);

-- ============ views ============

CREATE VIEW v_device_current_site AS
  SELECT h.device_id, h.site_id, s.name AS site_name, h.installed_at
  FROM dev_device_site_history h JOIN dev_site s ON s.site_id = h.site_id
  WHERE h.removed_at IS NULL;

CREATE VIEW v_open_alarms AS
  SELECT r.alarm_id, r.sensor_id, r.severity, r.raised_at, r.trigger_value
  FROM tel_alarm_raised r
  LEFT JOIN tel_alarm_cleared c ON c.alarm_id = r.alarm_id
  WHERE c.clear_id IS NULL;

CREATE VIEW v_alarm_time_to_ack AS
  SELECT r.alarm_id, r.severity,
         julianday(a.acked_at) - julianday(r.raised_at) AS days_to_ack
  FROM tel_alarm_raised r JOIN tel_alarm_ack a ON a.alarm_id = r.alarm_id;

CREATE VIEW v_silent_devices AS
  SELECT d.device_id, d.serial_number, d.last_seen_at
  FROM dev_device d
  WHERE d.status = 'ACTIVE' AND julianday('now') - julianday(d.last_seen_at) > 1;

CREATE VIEW v_low_battery AS
  SELECT h.device_id, MIN(h.battery_pct) AS battery_pct
  FROM tel_heartbeat h GROUP BY h.device_id HAVING MIN(h.battery_pct) < 20;

CREATE VIEW v_firmware_drift AS
  SELECT d.device_id, d.firmware_version AS running, b.version AS latest
  FROM dev_device d
  JOIN ops_firmware_build b ON b.model_id = d.model_id
  WHERE d.firmware_version <> b.version;

CREATE VIEW v_calibration_overdue AS
  SELECT c.sensor_id, MAX(c.next_due_on) AS next_due_on
  FROM ops_calibration c GROUP BY c.sensor_id
  HAVING MAX(c.next_due_on) < date('now');

CREATE VIEW v_hourly_site_average AS
  SELECT cs.site_id, r.bucket_start, AVG(r.avg_value) AS site_avg
  FROM tel_reading_1h r
  JOIN dev_sensor s ON s.sensor_id = r.sensor_id
  JOIN v_device_current_site cs ON cs.device_id = s.device_id
  GROUP BY cs.site_id, r.bucket_start;
"""

HINTS = {
    "tel_reading_raw": "every sample as received; huge; use only for sub-minute questions",
    "tel_reading_1m": "one-minute rollups; the default for anything within a day",
    "tel_reading_1h": "hourly rollups with p95; for trends over days or longer",
    "tel_alarm_raised": "when an alarm fired; join tel_alarm_ack and tel_alarm_cleared for the lifecycle",
    "dev_device_site_history": "where each device has been installed over time; v_device_current_site for now",
    "tel_heartbeat": "device liveness: uptime, battery and signal per check-in",
}

GOLDEN = [
    # grain by horizon
    ("every raw sample received in the last thirty seconds", {"tel_reading_raw"}),
    ("minute by minute readings for today",           {"tel_reading_1m"}),
    ("hourly p95 over the last month",                {"tel_reading_1h"}),
    ("average per site per hour",                     {"v_hourly_site_average"}),
    # alarm lifecycle
    ("alarms that have not been cleared",             {"v_open_alarms"}),
    ("how long alarms take to be acknowledged",       {"v_alarm_time_to_ack"}),
    ("alarm thresholds per sensor",                   {"tel_threshold"}),
    # placement
    ("which site each device is at right now",        {"v_device_current_site"}),
    ("where a device was installed last year",        {"dev_device_site_history"}),
    # health
    ("devices that stopped reporting",                {"v_silent_devices"}),
    ("devices with low battery",                      {"v_low_battery"}),
    ("devices running old firmware",                  {"v_firmware_drift"}),
    ("sensors overdue for calibration",               {"v_calibration_overdue"}),
    # ops
    ("firmware rollout status and errors",            {"ops_firmware_rollout"}),
    ("open work orders by technician",                {"ops_work_order"}),
    ("gateway signal strength per device",            {"dev_device_gateway"}),
]

DECOYS = [
    ("hourly p95 over the last month",         "tel_reading_1h",   "tel_reading_1h_bkp"),
    ("alarms raised with severity",            "tel_alarm_raised", "tel_alarm_raised_old"),
    ("device serial number and status",        "dev_device",       "dev_device_v2"),
    ("device serial number and status",        "dev_device",       "stg_device"),
    ("every raw sample received in the last thirty seconds", "tel_reading_raw", "stg_reading"),
]

#: The monthly partitions are six copies of one table. They are not shadows
#: by suffix rule (no known suffix), so this is where ``exclude`` earns its
#: keep -- and the test checks that the base table still wins without it.
PARTITIONS = [f"tel_reading_raw_2025_{m:02d}" for m in range(1, 7)]

GOLDEN_NEEDS_DESCRIPTIONS = [
    ("things that are beeping and nobody has dealt with", {"v_open_alarms"}),
    ("which boxes have gone quiet",                       {"v_silent_devices"}),
    ("what needs a technician visit soon",                {"v_calibration_overdue", "ops_work_order"}),
]

RESTRICTED = {
    "ops_config_audit": ["admin"],
}
