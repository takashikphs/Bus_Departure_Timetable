# Bus Departure Timetable

A self-hosted real-time bus departure dashboard using the Dutch [OVapi](http://v0.ovapi.nl) public transport API.

<img width="1631" height="921" alt="2026-04-06 13_41_39-Desktop - File Explorer" src="https://github.com/user-attachments/assets/93594b1d-1c04-4bb0-b050-ccb657a0dfc2" />

Shows live departure times for two directions from your local stop, local weather, and a commute ETA calculator. Push notifications via [ntfy.sh](https://ntfy.sh) when a bus is close.

## Features

- Live departure times polled every 30 seconds
- Two-direction display (home stop ↔ city terminal)
- Local weather widget (Open-Meteo)
- Commute ETA calculator
- Push notifications via ntfy.sh
- Dark map background (Leaflet + CARTO)
- Mobile-friendly responsive layout

## Setup

### 1. Copy and fill in `.env`

```bash
cp .env.example .env
```

Edit `.env` with your own values:

| Variable | Description |
|---|---|
| `HOME_STOP_NAME` | Display name for your local stop |
| `CITY_STOP_NAME` | Display name for the city terminal |
| `TPC_HOME_OUTBOUND` | OVAPI timing-point code on the outbound route |
| `TPC_CITY_INBOUND` | OVAPI timing-point code at the city terminal |
| `ROUTES` | Comma-separated bus line numbers, e.g. `4,54` |
| `MAP_LAT` / `MAP_LON` | Coordinates for the map centre and weather |
| `CITY_TO_HOME_MIN` | Travel minutes from city TPC to your home stop |
| `PROXY_OFFSET_MIN` | Travel minutes from your home stop to outbound proxy TPC |
| `NTFY_TOPIC` | Your ntfy.sh topic (leave blank to disable notifications) |
| `COMMUTE_OVERHEAD_MIN` | Total overhead for commute ETA (transit + walking) |

**Finding your TPC codes:** look up your stop on [ovzoeker.nl](https://ovzoeker.nl) and use the timing-point code from the URL or stop details.

### 2. Run with Docker

```bash
docker compose up -d
```

Open [http://localhost:8888](http://localhost:8888).

### 3. Run locally (no Docker)

```bash
pip install -r requirements.txt
python app.py
```

## Push notifications

Install the [ntfy app](https://ntfy.sh), set `NTFY_TOPIC` to a unique topic name, and subscribe to it in the app. You'll get a high-priority alert when a bus is within `NOTIFY_AT_MIN` minutes.
