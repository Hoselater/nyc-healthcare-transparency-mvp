"""The NYC Traffic Management Centre camera inventory.

``https://webcams.nyctmc.org/api/cameras`` returns the full public camera list
as JSON: several hundred fixed cameras, each with a point location, an online
flag, and a still-image URL. The stills are JPEGs refreshed every few seconds;
there is no history and no archive, so a still is only ever evidence about the
moment it was pulled.

Cameras produce no measurements. Their value here is corroboration: when a
speed link reports 6 mph, the nearest camera says whether that is a jam, a
closed lane, or a sensor talking nonsense.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import requests

from etl.nycdot import geo
from etl.nycdot.fetch import FeedUnavailable, build_session, first_present, get, get_json

log = logging.getLogger(__name__)

CAMERA_LIST_URL = "https://webcams.nyctmc.org/api/cameras"
CAMERA_IMAGE_URL_TEMPLATE = "https://webcams.nyctmc.org/api/cameras/{camera_id}/image"

FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "camera_id": ("id", "cameraId", "camera_id", "uuid"),
    "name": ("name", "cameraName", "title", "location"),
    "latitude": ("latitude", "lat"),
    "longitude": ("longitude", "lng", "lon", "long"),
    "area": ("area", "borough", "region", "roadway"),
    "is_online": ("isOnline", "online", "status", "active"),
    "image_url": ("imageUrl", "image_url", "imageURL", "url"),
    "video_url": ("videoUrl", "video_url", "videoURL", "streamUrl"),
}

TRUTHY = {"true", "yes", "y", "1", "online", "active", "ok"}
FALSEY = {"false", "no", "n", "0", "offline", "inactive", "down"}


@dataclass
class Camera:
    """One camera, normalised."""

    camera_id: str
    name: str | None
    latitude: float | None
    longitude: float | None
    area: str | None
    is_online: bool | None
    image_url: str | None
    video_url: str | None
    corridor: str | None = None
    in_region: bool = False
    match_reason: str | None = None

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    """The online flag arrives as a bool, as "true"/"false", or as a status word."""
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if text in TRUTHY:
        return True
    if text in FALSEY:
        return False
    return None


def _unwrap(payload: Any) -> list[dict[str, Any]]:
    """Accept a bare array or any of the wrapper shapes the API has used."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("cameras", "data", "items", "results", "features"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    raise FeedUnavailable(
        "The camera feed returned a shape this parser does not recognise "
        f"({type(payload).__name__}); expected a JSON array of cameras."
    )


def normalise_camera(record: dict[str, Any]) -> Camera | None:
    """One raw record to a Camera, or None if it has no usable identity."""
    values = {
        target: first_present(record, candidates)
        for target, candidates in FIELD_CANDIDATES.items()
    }

    camera_id = values["camera_id"]
    if camera_id in (None, ""):
        return None
    camera_id = str(camera_id).strip()

    latitude = _as_float(values["latitude"])
    longitude = _as_float(values["longitude"])
    if not geo.is_plausible_nyc_coordinate(latitude, longitude):
        # Keep the camera, drop the coordinates: a few records carry 0/0, and a
        # camera with a usable name is still worth listing.
        latitude, longitude = None, None

    image_url = values["image_url"]
    if image_url in (None, ""):
        image_url = CAMERA_IMAGE_URL_TEMPLATE.format(camera_id=camera_id)

    name = str(values["name"]).strip() if values["name"] not in (None, "") else None

    return Camera(
        camera_id=camera_id,
        name=name,
        latitude=latitude,
        longitude=longitude,
        area=str(values["area"]).strip() if values["area"] not in (None, "") else None,
        is_online=_as_bool(values["is_online"]),
        image_url=str(image_url),
        video_url=str(values["video_url"]) if values["video_url"] not in (None, "") else None,
        corridor=geo.corridor_for(name),
    )


def tag_region(cameras: Iterable[Camera], region: geo.Region = geo.EAST_SIDE) -> list[Camera]:
    """Mark which cameras fall in the region, and record why."""
    tagged: list[Camera] = []
    for camera in cameras:
        inside_polygon = region.contains_point(camera.latitude, camera.longitude)
        corridor = region.corridor_for(camera.name)
        camera.corridor = corridor
        if inside_polygon:
            camera.in_region = True
            camera.match_reason = "inside polygon"
        elif corridor and camera.latitude is None:
            # No coordinates to test, but the name names an East Side corridor.
            camera.in_region = True
            camera.match_reason = f"name matched {corridor} (no coordinates)"
        else:
            camera.in_region = False
            camera.match_reason = None
        tagged.append(camera)
    return tagged


