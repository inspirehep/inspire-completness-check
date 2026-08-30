import os
from datetime import date
from unittest.mock import MagicMock

os.environ.setdefault("OPENSEARCH_INSPIRE_HOST", "test")
os.environ.setdefault("OPENSEARCH_INSPIRE_USER", "test")
os.environ.setdefault("OPENSEARCH_INSPIRE_PASSWORD", "test")

from lib import arxiv_completness_check_script as completeness


def test_backoffice_check_includes_lower_date_boundary(monkeypatch):
    search = MagicMock()
    search.query.return_value.params.return_value.execute.return_value = {
        "hits": {"hits": []}
    }
    monkeypatch.setattr(completeness, "LiteratureSearch", lambda index: search)

    completeness.backoffice_check(
        {"2501.00001"},
        date(2025, 1, 3),
        date(2025, 1, 6),
    )

    query = search.query.call_args[0][0].to_dict()
    date_range = next(
        clause["range"]["data.acquisition_source.datetime"]
        for clause in query["bool"]["must"]
        if "range" in clause
        and "data.acquisition_source.datetime" in clause["range"]
    )
    assert date_range["gte"] == date(2025, 1, 3)
