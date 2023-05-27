from __future__ import annotations

import datetime as dt
import json
import subprocess
import urllib
from pathlib import Path

import dateutil
import github
import pandas as pd
import requests
from github.GithubException import UnknownObjectException

from oss_health.summary import Summary

PROJECT_ROOT = Path(__file__).parent.parent
CACHE_ROOT = PROJECT_ROOT / "docs" / "source" / "cache"
ONLINE_CACHE_ROOT = "https://rhshadrach.github.io/oss_health"


def determine_default_branch(repo) -> str:
    resolution_order = ("main", "master")
    for branch in resolution_order:
        try:
            next(iter(repo.get_commits(branch)))
            return branch
        # except github.GithubException.UnknownObjectException:
        except UnknownObjectException:
            pass
    raise NotImplementedError("Unable to determine default branch")


def get_regular_commiters(history: pd.DataFrame) -> set[str]:
    result = set()
    for author in history.author.unique():
        # TODO: Ensure index contains every month
        t = history.set_index("timestamp")
        time_series = (
            t["author"].eq(author).sort_index().resample(dt.timedelta(days=30)).sum()
        )
        if time_series.gt(1).mean() > 0.5 and time_series.mean() > 1:
            result.add(author)
    return result


def repo_commits(repo, branch: str):
    # TODO: Make safer via multiple retries with sleeping
    yield from repo.get_commits(branch)


def get_history(gh: github.Github, name: str, default_branch: str | None = None):
    repo = gh.get_repo(name)
    now = dt.datetime.now(dt.timezone.utc)
    one_year = dt.timedelta(days=360)

    input_path = ONLINE_CACHE_ROOT + f"/python/{name}.parquet"
    output_path = CACHE_ROOT / "python" / f"{name}.parquet"
    response = requests.get(input_path)
    if response.status_code == 200:
        cached = pd.read_parquet(input_path)
        shas = set(cached.sha)
    else:
        cached = None
        shas = {}

    if default_branch is None:
        default_branch = determine_default_branch(repo)

    data = []
    k = 0
    try:
        for k, commit in enumerate(repo_commits(repo, default_branch)):
            timestamp = dateutil.parser.parse(commit.last_modified)
            if commit.author is None:
                author = "None"
            else:
                author = commit.author.login
            data.append((commit.sha, timestamp, author))

            if commit.sha in shas or now - timestamp > one_year:
                k += 1
                break
    except requests.exceptions.ReadTimeout:
        if cached is None:
            raise
    print(f"Loaded history by grabbing {k} commits")

    result = pd.DataFrame(data, columns=["sha", "timestamp", "author"])
    if cached is not None:
        result = pd.concat([result, cached])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_parquet(str(output_path))
    return result


def make_summaries(
    gh: github.Github, name: str, default_branch: str | None = None
) -> dict[int, Summary]:
    history = get_history(gh, name, default_branch)
    now = dt.datetime.now(dt.timezone.utc)
    result = {}
    for days in (360, 180, 90, 60):
        cutoff = dt.timedelta(days=days)
        subset = history[now - history.timestamp < cutoff]
        regular_commiters = get_regular_commiters(subset)
        regular_commiters_summary = (
            subset[subset.author.isin(list(regular_commiters))]
            .groupby("author")
            .size()
            .sort_values(ascending=False)
        )
        top_irregular_commiters = (
            subset[~subset.author.isin(list(regular_commiters))]
            .groupby("author")
            .timestamp.agg(["size", "min", "max"])
            .sort_values("size", ascending=False)
            .head()
        )
        result[days] = Summary(
            name=name,
            days=days,
            history=subset,
            regular_commiters=regular_commiters,
            regular_commiters_summary=regular_commiters_summary,
            top_irregular_commiters=top_irregular_commiters,
        )
    return result


def make_report(summaries: dict[int, Summary]) -> None:
    for days, summary in summaries.items():
        print(f"{days=}; {len(summary.regular_commiters)} regular committers")
        print()
        print(summary.regular_commiters_summary.to_string())
        print()
        print("Top non-regular contributors:")
        print(summary.top_irregular_commiters.to_string())
        print()
        print("---")
        print()

    print("Summary:")
    ser = pd.Series(
        {days: len(summaries[days].regular_commiters) for days in summaries},
        name="n_authors",
    )
    ser.index.name = "days"
    print(ser.to_string())


def run(github_pat: str):
    gh = github.Github(github_pat)
    with open(f"{CACHE_ROOT}/python/pypi_mapping.json") as f:
        python_projects = list(json.load(f).values())
    projects = {
        "python": python_projects,
    }

    for domain in projects:
        index = []
        buf = []
        for repo_name, downloads in projects["python"]:
            try:
                summaries = make_summaries(gh, repo_name)
                index.append(repo_name)
                buf.append(
                    {days: len(summaries[days].regular_commiters) for days in summaries}
                    | {"downloads_per_day": downloads}
                )
            except Exception as err:
                print(repo_name, "failed:", str(err))
        path = Path(__file__).parent / ".." / "docs" / "source" / "domains" / domain
        path.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(buf, index=index).to_csv(str(path / "summary.csv"))


def extract_substring(s, start, end=None):
    idx = s.find(start)
    if idx < 0:
        return ""
    s = s[idx + len(start) :]
    if end is None:
        return s
    idx = s.find(end)
    if idx < 0:
        return ""
    result = s[:idx]
    return result


def nth_idx(s, n, needle):
    result = 0
    for _ in range(n):
        idx = s.find(needle)
        if idx < 0:
            return idx
        result += idx + len(needle)
        s = s[idx + len(needle) :]
    result -= len(needle)
    return result


def abbreviate(x):
    abbreviations = ["", "K", "M", "B", "T"]
    thing = "1"
    a = 0
    while len(thing) < len(str(x)) - 2:
        thing += "000"
        a += 1
    b = int(thing)
    thing = round(x / b, 3)
    return str(thing) + " " + abbreviations[a]


def make_pypi_to_github_mapping(n_packages: int):
    url = "https://hugovk.github.io/top-pypi-packages/top-pypi-packages-30-days.json"
    with urllib.request.urlopen(url) as f:
        data = json.load(f)
    pypi_projects = pd.DataFrame(data["rows"]).set_index("project")["download_count"]

    input_path = f"{ONLINE_CACHE_ROOT}/python/pypi_mapping.json"
    output_path = CACHE_ROOT / "python" / "pypi_mapping.json"
    response = requests.get(input_path)
    if response.status_code == 200:
        with urllib.request.urlopen(input_path) as f:
            pypi_to_github = json.load(f)
    else:
        pypi_to_github = {}
    successes = 0
    for pypi_name, downloads in pypi_projects.items():
        value = pypi_to_github.get(pypi_name)
        if value is None:
            response = subprocess.run(
                f"python -m pypi_search {pypi_name}", shell=True, capture_output=True
            )
            pypi_summary = response.stdout.decode()
            project = extract_substring(pypi_summary, "https://github.com/", "\n")
            idx = nth_idx(project, 2, "/")
            if idx >= 0:
                project = project[:idx]
        else:
            project = value[0]

        if project != "":
            successes += 1

        pypi_to_github[pypi_name] = (project, abbreviate(downloads // 30))

        if successes == n_packages:
            break

    print(f"Processed {len(pypi_to_github)} repos:")
    print(pypi_to_github)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(pypi_to_github, f, indent=4)
