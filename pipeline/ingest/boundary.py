import json
from pathlib import Path

from pipeline.config import CityConfig, save_city
from pipeline.http import Fetcher

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

QUERY = """
[out:json][timeout:180];
relation["boundary"="administrative"]["name"="Thành phố Huế"]
        ["admin_level"~"^(4|6)$"];
out geom;
"""

GENERATED_WKT_PATH = "data/generated/city_hue_boundary.wkt"


def _chain_ways(ways: list[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    """Nối các way 'outer' lại thành một vòng liên tục bằng cách khớp điểm đầu/cuối.

    OSM không đảm bảo các member của relation được lưu nối tiếp nhau, nên
    ta phải chủ động tìm way khớp điểm cuối của vòng hiện tại (đảo chiều
    nếu cần) thay vì nối theo đúng thứ tự Overpass trả về.
    """
    remaining = [list(way) for way in ways]
    ring = remaining.pop(0)
    while remaining:
        last = ring[-1]
        for i, way in enumerate(remaining):
            if way[0] == last:
                ring.extend(way[1:])
                remaining.pop(i)
                break
            if way[-1] == last:
                ring.extend(list(reversed(way))[1:])
                remaining.pop(i)
                break
        else:
            raise ValueError(
                f"Không thể nối {len(remaining)} way còn lại vào vòng ranh giới"
                " — ranh giới có thể bị đứt đoạn hoặc rời rạc"
            )
    return ring


def rings_to_wkt(elements: list[dict]) -> str:
    relations = [e for e in elements if e.get("type") == "relation"]
    if not relations:
        raise ValueError("Overpass không trả về relation ranh giới nào")
    chosen = max(relations, key=lambda e: int(e.get("tags", {}).get("admin_level", 0)))

    ways: list[list[tuple[float, float]]] = []
    for member in chosen.get("members", []):
        if member.get("role") != "outer":
            continue
        way = [(node["lon"], node["lat"]) for node in member.get("geometry", [])]
        if way:
            ways.append(way)
    if not ways:
        raise ValueError("Ring outer có dưới 3 điểm")

    points = _chain_ways(ways)
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

    out_path = Path(GENERATED_WKT_PATH)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(wkt, encoding="utf-8")

    save_city(
        CityConfig(cfg.name, cfg.bbox, cfg.core_center, cfg.core_radius_km, GENERATED_WKT_PATH),
        "config/city_hue.yml",
    )
    return 1
