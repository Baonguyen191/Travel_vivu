"""Dựng dữ liệu OSRM (định tuyến đường bộ, miễn phí) cho Huế.

    python scripts/setup_osrm.py                      # đường bộ Huế qua Overpass, ~1 phút
    python scripts/setup_osrm.py --vietnam            # cả nước (Geofabrik), cần ~8 GB RAM Docker và ~5 GB đĩa
    docker compose --profile routing up -d osrm       # chạy server tại http://localhost:5100

Mặc định chỉ lấy đường trong khung `bbox` của config/city_hue.yml (nới thêm
MARGIN độ) qua Overpass: đúng phạm vi đồ án, xử lý nhanh và nhẹ. Bản cả nước
từng làm Docker Desktop sập giữa bước osrm-extract trên máy 8 GB RAM.

Dữ liệu nằm trong Docker volume `travel_osrm`, không nằm trong repo: repo ở thư
mục OneDrive nên dữ liệu bản đồ sẽ bị đồng bộ lên mạng. File tải về nằm ở thư
mục cache ngoài repo và bị xoá sau khi chép vào volume.

Profile `car` của OSRM (không có profile xe máy): xe máy tính theo đường ô tô;
đi bộ tính từ quãng đường (xem maps/osrm.py). Dữ liệu © OpenStreetMap (ODbL).
"""

import argparse
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
PBF_URL = "https://download.geofabrik.de/asia/vietnam-latest.osm.pbf"
IMAGE = "ghcr.io/project-osrm/osrm-backend:v6.0.0"
VOLUME = "travel_osrm"
CACHE = Path.home() / ".cache" / "osrm"
NAME = "map"  # docker-compose.yml chạy /data/map.osrm
MARGIN = 0.05  # độ, để tuyến ra vào sát rìa khung vẫn tìm được đường


def docker(*args: str) -> None:
    print("$ docker", " ".join(args), flush=True)
    subprocess.run(["docker", *args], check=True)


def in_volume(*command: str) -> None:
    docker("run", "--rm", "-v", f"{VOLUME}:/data", IMAGE, *command)


def download_hue(target: Path) -> None:
    south, west, north, east = yaml.safe_load(Path("config/city_hue.yml").read_text(encoding="utf-8"))["bbox"]
    box = f"{south - MARGIN},{west - MARGIN},{north + MARGIN},{east + MARGIN}"
    query = (f'[out:xml][timeout:300];(way["highway"]({box});relation["type"="restriction"]({box}););'
             "(._;>;);out body;")
    request = urllib.request.Request(
        OVERPASS_URL, data=urllib.parse.urlencode({"data": query}).encode(),
        headers={"User-Agent": "travel-agent-thesis/0.1 (OSRM setup)"})
    print(f"Tải đường bộ trong khung {box} từ Overpass...", flush=True)
    with urllib.request.urlopen(request, timeout=600) as resp, target.open("wb") as out:
        while chunk := resp.read(1 << 20):
            out.write(chunk)
    print(f"Đã tải {target.stat().st_size / 1e6:.1f} MB", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vietnam", action="store_true", help="Dựng cho cả nước thay vì chỉ Huế")
    args = parser.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)
    suffix = ".osm.pbf" if args.vietnam else ".osm"
    source = CACHE / f"{NAME}{suffix}"
    if not source.exists():
        if args.vietnam:
            urllib.request.urlretrieve(PBF_URL, source)
        else:
            download_hue(source)

    # Xoá dữ liệu cũ (kể cả lần dựng dở) để không tốn chỗ và không lẫn phiên bản.
    subprocess.run(["docker", "volume", "rm", VOLUME], capture_output=True)
    docker("volume", "create", VOLUME)
    docker("run", "--rm", "-v", f"{VOLUME}:/data", "-v", f"{CACHE}:/in", IMAGE,
           "cp", f"/in/{NAME}{suffix}", "/data/")
    in_volume("osrm-extract", "-p", "/opt/car.lua", f"/data/{NAME}{suffix}")
    in_volume("osrm-partition", f"/data/{NAME}.osrm")
    in_volume("osrm-customize", f"/data/{NAME}.osrm")
    in_volume("rm", f"/data/{NAME}{suffix}")
    source.unlink()
    print("Xong. Chạy: docker compose --profile routing up -d osrm", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
