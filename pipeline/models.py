from dataclasses import dataclass, field


@dataclass
class PlaceRecord:
    name: str
    external_ids: dict[str, str]
    lat: float
    lon: float
    category: str = "khac"
    name_en: str | None = None
    opening_hours: dict | None = None
    opening_hours_raw: str | None = None
    website: str | None = None
    source_url: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    wikidata_classes: list[str] = field(default_factory=list)
    avg_visit_minutes: int | None = None
    indoor_ratio: float | None = None
    weather_sensitivity: dict | None = None
    best_time_of_day: list[str] | None = None
    unsafe_conditions: list[str] | None = None
    ticket_price: dict | None = None
    dress_code: str | None = None
    label_source: str = "default"

    @property
    def place_key(self) -> str:
        source, external_id = sorted(self.external_ids.items())[0]
        return f"{source}:{external_id}"


@dataclass
class ImageRecord:
    place_key: str
    image_url: str
    license: str | None = None
    author: str | None = None


@dataclass
class ChunkRecord:
    place_key: str
    content: str
    source_url: str