def fetch_cameras(
    session: requests.Session,
    *,
    url: str = CAMERA_LIST_URL,
    region: geo.Region | None = geo.EAST_SIDE,
) -> list[Camera]:
    """Pull the inventory, normalise it, and tag region membership."""
    log.info("Fetching camera inventory from %s", url)
    payload = get_json(session, url)
    records = _unwrap(payload)

    cameras: list[Camera] = []
    skipped = 0
    for record in records:
        camera = normalise_camera(record)
        if camera is None:
            skipped += 1
            continue
        cameras.append(camera)

    if skipped:
        log.warning("Skipped %d camera records with no usable id", skipped)
    log.info("Camera inventory: %d cameras", len(cameras))

    if region is not None:
        cameras = tag_region(cameras, region)
        log.info(
            "%d of %d cameras fall in %s",
            sum(1 for camera in cameras if camera.in_region),
            len(cameras),
            region.name,
        )
    return cameras


def nearest_cameras(
    latitude: float | None,
    longitude: float | None,
    cameras: Sequence[Camera],
    *,
    limit: int = 2,
    max_miles: float = 0.5,
) -> list[tuple[Camera, float]]:
    """The closest cameras to a point, nearest first, within ``max_miles``.

    Half a mile is about six Manhattan cross-town blocks; beyond that a camera
    is no longer looking at the same traffic.
    """
    if latitude is None or longitude is None:
        return []
    scored: list[tuple[Camera, float]] = []
    for camera in cameras:
        if camera.latitude is None or camera.longitude is None:
            continue
        distance = geo.haversine_miles(
            latitude, longitude, camera.latitude, camera.longitude
        )
        if distance <= max_miles:
            scored.append((camera, distance))
    scored.sort(key=lambda pair: pair[1])
    return scored[:limit]


def download_stills(
    session: requests.Session | None,
    cameras: Sequence[Camera],
    destination: Path,
    *,
    limit: int | None = None,
    timeout: int = 20,
) -> list[dict[str, Any]]:
    """Save one still per camera. Returns a manifest, one row per attempt.

    Failures are recorded and stepped over: a handful of cameras are always
    offline, and one dead camera must not abort a snapshot of 60 others. Stills
    get their own low-retry session by default, because retrying every dead
    camera three times with backoff turns a 300-camera sweep into a long wait
    for images that were never going to arrive.
    """
    if session is None:
        session = build_session(retries=1)
    destination.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    selected = list(cameras)[:limit] if limit else list(cameras)

    for camera in selected:
        captured_at = datetime.now(timezone.utc)
        row: dict[str, Any] = {
            "camera_id": camera.camera_id,
            "name": camera.name,
            "corridor": camera.corridor,
            "captured_at_utc": captured_at.isoformat(timespec="seconds"),
            "path": None,
            "bytes": 0,
            "ok": False,
            "error": None,
        }
        if not camera.image_url:
            row["error"] = "no image url"
            manifest.append(row)
            continue

        safe_id = "".join(
            char if char.isalnum() or char in "-_" else "_" for char in camera.camera_id
        )
        path = destination / f"{safe_id}.jpg"
        try:
            response = get(session, camera.image_url, timeout=timeout)
            content = response.content
            # An offline camera answers 200 with a tiny placeholder rather than
            # an error, so size is the only signal that the frame is real.
            if len(content) < 1024:
                row["error"] = f"placeholder or empty image ({len(content)} bytes)"
                row["bytes"] = len(content)
                manifest.append(row)
                continue
            path.write_bytes(content)
            row.update({"path": str(path), "bytes": len(content), "ok": True})
        except FeedUnavailable as exc:
            row["error"] = str(exc)[:200]
        except OSError as exc:
            row["error"] = f"could not write {path}: {exc}"
        manifest.append(row)

    saved = sum(1 for row in manifest if row["ok"])
    log.info("Saved %d of %d camera stills to %s", saved, len(manifest), destination)
    return manifest
