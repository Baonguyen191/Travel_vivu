import json

from pipeline.config import CityConfig, save_city
from pipeline.http import Fetcher

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

QUERY = """
[out:json][timeout:180];
relation["boundary"="administrative"]["name"="Thành phố Huế"]
        ["admin_level"~"^(4|6)$"];
out geom;
"""


def rings_to_wkt(elements: list[dict]) -> str:
    relations = [e for e in elements if e.get("type") == "relation"]
    if not relations:
        raise ValueError("Overpass không trả về relation ranh giới nào")
    chosen = max(relations, key=lambda e: int(e["tags"].get("admin_level", 0)))

    points: list[tuple[float, float]] = []
    for member in chosen.get("members", []):
        if member.get("role") != "outer":
            continue
        for node in member.get("geometry", []):
            points.append((node["lon"], node["lat"]))
    if len(points) < 3:
        raise ValueError("Ring outer có dưới 3 điểm")
    if points[0] != points[-1]:
        points.append(points[0])
    body = ", ".join(f"{lon} {lat}" for lon, lat in points)
    return f"POLYGON(({body}))"


def run(conn, cfg: CityConfig, force: bool = False) -> int:
    fetcher = Fetcher(conn, "osm_boundary", min_interval=2.0)
    res = fetcher.fetch(OVERPASS_URL, method="POST", data=QUERY, force=force)
    elements = json.loads(res.content)["elements"]
    wkt = rings_to_wkt(elements)
    save_city(
        CityConfig(cfg.name, cfg.bbox, cfg.core_center, cfg.core_radius_km, wkt),
        "config/city_hue.yml",
    )
    return 1
