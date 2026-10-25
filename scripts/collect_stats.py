import json
import os
import re
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

ORG = os.getenv("GITHUB_ORG", "TheTuringClubJU")
TOKEN = os.getenv("GITHUB_TOKEN")
API = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "README.template.md"
OUTPUT = ROOT / "profile" / "README.md"
DATA_FILE = ROOT / "data" / "stats.json"

if not TOKEN:
    raise SystemExit("Set GITHUB_TOKEN before running the collector.")

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})


def get(url, params=None):
    response = session.get(url, params=params, timeout=30)

    if response.status_code in (403, 429):
        if response.headers.get("X-RateLimit-Remaining") == "0":
            raise RuntimeError(
                "GitHub API rate limit reached. Retry after reset."
            )

    response.raise_for_status()
    return response


def paginate(url, params=None):
    params = dict(params or {})
    params.setdefault("per_page", 100)

    while url:
        response = get(url, params)
        items = response.json()

        if not isinstance(items, list):
            raise RuntimeError(f"Unexpected response from {url}")

        yield from items

        next_url = response.links.get("next", {}).get("url")
        url = next_url
        params = {}


def current_month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def in_current_month(timestamp, month):
    return bool(timestamp and timestamp.startswith(month))


def new_person():
    return {
        "commits_all": 0,
        "commits_month": 0,
        "additions_all": 0,
        "additions_month": 0,
        "deletions_all": 0,
        "deletions_month": 0,
        "prs_opened_all": 0,
        "prs_opened_month": 0,
        "prs_merged_all": 0,
        "prs_merged_month": 0,
        "reviews_all": 0,
        "reviews_month": 0,
        "repositories": set(),
    }


def get_git_history(repo_url, branch):
    """Get full default-branch history and line changes."""
    with tempfile.TemporaryDirectory() as tmp:
        destination = str(Path(tmp) / "repo")

        subprocess.run(
            [
                "git", "clone", "--quiet",
                "--single-branch",
                "--branch", branch,
                repo_url, destination,
            ],
            check=True,
            timeout=1800,
            capture_output=True,
            text=True,
        )

        result = subprocess.run(
            [
                "git", "-C", destination,
                "log", "--numstat",
                "--format=COMMIT%x1f%H%x1f%an%x1f%aI",
                "HEAD",
            ],
            check=True,
            timeout=1800,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    commits = []
    current = None

    for line in result.stdout.splitlines():
        if line.startswith("COMMIT\x1f"):
            if current is not None:
                commits.append(current)

            parts = line.split("\x1f", 3)

            if len(parts) != 4:
                raise RuntimeError(
                    f"Unexpected Git commit record: {line!r}"
                )

            current = {
                "sha": parts[1],
                "author_name": parts[2].strip()
                    or "Unlinked Git author",
                "date": parts[3],
                "additions": 0,
                "deletions": 0,
            }
            continue

        if current is None or not line.strip():
            continue

        columns = line.split("\t", 2)

        if len(columns) != 3:
            continue

        try:
            if columns[0] != "-":
                current["additions"] += int(columns[0])

            if columns[1] != "-":
                current["deletions"] += int(columns[1])

        except ValueError:
            continue

    if current is not None:
        commits.append(current)

    print(f"  Git history collected: {len(commits)} commits")
    return commits

def collect():
    month = current_month()
    people = defaultdict(new_person)
    repositories = []

    repos = list(paginate(
        f"{API}/orgs/{ORG}/repos",
        {"type": "public", "sort": "full_name"},
    ))

    # Exclude forks and archived repositories.
    repos = [
        repo for repo in repos
        if not repo.get("fork") and not repo.get("archived")
    ]

    for index, repo in enumerate(repos, start=1):
        full_name = repo["full_name"]
        branch = repo.get("default_branch")

        if not branch:
            continue

        print(f"[{index}/{len(repos)}] {full_name}", flush=True)

        contributor_names = set()

        # GitHub API associates commit SHAs with GitHub logins when possible.
        api_commits = list(paginate(
            f"{API}/repos/{full_name}/commits",
            {"sha": branch},
        ))

        login_by_sha = {
            commit["sha"]: (
                (commit.get("author") or {}).get("login")
            )
            for commit in api_commits
        }

        git_commits = get_git_history(repo["clone_url"], branch)

        for commit in git_commits:
            login = login_by_sha.get(commit["sha"])
            name = login or commit["author_name"] or "Unlinked Git author"

            person = people[name]
            person["commits_all"] += 1
            person["additions_all"] += commit["additions"]
            person["deletions_all"] += commit["deletions"]
            person["repositories"].add(full_name)
            contributor_names.add(name)

            if in_current_month(commit["date"], month):
                person["commits_month"] += 1
                person["additions_month"] += commit["additions"]
                person["deletions_month"] += commit["deletions"]

        # Collect PRs. Opened = creation date; merged = merge date.
        prs = list(paginate(
            f"{API}/repos/{full_name}/pulls",
            {
                "state": "all",
                "sort": "created",
                "direction": "desc",
            },
        ))

        repo_opened = 0
        repo_merged = 0
        repo_reviews = 0

        for pr in prs:
            author = (pr.get("user") or {}).get("login")

            if author:
                person = people[author]
                person["prs_opened_all"] += 1
                person["repositories"].add(full_name)
                contributor_names.add(author)
                repo_opened += 1

                if in_current_month(pr.get("created_at"), month):
                    person["prs_opened_month"] += 1

                if pr.get("merged_at"):
                    person["prs_merged_all"] += 1
                    repo_merged += 1

                    if in_current_month(pr["merged_at"], month):
                        person["prs_merged_month"] += 1

            # Reviews belong to the reviewer, not the PR author.
            reviews = list(paginate(
                f"{API}/repos/{full_name}/pulls/"
                f"{pr['number']}/reviews"
            ))

            for review in reviews:
                reviewer = (review.get("user") or {}).get("login")
                submitted = review.get("submitted_at")

                if not reviewer or not submitted:
                    continue

                person = people[reviewer]
                person["reviews_all"] += 1
                person["repositories"].add(full_name)
                contributor_names.add(reviewer)
                repo_reviews += 1

                if in_current_month(submitted, month):
                    person["reviews_month"] += 1

        repositories.append({
            "name": repo["name"],
            "full_name": full_name,
            "url": repo["html_url"],
            "commits": len(git_commits),
            "prs_opened": repo_opened,
            "prs_merged": repo_merged,
            "reviews": repo_reviews,
            "contributors": len(contributor_names),
        })

    # Convert sets to JSON-serializable counts.
    for person in people.values():
        person["repositories"] = len(person["repositories"])

    return {
        "organization": ORG,
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        ),
        "month": month,
        "repositories_scanned": len(repositories),
        "people": dict(people),
        "repositories": sorted(
            repositories,
            key=lambda item: (
                item["commits"], item["prs_merged"]
            ),
            reverse=True,
        ),
    }


