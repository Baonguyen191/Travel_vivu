import yaml

DEFAULT_CATEGORY = "khac"


def load_category_rules(path: str = "config/categories.yml") -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _rule_matches(tags: dict, match: dict) -> bool:
    """True nếu `tags` khớp mọi cặp key/value trong `match` (AND, không phải OR).

    Một rule nhiều key (vd. {amenity: restaurant, cuisine: vietnamese}) chỉ khớp
    khi tất cả điều kiện đều đúng; nếu chỉ cần một điều kiện là đủ, rule đó nên
    được tách thành nhiều rule riêng trong YAML.
    """
    for key, expected in match.items():
        value = tags.get(key)
        if value is None or (expected != "*" and value != expected):
            return False
    return True


def map_category(tags: dict, wikidata_classes: list[str], rules: dict) -> str:
    by_qid = rules.get("wikidata", {})
    for qid in wikidata_classes:
        if qid in by_qid:
            return by_qid[qid]

    for rule in rules.get("osm", []):
        if _rule_matches(tags, rule["match"]):
            return rule["category"]
    return DEFAULT_CATEGORY
