import yaml

DEFAULT_CATEGORY = "khac"


def load_category_rules(path: str = "config/categories.yml") -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def map_category(tags: dict, wikidata_classes: list[str], rules: dict) -> str:
    by_qid = rules.get("wikidata", {})
    for qid in wikidata_classes:
        if qid in by_qid:
            return by_qid[qid]

    for rule in rules.get("osm", []):
        for key, expected in rule["match"].items():
            value = tags.get(key)
            if value is not None and (expected == "*" or value == expected):
                return rule["category"]
    return DEFAULT_CATEGORY
