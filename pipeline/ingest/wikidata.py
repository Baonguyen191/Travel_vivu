import json
import re
from pathlib import Path

from pipeline.config import CityConfig
from pipeline.http import Fetcher
from pipeline.models import PlaceRecord

SPARQL_URL = "https://query.wikidata.org/sparql"
STAGED_PATH = "data/staged/wikidata.json"

POINT_RE = re.compile(r"Point\(([-\d.]+) ([-\d.]+)\)")

QUERY_TEMPLATE = """
SELECT ?item ?itemLabel ?coord ?image ?commons ?viTitle
       (GROUP_CONCAT(DISTINCT ?cls; separator="|") AS ?classes)
WHERE {{
  SERVICE wikibase:box {{
    ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:cornerWest "Point({west} {south})"^^geo:wktLiteral .
    bd:serviceParam wikibase:cornerEast "Point({east} {north})"^^geo:wktLiteral .
  }}
  ?item wdt:P31 ?cls .
  OPTIONAL {{ ?item wdt:P18 ?image. }}
  OPTIONAL {{ ?item wdt:P373 ?commons. }}
  OPTIONAL {{
    ?viArticle schema:about ?item ;
               schema:isPartOf <https://vi.wikipedia.org/> ;
               schema:name ?viTitle .
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "vi,en". }}
}}
GROUP BY ?item ?itemLabel ?coord ?image ?commons ?viTitle
"""


def parse_bindings(bindings: list[dict]) -> list[PlaceRecord]:
    places = []
    seen_qids: set[str] = set()
    for row in bindings:
        coord = row.get("coord", {}).get("value", "")
        match = POINT_RE.match(coord)
        if not match:
            continue
        lon, lat = float(match.group(1)), float(match.group(2))
        qid = row["item"]["value"].rsplit("/", 1)[-1]
        if qid in seen_qids:
            continue
        seen_qids.add(qid)
        classes = [
            c.rsplit("/", 1)[-1]
            for c in row.get("classes", {}).get("value", "").split("|")
            if c
        ]

        tags: dict[str, str] = {}
        if row.get("commons"):
            tags["commons_category"] = row["commons"]["value"]
        if row.get("image"):
            tags["image_url"] = row["image"]["value"]
        if row.get("viTitle"):
            tags["vi_title"] = row["viTitle"]["value"]

        places.append(
            PlaceRecord(
                name=row["itemLabel"]["value"],
                external_ids={"wikidata": qid},
                lat=lat,
                lon=lon,
                wikidata_classes=classes,
                tags=tags,
                source_url=row["item"]["value"],
            )
        )
    return places


def run(conn, cfg: CityConfig, force: bool = False) -> int:
    south, west, north, east = cfg.bbox
    query = QUERY_TEMPLATE.format(south=south, west=west, north=north, east=east)
    fetcher = Fetcher(conn, "wikidata", min_interval=1.0)
    res = fetcher.fetch(
        SPARQL_URL, params={"query": query, "format": "json"}, force=force
    )
    bindings = json.loads(res.content)["results"]["bindings"]
    places = parse_bindings(bindings)

    Path(STAGED_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(STAGED_PATH).write_text(
        json.dumps([p.__dict__ for p in places], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return len(places)
