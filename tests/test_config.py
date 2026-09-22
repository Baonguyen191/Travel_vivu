from pipeline.config import CityConfig, load_city, save_city


def test_load_city_reads_bbox_and_core(tmp_path):
    p = tmp_path / "city.yml"
    p.write_text(
        "name: Huế\n"
        "bbox: [16.335, 107.435, 16.605, 107.725]\n"
        "core_center: [16.4698, 107.5796]\n"
        "core_radius_km: 15\n"
        "polygon_path: null\n",
        encoding="utf-8",
    )
    cfg = load_city(str(p))
    assert cfg.name == "Huế"
    assert cfg.bbox == (16.335, 107.435, 16.605, 107.725)
    assert cfg.core_center == (16.4698, 107.5796)
    assert cfg.polygon_path is None


def test_save_city_roundtrips_polygon(tmp_path):
    p = tmp_path / "city.yml"
    cfg = CityConfig(
        name="Huế",
        bbox=(16.0, 107.0, 17.0, 108.0),
        core_center=(16.5, 107.5),
        core_radius_km=15.0,
        polygon_path="data/generated/city_hue_boundary.wkt",
    )
    save_city(cfg, str(p))
    assert load_city(str(p)) == cfg
