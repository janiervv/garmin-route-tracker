#!/usr/bin/env python3
"""Fetch all running activities from Garmin Connect and save routes as GeoJSON."""

import json
import os
import sys
from datetime import datetime
from getpass import getpass
from pathlib import Path

from garminconnect import Garmin, GarminConnectAuthenticationError
import fitdecode


TOKENSTORE = str(Path(__file__).parent / ".garmin_tokens")
OUTPUT_FILE = Path(__file__).parent / "routes.geojson"

# Garmin activity type for running
RUNNING_TYPE = "running"


def authenticate() -> Garmin:
    """Authenticate with Garmin Connect, reusing saved token store if available."""
    email = os.environ.get("GARMIN_EMAIL", "")
    password = os.environ.get("GARMIN_PASSWORD", "")

    if not email:
        email = input("Garmin Connect email: ")
    if not password:
        password = getpass("Garmin Connect password: ")

    client = Garmin(email, password, prompt_mfa=lambda: input("MFA code: "))

    # Try to resume saved token store
    if Path(TOKENSTORE).exists():
        try:
            client.login(TOKENSTORE)
            print("Resumed saved session.")
            return client
        except Exception:
            print("Saved session expired, logging in again...")

    try:
        client.login()
        client.garth.dump(TOKENSTORE)
    except GarminConnectAuthenticationError:
        print("Authentication failed. Check your email and password.")
        sys.exit(1)

    print("Logged in successfully.")
    return client


def fetch_all_running_activities(client: Garmin) -> list[dict]:
    """Fetch all running activities metadata."""
    activities = []
    start = 0
    batch_size = 100

    while True:
        batch = client.get_activities(
            start=start,
            limit=batch_size,
            activitytype=RUNNING_TYPE,
        )
        if not batch:
            break
        activities.extend(batch)
        print(f"  Fetched {len(activities)} activities so far...")
        start += batch_size

    print(f"Found {len(activities)} running activities total.")
    return activities


def extract_coords_from_fit(fit_bytes: bytes) -> list[list[float]]:
    """Extract GPS coordinates from FIT file bytes. Returns [[lon, lat], ...]."""
    coords = []
    with fitdecode.FitReader(fit_bytes) as reader:
        for frame in reader:
            if isinstance(frame, fitdecode.FitDataMessage) and frame.name == "record":
                try:
                    lat_field = frame.get_field("position_lat")
                    lon_field = frame.get_field("position_long")
                except KeyError:
                    continue
                if lat_field and lon_field and lat_field.value is not None and lon_field.value is not None:
                    # FIT uses semicircles, convert to degrees
                    lat = lat_field.value * (180 / 2**31)
                    lon = lon_field.value * (180 / 2**31)
                    coords.append([lon, lat])
    return coords


def activity_to_feature(client: Garmin, activity: dict) -> dict | None:
    """Convert a Garmin activity to a GeoJSON Feature with GPS track."""
    activity_id = activity["activityId"]
    name = activity.get("activityName", "Unnamed run")
    start_time = activity.get("startTimeLocal", "")
    distance_m = activity.get("distance", 0) or 0
    duration_s = activity.get("duration", 0) or 0
    avg_speed = activity.get("averageSpeed", 0) or 0  # m/s

    distance_km = distance_m / 1000
    duration_min = duration_s / 60

    # Calculate pace (min/km)
    pace_min_per_km = (duration_min / distance_km) if distance_km > 0 else 0
    pace_min = int(pace_min_per_km)
    pace_sec = int((pace_min_per_km - pace_min) * 60)
    pace_str = f"{pace_min}:{pace_sec:02d}"

    # Parse date for filtering
    date_str = ""
    if start_time:
        try:
            dt = datetime.fromisoformat(start_time)
            date_str = dt.strftime("%Y-%m-%d")
        except ValueError:
            date_str = start_time[:10]

    # Download GPS data (FIT file)
    try:
        fit_data = client.download_activity(activity_id, dl_fmt=client.ActivityDownloadFormat.ORIGINAL)
    except Exception as e:
        print(f"  Skipping {activity_id} ({name}): could not download GPS data — {e}")
        return None

    # FIT download may return a ZIP; handle both cases
    coords = []
    if fit_data[:2] == b"PK":
        import zipfile
        import io
        with zipfile.ZipFile(io.BytesIO(fit_data)) as zf:
            for zname in zf.namelist():
                if zname.endswith(".fit"):
                    coords = extract_coords_from_fit(zf.read(zname))
                    break
    else:
        coords = extract_coords_from_fit(fit_data)

    if len(coords) < 2:
        print(f"  Skipping {activity_id} ({name}): no GPS data")
        return None

    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": coords,
        },
        "properties": {
            "id": activity_id,
            "name": name,
            "date": date_str,
            "distance_km": round(distance_km, 2),
            "duration_min": round(duration_min, 1),
            "pace": pace_str,
            "avg_speed_kmh": round(avg_speed * 3.6, 1),
        },
    }


def load_existing_features() -> list[dict]:
    """Load previously saved features from routes.geojson."""
    if not OUTPUT_FILE.exists():
        return []
    try:
        with open(OUTPUT_FILE) as f:
            data = json.load(f)
        return data.get("features", [])
    except (json.JSONDecodeError, KeyError):
        return []


def main():
    print("=== Garmin Running Routes Fetcher ===\n")

    client = authenticate()

    # Load existing routes to avoid re-downloading
    existing_features = load_existing_features()
    existing_ids = {f["properties"]["id"] for f in existing_features}
    if existing_features:
        print(f"\nFound {len(existing_features)} existing routes locally.")

    print("\nFetching running activities...")
    activities = fetch_all_running_activities(client)

    if not activities:
        print("No running activities found.")
        return

    # Filter to only new activities
    new_activities = [a for a in activities if a["activityId"] not in existing_ids]

    if not new_activities:
        print("No new activities to download. Already up to date.")
        return

    print(f"\nDownloading {len(new_activities)} new GPS tracks (skipping {len(activities) - len(new_activities)} already saved)...")
    new_features = []
    for i, activity in enumerate(new_activities, 1):
        name = activity.get("activityName", "Unnamed")
        print(f"  [{i}/{len(new_activities)}] {name}")
        feature = activity_to_feature(client, activity)
        if feature:
            new_features.append(feature)

    all_features = existing_features + new_features

    geojson = {
        "type": "FeatureCollection",
        "features": all_features,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(geojson, f)

    print(f"\nDone! Added {len(new_features)} new routes. Total: {len(all_features)} routes in {OUTPUT_FILE}")
    print(f"\nTo view the map, run:")
    print(f"  python -m http.server 8000")
    print(f"Then open http://localhost:8000")


if __name__ == "__main__":
    main()
