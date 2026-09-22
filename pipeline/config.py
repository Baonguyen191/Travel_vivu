from dataclasses import asdict, dataclass

import yaml


@dataclass(frozen=True)
class CityConfig:
    name: str
    bbox: tuple[float, float, float, float]
    core_center: tuple[float, float]
    core_radius_km: float
    polygon_wkt: str | None = None


def load_city(path: str = "config/city_hue.yml") -> CityConfig:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return CityConfig(
        name=raw["name"],
        bbox=tuple(raw["bbox"]),
        core_center=tuple(raw["core_center"]),
        core_radius_km=float(raw["core_radius_km"]),
        polygon_wkt=raw.get("polygon_wkt"),
    )


def save_city(cfg: CityConfig, path: str = "config/city_hue.yml") -> None:
    data = asdict(cfg)
    data["bbox"] = list(cfg.bbox)
    data["core_center"] = list(cfg.core_center)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
