import importlib.util
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "collect_stats.py"
)

spec = importlib.util.spec_from_file_location("collector", SCRIPT)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def test_render_generates_contributor_table():
    data = {
        "organization": "ExampleOrg",
        "generated_at": "2026-10-25 10:00 UTC",
        "month": "2026-10",
        "repositories_scanned": 1,
        "people": {
            "octocat": {
                "commits_all": 2,
                "commits_month": 1,
                "additions_all": 10,
                "additions_month": 5,
                "deletions_all": 3,
                "deletions_month": 1,
                "prs_opened_all": 1,
                "prs_opened_month": 1,
                "prs_merged_all": 1,
                "prs_merged_month": 0,
                "reviews_all": 2,
                "reviews_month": 1,
                "repositories": 1,
            }
        },
        "repositories": [
            {
                "name": "demo",
                "url": "https://github.com/ExampleOrg/demo",
                "commits": 2,
                "prs_opened": 1,
                "prs_merged": 1,
                "contributors": 1,
            }
        ],
    }

    result = collector.render(data)

    assert "ExampleOrg — Engineering Activity" in result
    assert "@octocat" in result
    assert "+10" in result
    assert "-3" in result
    assert "{{ORG}}" not in result