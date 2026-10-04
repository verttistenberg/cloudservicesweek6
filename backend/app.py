from flask import Flask, jsonify, request, g
import json
import logging
import os
import sys
import time
import requests
import mysql.connector
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET

app = Flask(__name__)

DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_USER = os.getenv('DB_USER', 'appuser')
DB_PASSWORD = os.getenv('DB_PASSWORD', 'changeme')
DB_NAME = os.getenv('DB_NAME', 'appdb')
WEATHER_URL = os.getenv('WEATHER_API_URL', 'https://api.open-meteo.com/v1/forecast')

def get_connection(timeout=None):
    return mysql.connector.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        connection_timeout=timeout
    )


def init_db():
    for attempt in range(10):
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS visits (
                    id INT PRIMARY KEY,
                    count INT NOT NULL DEFAULT 0
                )
            """)
            cur.execute("INSERT IGNORE INTO visits (id, count) VALUES (1, 0)")
            conn.commit()
            cur.close()
            conn.close()
            return
        except mysql.connector.Error:
            time.sleep(3)


init_db()


req_logger = logging.getLogger('requests')
req_logger.setLevel(logging.INFO)
req_logger.propagate = False
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter('%(message)s'))
req_logger.addHandler(_handler)

QUIET_PATHS = {'/health', '/healthz'}


@app.before_request
def start_timer():
    g.start = time.perf_counter()


@app.after_request
def log_request(response):
    if request.path not in QUIET_PATHS:
        req_logger.info(json.dumps({
            'time': datetime.now(timezone.utc).isoformat(),
            'method': request.method,
            'path': request.path,
            'status': response.status_code,
            'duration_ms': round((time.perf_counter() - g.start) * 1000, 1),
        }))
    return response


@app.get('/health')
def health():
    return jsonify({'status': 'ok'})


@app.get('/healthz')
def healthz():
    try:
        conn = get_connection(timeout=2)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
        conn.close()
        return jsonify({'status': 'ok', 'database': 'up'})
    except mysql.connector.Error as e:
        app.logger.error('healthz: database check failed: %s', e)
        return jsonify({'status': 'unavailable', 'database': 'down'}), 503


@app.get('/')
def index():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("UPDATE visits SET count = count + 1 WHERE id = 1")
    conn.commit()

    cur.execute("SELECT count FROM visits WHERE id = 1")
    visits = cur.fetchone()[0]

    cur.execute("SELECT NOW()")
    server_time = cur.fetchone()[0]

    cur.close()
    conn.close()

    return jsonify({
        'message': f'hello from mysql via flask, server time is {server_time}',
        'visits': visits
    })

@app.get('/weather')
def weather():
    try:
        lat = float(request.args.get('lat', '60.17'))
        lon = float(request.args.get('lon', '24.94'))
    except ValueError:
        return jsonify({'error': 'lat and lon must be numbers'}), 400

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify({'error': 'lat/lon out of range'}), 400

    try:
        r = requests.get(
            WEATHER_URL,
            params={
                'latitude': lat,
                'longitude': lon,
                'current': 'temperature_2m,wind_speed_10m'
            },
            timeout=5
        )
        r.raise_for_status()
    except requests.RequestException as e:
        app.logger.error('Upstream API failed: %s', e)
        return jsonify({'error': 'Upstream service unavailable.'}), 502

    current = r.json()['current']
    return jsonify({
        'temperature': current['temperature_2m'],
        'wind_speed': current['wind_speed_10m']
    })

FMI_URL = os.getenv('FMI_API_URL', 'https://opendata.fmi.fi/wfs')
NS = {'BsWfs': 'http://xml.fmi.fi/schema/wfs/2.0'}

@app.get('/oulu')
def oulu_weather():
    start = (datetime.now(timezone.utc) - timedelta(minutes=60)).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        r = requests.get(
            FMI_URL,
            params={
                'service': 'WFS',
                'version': '2.0.0',
                'request': 'getFeature',
                'storedquery_id': 'fmi::observations::weather::simple',
                'place': 'Oulunsalo',
                'parameters': 't2m,ws_10min',
                'starttime': start,
            },
            timeout=5
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError) as e:
        app.logger.error('FMI request failed: %s', e)
        return jsonify({'error': 'Upstream service unavailable.'}), 502

    latest = {}
    for el in root.findall('.//BsWfs:BsWfsElement', NS):
        name = el.find('BsWfs:ParameterName', NS).text
        value = el.find('BsWfs:ParameterValue', NS).text
        if value and value != 'NaN':
            latest[name] = float(value)

    if not latest:
        return jsonify({'error': 'No recent observations.'}), 502

    return jsonify({
        'station': 'Oulunsalo',
        'temperature': latest.get('t2m'),
        'wind_speed': latest.get('ws_10min')
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)