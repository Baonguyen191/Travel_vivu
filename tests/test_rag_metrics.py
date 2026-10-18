import math

import pytest

from rag.metrics import (
    bootstrap_ci, hit_at_k, keyword_recall, ndcg_at_k, paired_bootstrap_diff, place_prf_at_k,
    precision_at_k, reciprocal_rank,
)

FLAGS = [False, True, False, True, False]


def test_rank_metrics():
    assert hit_at_k(FLAGS, 1) == 0.0
    assert hit_at_k(FLAGS, 2) == 1.0
    assert reciprocal_rank(FLAGS) == 0.5
    assert reciprocal_rank([False] * 5) == 0.0
    assert precision_at_k(FLAGS, 5) == pytest.approx(0.4)


def test_ndcg_matches_hand_computation():
    dcg = 1 / math.log2(3) + 1 / math.log2(5)
    idcg = 1 / math.log2(2) + 1 / math.log2(3)
    assert ndcg_at_k(FLAGS, 5, n_relevant_total=2) == pytest.approx(dcg / idcg)
    assert ndcg_at_k([True, True], 5, n_relevant_total=2) == pytest.approx(1.0)
    assert ndcg_at_k(FLAGS, 5, n_relevant_total=0) == 0.0


def test_place_prf_dedups_places_and_ignores_none():
    places = ["wikidata:Q1", "wikidata:Q1", None, "wikidata:Q3", "wikidata:Q2"]
    p, r, f1 = place_prf_at_k(places, {"wikidata:Q1", "wikidata:Q2"}, 5)
    assert (p, r) == (pytest.approx(2 / 3), 1.0)
    assert f1 == pytest.approx(0.8)
    assert place_prf_at_k(places, {"wikidata:Q9"}, 5) == (0.0, 0.0, 0.0)


def test_keyword_recall():
    assert keyword_recall("Chùa xây năm 1601 bởi chúa Nguyễn Hoàng", ["1601", "nguyễn hoàng"]) == 1.0
    assert keyword_recall("Không rõ", ["1601"]) == 0.0
    assert math.isnan(keyword_recall("x", []))


def test_bootstrap_ci_contains_mean_and_skips_nan():
    mean, lo, hi = bootstrap_ci([0, 1, 1, 1, float("nan")])
    assert mean == pytest.approx(0.75)
    assert lo <= mean <= hi


def test_paired_diff():
    mean, lo, hi = paired_bootstrap_diff([1, 1, 1, 1], [0, 0, 0, 0])
    assert (mean, lo, hi) == (1.0, 1.0, 1.0)
    with pytest.raises(ValueError):
        paired_bootstrap_diff([1], [1, 2])