def fmt(value):
    return f"{value:,}"


def contributor_rows(people, period):
    rows = []

    suffix = "all" if period == "all" else "month"

    ranked = sorted(
        people.items(),
        key=lambda item: (
            item[1][f"commits_{suffix}"]
            + 2 * item[1][f"prs_merged_{suffix}"]
            + item[1][f"reviews_{suffix}"]
        ),
        reverse=True,
    )

    for name, person in ranked[:20]:
        if re.fullmatch(r"[A-Za-z0-9-]{1,39}", name):
            label = f"[@{name}](https://github.com/{name})"
        else:
            label = name.replace("|", "\\|")

        rows.append(
            f"| {label} "
            f"| {fmt(person[f'commits_{suffix}'])} "
            f"| {fmt(person[f'prs_opened_{suffix}'])} "
            f"| {fmt(person[f'prs_merged_{suffix}'])} "
            f"| {fmt(person[f'reviews_{suffix}'])} "
            f"| +{fmt(person[f'additions_{suffix}'])} "
            f"| -{fmt(person[f'deletions_{suffix}'])} |"
        )

    if not rows:
        return "| No contributors yet | 0 | 0 | 0 | 0 | 0 | 0 |"

    return "\n".join(rows)


def render(data):
    people = data["people"]
    repos = data["repositories"]

    def total(metric, period="all"):
        return sum(
            person[f"{metric}_{period}"]
            for person in people.values()
        )

    values = {
        "ORG": data["organization"],
        "GENERATED_AT": data["generated_at"],
        "MONTH": data["month"],
        "REPOSITORIES_SCANNED": fmt(data["repositories_scanned"]),
        "CONTRIBUTORS_ALL": fmt(len(people)),
        "COMMITS_ALL": fmt(total("commits")),
        "PRS_ALL": fmt(total("prs_opened")),
        "MERGED_ALL": fmt(total("prs_merged")),
        "REVIEWS_ALL": fmt(total("reviews")),
        "ADDITIONS_ALL": fmt(total("additions")),
        "DELETIONS_ALL": fmt(total("deletions")),
        "COMMITS_MONTH": fmt(total("commits", "month")),
        "PRS_MONTH": fmt(total("prs_opened", "month")),
        "MERGED_MONTH": fmt(total("prs_merged", "month")),
        "REVIEWS_MONTH": fmt(total("reviews", "month")),
        "ADDITIONS_MONTH": fmt(total("additions", "month")),
        "DELETIONS_MONTH": fmt(total("deletions", "month")),
        "ALL_TIME_ROWS": contributor_rows(people, "all"),
        "MONTH_ROWS": contributor_rows(people, "month"),
    }

    repo_rows = []

    for repo in repos[:15]:
        repo_rows.append(
            f"| [{repo['name']}]({repo['url']}) "
            f"| {fmt(repo['commits'])} "
            f"| {fmt(repo['prs_opened'])} "
            f"| {fmt(repo['prs_merged'])} "
            f"| {fmt(repo['contributors'])} |"
        )

    values["REPO_ROWS"] = "\n".join(repo_rows) or (
        "| No eligible repositories | 0 | 0 | 0 | 0 |"
    )

    content = TEMPLATE.read_text(encoding="utf-8")

    for key, value in values.items():
        content = content.replace("{{" + key + "}}", value)

    return content


def main():
    data = collect()

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

    OUTPUT.write_text(render(data), encoding="utf-8")
    DATA_FILE.write_text(
        json.dumps(data, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print(f"Updated {OUTPUT}")
    print(f"Updated {DATA_FILE}")


if __name__ == "__main__":
    main()