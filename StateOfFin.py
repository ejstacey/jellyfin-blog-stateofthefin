#!/usr/bin/env python3

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from github import Auth, Github, GithubException

# MARK: - Configuration

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPOS_JSON = os.path.join(SCRIPT_DIR, "repos.json")
CONTRIBUTORS_JSON = os.path.join(SCRIPT_DIR, "contributors.json")
TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "templates", "stateofthefin.mdx")
CURRENT_DIR = os.path.join(SCRIPT_DIR, "current")
ARCHIVE_DIR = os.path.join(SCRIPT_DIR, "archive")
BLOG_DIR = os.path.join(SCRIPT_DIR, "blog")


def load_config() -> dict:
    with open(REPOS_JSON) as f:
        return json.load(f)


@dataclass
class ContributorsConfig:
    maintainers: list[dict[str, str]]
    maintainer_usernames: set[str]
    blacklist: set[str]
    hidden: set[str]  # combined: maintainers + blacklist
    repo_maintainers: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # lowercase repo key -> list of (name, url)


def load_contributors() -> ContributorsConfig:
    if not os.path.isfile(CONTRIBUTORS_JSON):
        return ContributorsConfig([], set(), set(), set(), {})
    with open(CONTRIBUTORS_JSON) as f:
        data = json.load(f)
    maintainers = data.get("maintainers", [])
    maintainer_usernames = {m["username"] for m in maintainers}
    blacklist = set(data.get("blacklist", []))
    hidden = maintainer_usernames | blacklist
    # Build per-repo maintainer lookup: repo -> [(name, url), ...]
    repo_maintainers: dict[str, list[tuple[str, str]]] = {}
    for m in maintainers:
        name = m.get("name", m["username"])
        url = m.get("url", f"https://github.com/{m['username']}")
        for repo in m.get("repos", []):
            repo_maintainers.setdefault(repo.lower(), []).append((name, url))
    return ContributorsConfig(maintainers, maintainer_usernames, blacklist, hidden, repo_maintainers)


def _find_last_blog_date() -> Optional[datetime]:
    """Scan the blog/ directory for the most recent State of the Fin post date."""
    if not os.path.isdir(BLOG_DIR):
        return None
    dates: list[datetime] = []
    for year_dir in os.listdir(BLOG_DIR):
        year_path = os.path.join(BLOG_DIR, year_dir)
        if not os.path.isdir(year_path):
            continue
        for entry in os.listdir(year_path):
            # Expect format: MM-DD-state-of-the-fin
            if not entry.endswith("-state-of-the-fin"):
                continue
            parts = entry.split("-state-of-the-fin")[0]  # e.g. "01-06"
            try:
                dt = datetime.strptime(f"{year_dir}-{parts}", "%Y-%m-%d")
                dates.append(dt)
            except ValueError:
                continue
    return max(dates) if dates else None


def auto_date_range(reference: Optional[datetime] = None) -> tuple[datetime, datetime]:
    ref = reference or datetime.now()
    end = ref
    last_blog = _find_last_blog_date()
    if last_blog and last_blog < end:
        start = last_blog
    else:
        # Fallback: previous month (first run or no prior blogs)
        first_of_month = ref.replace(day=1)
        if first_of_month.month == 1:
            start = first_of_month.replace(year=first_of_month.year - 1, month=12)
        else:
            start = first_of_month.replace(month=first_of_month.month - 1)
    return start, end


# MARK: - Models

@dataclass
class MonthlyStats:
    month_start: str
    month_end: str
    display_name: str
    closed_issues: int = 0
    merged_prs: int = 0
    contributors: int = 0


@dataclass
class Release:
    repo: str
    display_name: str
    tag: str
    name: str
    published_at: str
    url: str
    commits_count: int = 0


@dataclass
class RepoStats:
    name: str
    display_name: str
    closed_issues: int = 0
    merged_prs: int = 0
    unique_contributors: int = 0
    top_contributors: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class RepoInput:
    repo_name: str
    client_name: str
    client_url: str
    author_name: str
    author_url: str
    content: str


@dataclass
class RangeData:
    start_date: datetime
    end_date: datetime
    monthly_stats: list[MonthlyStats]
    chart_monthly_stats: list[MonthlyStats]
    unique_contributors: set[str]
    yearly_contributors: set[str]
    releases: list[Release]
    repo_stats: dict[str, RepoStats]


# MARK: - Date Utilities

def get_month_ranges(start_date: datetime, end_date: datetime) -> list[tuple[datetime, datetime, str]]:
    ranges = []
    current = start_date.replace(day=1)
    end_month = end_date.replace(day=1)

    while current <= end_month:
        if current.month == 12:
            next_month = current.replace(year=current.year + 1, month=1)
        else:
            next_month = current.replace(month=current.month + 1)
        month_end = next_month - timedelta(days=1)

        actual_start = max(current, start_date)
        actual_end = min(month_end, end_date)

        display = current.strftime("%b %Y")
        ranges.append((actual_start, actual_end, display))

        current = next_month

    return ranges


def get_trailing_months(end_date: datetime, months: int = 12) -> list[tuple[datetime, datetime, str]]:
    ranges = []

    # Always include the current (possibly partial) month
    current_month_start = end_date.replace(day=1)
    display = current_month_start.strftime("%b")
    ranges.append((current_month_start, end_date, display))

    # Then walk backwards for the remaining months
    current = current_month_start
    for _ in range(months - 1):
        prev_month = current - timedelta(days=1)
        month_start = prev_month.replace(day=1)
        month_end = prev_month
        display = month_start.strftime("%b")
        ranges.append((month_start, month_end, display))
        current = month_start

    ranges.reverse()
    return ranges


# MARK: - DataCollector

class DataCollector:

    def __init__(self, gh: Github, org: str, contributors: Optional[ContributorsConfig] = None):
        self.gh = gh
        self.org = org
        self.contributors = contributors or ContributorsConfig([], set(), set(), set())

    def _build_scope(self, repo: Optional[str] = None) -> str:
        if repo:
            return f"repo:{self.org}/{repo}"
        return f"org:{self.org}"

    def count_closed_issues(self, start: str, end: str, repo: Optional[str] = None) -> int:
        scope = self._build_scope(repo)
        query = f"{scope} is:issue is:closed closed:{start}..{end}"
        return self.gh.search_issues(query).totalCount

    def count_merged_prs(self, start: str, end: str, repo: Optional[str] = None) -> int:
        scope = self._build_scope(repo)
        query = f"{scope} is:pr is:merged merged:{start}..{end}"
        return self.gh.search_issues(query).totalCount

    def fetch_merged_pr_authors(self, start: str, end: str, repo: Optional[str] = None) -> list[str]:
        scope = self._build_scope(repo)
        query = f"{scope} is:pr is:merged merged:{start}..{end}"
        authors = []
        for pr in self.gh.search_issues(query):
            if pr.user:
                authors.append(pr.user.login)
        return authors

    def fetch_repo_releases(self, repo_name: str, display_name: str, since: datetime, until: datetime) -> list[Release]:
        try:
            repo = self.gh.get_repo(f"{self.org}/{repo_name}")
        except GithubException:
            return []

        releases = []
        all_releases = []

        for release in repo.get_releases():
            if release.draft or not release.published_at:
                continue
            pub_naive = release.published_at.replace(tzinfo=None)
            all_releases.append({
                "tag": release.tag_name,
                "name": release.name or release.tag_name,
                "published_at": pub_naive,
                "url": release.html_url,
            })

        all_releases.sort(key=lambda x: x["published_at"])

        for i, rel in enumerate(all_releases):
            if not (since <= rel["published_at"] <= until):
                continue

            commits_count = 0
            if i > 0:
                prev_tag = all_releases[i - 1]["tag"]
                try:
                    comparison = repo.compare(prev_tag, rel["tag"])
                    commits_count = comparison.total_commits
                except GithubException:
                    pass

            releases.append(Release(
                repo=repo_name,
                display_name=display_name,
                tag=rel["tag"],
                name=rel["name"],
                published_at=rel["published_at"].strftime("%Y-%m-%d"),
                url=rel["url"],
                commits_count=commits_count,
            ))

        return releases

    def _fetch_chart_contributors(
        self, chart_ranges: list[tuple[datetime, datetime, str]]
    ) -> tuple[dict[int, int], set[str]]:
        """Fetch unique contributor counts per chart month via batched searches.

        Processes chart months in bi-monthly chunks to stay under GitHub's
        1000-result search API limit while avoiding 12 separate full iterations.

        Returns (month_index -> unique_count, all_yearly_contributors).
        """
        if not chart_ranges:
            return {}, set()

        month_authors: dict[int, set[str]] = {i: set() for i in range(len(chart_ranges))}
        all_authors: set[str] = set()
        scope = self._build_scope()

        # Process in bi-monthly chunks to stay under 1000 result limit
        chunk_size = 2
        for chunk_start in range(0, len(chart_ranges), chunk_size):
            chunk = chart_ranges[chunk_start:chunk_start + chunk_size]
            chunk_indices = range(chunk_start, chunk_start + len(chunk))

            start = chunk[0][0].strftime("%Y-%m-%d")
            end = chunk[-1][1].strftime("%Y-%m-%d")

            query = f"{scope} is:pr is:merged merged:{start}..{end}"

            for pr in self.gh.search_issues(query):
                if not pr.user:
                    continue
                author = pr.user.login
                all_authors.add(author)

                if pr.closed_at:
                    closed_date = pr.closed_at.replace(tzinfo=None).date()
                    for i in chunk_indices:
                        ms, me, _ = chart_ranges[i]
                        if ms.date() <= closed_date <= me.date():
                            month_authors[i].add(author)
                            break

        counts = {i: len(authors) for i, authors in month_authors.items()}
        return counts, all_authors

    def _collect_repo_stats(self, repo: str, display_name: str, start_str: str, end_str: str) -> RepoStats:
        closed = self.count_closed_issues(start_str, end_str, repo)
        merged = self.count_merged_prs(start_str, end_str, repo)

        top_contributors: list[tuple[str, int]] = []
        unique_contributors = 0
        if merged > 0:
            authors = self.fetch_merged_pr_authors(start_str, end_str, repo)
            contributor_counts: dict[str, int] = {}
            for author in authors:
                contributor_counts[author] = contributor_counts.get(author, 0) + 1
            filtered = {k: v for k, v in contributor_counts.items() if k not in self.contributors.hidden}
            top_contributors = sorted(filtered.items(), key=lambda x: x[1], reverse=True)[:3]
            unique_contributors = len(contributor_counts)

        return RepoStats(
            name=repo,
            display_name=display_name,
            closed_issues=closed,
            merged_prs=merged,
            unique_contributors=unique_contributors,
            top_contributors=top_contributors,
        )

    def collect_range_data(
        self,
        start_date: datetime,
        end_date: datetime,
        all_repos: dict[str, str],
    ) -> RangeData:
        start_str = start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")

        print(f"Collecting data from {start_str} to {end_str}...", file=sys.stderr)

        # Monthly stats for the period
        month_ranges = get_month_ranges(start_date, end_date)
        monthly_stats: list[MonthlyStats] = []
        all_contributors: set[str] = set()

        print("  Fetching period monthly stats and contributors...", file=sys.stderr)
        for i, (month_start, month_end, display) in enumerate(month_ranges, 1):
            ms_str = month_start.strftime("%Y-%m-%d")
            me_str = month_end.strftime("%Y-%m-%d")

            print(f"    [{i}/{len(month_ranges)}] {display}", file=sys.stderr)

            closed_issues = self.count_closed_issues(ms_str, me_str)
            merged_prs = self.count_merged_prs(ms_str, me_str)

            authors = self.fetch_merged_pr_authors(ms_str, me_str)
            all_contributors.update(authors)

            monthly_stats.append(MonthlyStats(
                month_start=ms_str,
                month_end=me_str,
                display_name=display,
                closed_issues=closed_issues,
                merged_prs=merged_prs,
            ))

        # 12-month trailing chart data
        print("  Fetching 12-month chart data...", file=sys.stderr)
        chart_ranges = get_trailing_months(end_date, 12)
        chart_monthly_stats: list[MonthlyStats] = []

        # Batch-fetch contributor counts for all 12 months in one search
        print("    Fetching contributor counts (batched)...", file=sys.stderr)
        contributor_counts, yearly_contributors = self._fetch_chart_contributors(chart_ranges)

        for i, (month_start, month_end, display) in enumerate(chart_ranges):
            ms_str = month_start.strftime("%Y-%m-%d")
            me_str = month_end.strftime("%Y-%m-%d")

            print(f"    [{i + 1}/12] {display}", file=sys.stderr)

            closed_issues = self.count_closed_issues(ms_str, me_str)
            merged_prs = self.count_merged_prs(ms_str, me_str)

            chart_monthly_stats.append(MonthlyStats(
                month_start=ms_str,
                month_end=me_str,
                display_name=display,
                closed_issues=closed_issues,
                merged_prs=merged_prs,
                contributors=contributor_counts.get(i, 0),
            ))

        # Releases for all repos
        print("  Fetching releases...", file=sys.stderr)
        all_releases: list[Release] = []
        for repo, display_name in all_repos.items():
            releases = self.fetch_repo_releases(repo, display_name, start_date, end_date)
            all_releases.extend(releases)
        all_releases.sort(key=lambda x: x.published_at)

        # Per-repo stats
        repo_stats: dict[str, RepoStats] = {}
        total = len(all_repos)
        for i, (repo, display_name) in enumerate(all_repos.items(), 1):
            print(f"  [{i}/{total}] Fetching stats for {repo}...", file=sys.stderr)
            stats = self._collect_repo_stats(repo, display_name, start_str, end_str)
            repo_stats[repo] = stats

        return RangeData(
            start_date=start_date,
            end_date=end_date,
            monthly_stats=monthly_stats,
            chart_monthly_stats=chart_monthly_stats,
            unique_contributors=all_contributors,
            yearly_contributors=yearly_contributors,
            releases=all_releases,
            repo_stats=repo_stats,
        )


# MARK: - Input Reading

def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse YAML-like frontmatter from a markdown file.

    Returns (metadata dict, body content after frontmatter).
    """
    metadata: dict[str, str] = {}

    if not text.startswith("---"):
        return metadata, text

    end = text.find("---", 3)
    if end == -1:
        return metadata, text

    front = text[3:end].strip()
    body = text[end + 3:].strip()

    for line in front.split("\n"):
        line = line.strip()
        if not line:
            continue
        colon = line.find(":")
        if colon == -1:
            continue
        key = line[:colon].strip()
        value = line[colon + 1:].strip()
        metadata[key] = value

    return metadata, body


def read_current_inputs(current_dir: str) -> tuple[dict[str, RepoInput], str, str, str]:
    """Read all .md files from current/.

    Returns:
        (repo_inputs, overview_content, other_title, other_content)
        repo_inputs: dict mapping repo name -> RepoInput with frontmatter + content
        overview_content: raw content of overview.md (empty string if missing)
        other_title: title from other.md frontmatter (empty string if missing)
        other_content: body content of other.md after frontmatter (empty string if missing)
    """
    repo_inputs: dict[str, RepoInput] = {}
    overview_content = ""
    other_title = ""
    other_content = ""

    if not os.path.isdir(current_dir):
        return repo_inputs, overview_content, other_title, other_content

    for filename in sorted(os.listdir(current_dir)):
        if not filename.endswith(".md") or filename == ".gitkeep":
            continue

        filepath = os.path.join(current_dir, filename)
        if not os.path.isfile(filepath):
            continue

        with open(filepath) as f:
            raw = f.read().strip()

        if filename == "overview.md":
            overview_content = raw
        elif filename == "other.md":
            metadata, body = parse_frontmatter(raw)
            other_title = metadata.get("title", "")
            other_content = body
        else:
            repo_name = filename[:-3]  # strip .md
            metadata, body = parse_frontmatter(raw)
            repo_inputs[repo_name] = RepoInput(
                repo_name=repo_name,
                client_name=metadata.get("client_name", ""),
                client_url=metadata.get("client_url", ""),
                author_name=metadata.get("author_name", ""),
                author_url=metadata.get("author_url", ""),
                content=body,
            )

    return repo_inputs, overview_content, other_title, other_content


def parse_overview(content: str) -> dict[str, str]:
    """Parse overview.md into sections by ## headings.

    Returns dict with lowercase keys like:
        "introduction", "project updates", "sign off"
    """
    sections: dict[str, str] = {}
    if not content:
        return sections

    current_heading = None
    current_lines: list[str] = []

    for line in content.split("\n"):
        heading_match = re.match(r"^##\s+(.+)$", line)
        if heading_match:
            if current_heading is not None:
                sections[current_heading] = "\n".join(current_lines).strip()
            current_heading = heading_match.group(1).strip().lower()
            current_lines = []
        elif current_heading is not None:
            current_lines.append(line)

    if current_heading is not None:
        sections[current_heading] = "\n".join(current_lines).strip()

    return sections


# MARK: - Content Generation

def generate_activity(data: RangeData) -> str:
    lines: list[str] = []

    total_closed = sum(m.closed_issues for m in data.monthly_stats)
    total_merged = sum(m.merged_prs for m in data.monthly_stats)
    total_contributors = len(data.unique_contributors)

    yearly_closed = sum(m.closed_issues for m in data.chart_monthly_stats)
    yearly_merged = sum(m.merged_prs for m in data.chart_monthly_stats)
    yearly_contributors = len(data.yearly_contributors)

    period_start = data.start_date.strftime("%b %d, %Y")
    period_end = data.end_date.strftime("%b %d, %Y")

    chart_start = datetime.strptime(data.chart_monthly_stats[0].month_start, "%Y-%m-%d").strftime("%b %d, %Y") if data.chart_monthly_stats else ""
    chart_end = datetime.strptime(data.chart_monthly_stats[-1].month_end, "%Y-%m-%d").strftime("%b %d, %Y") if data.chart_monthly_stats else ""

    lines.append(f"**{period_start} \u2013 {period_end}**<br/>")
    lines.append(f"_{total_closed:,} issues closed_<br/>")
    lines.append(f"_{total_merged:,} PRs merged_<br/>")
    lines.append(f"_{total_contributors:,} contributors_")
    lines.append("")
    lines.append(f"**{chart_start} \u2013 {chart_end}**<br/>")
    lines.append(f"_{yearly_closed:,} issues closed_<br/>")
    lines.append(f"_{yearly_merged:,} PRs merged_<br/>")
    lines.append(f"_{yearly_contributors:,} contributors_")
    lines.append("")

    if data.chart_monthly_stats:
        max_value = max(max(m.merged_prs, m.closed_issues, m.contributors) for m in data.chart_monthly_stats)
        y_axis_max = ((max_value + 99) // 100) * 100

        lines.append("```mermaid")
        lines.append("---")
        lines.append("config:")
        lines.append("  themeVariables:")
        lines.append("    xyChart:")
        lines.append('      plotColorPalette: "#22c55e, #ef4444, #eab308"')
        lines.append("---")
        lines.append("xychart-beta")
        lines.append('  title "Activity by Month"')
        lines.append("  x-axis [" + ", ".join(f'"{m.display_name}"' for m in data.chart_monthly_stats) + "]")
        lines.append(f'  y-axis "Count" 0 --> {y_axis_max}')
        lines.append('  line "PRs Merged" [' + ", ".join(str(m.merged_prs) for m in data.chart_monthly_stats) + "]")
        lines.append('  line "Issues Closed" [' + ", ".join(str(m.closed_issues) for m in data.chart_monthly_stats) + "]")
        lines.append('  line "Contributors" [' + ", ".join(str(m.contributors) for m in data.chart_monthly_stats) + "]")
        lines.append("```")
        lines.append("")
        lines.append("<center>\U0001f7e2 PRs Merged \u00b7 \U0001f534 Issues Closed \u00b7 \U0001f7e1 Contributors</center>")

    return "\n".join(lines)


def generate_releases(data: RangeData) -> str:
    if not data.releases:
        return ""

    lines: list[str] = []
    lines.append("#### Releases")
    lines.append("")
    lines.append("| Date | Repository | Release | Commits |")
    lines.append("|------|------------|---------|---------|")
    for release in data.releases:
        release_name = release.name if release.name != release.tag else release.tag
        lines.append(f"| {release.published_at} | {release.display_name} | [{release_name}]({release.url}) | {release.commits_count} |")

    return "\n".join(lines)


def _render_repo_heading(repo_input: RepoInput, config_display_name: str) -> str:
    """Render a repo heading, using frontmatter client_name/url if available."""
    name = repo_input.client_name or config_display_name
    if repo_input.client_url:
        return f"### [{name}]({repo_input.client_url})"
    return f"### {name}"


def _render_repo_block(
    repo_input: RepoInput,
    config_display_name: str,
    stats: Optional[RepoStats],
    maintainers: Optional[list[tuple[str, str]]] = None,
) -> list[str]:
    """Render a single repo's section: heading, stats, maintainers, top contributors, content, then author at bottom."""
    lines: list[str] = []

    lines.append(_render_repo_heading(repo_input, config_display_name))
    lines.append("")

    if stats:
        lines.append(f"_{stats.closed_issues} issues closed \u00b7 {stats.merged_prs} PRs merged \u00b7 {stats.unique_contributors} contributors_")
        lines.append("")
        if maintainers:
            label = "Maintainer" if len(maintainers) == 1 else "Maintainers"
            linked = ", ".join(f"[{name}]({url})" for name, url in maintainers)
            lines.append(f"**{label}:** {linked}")
            lines.append("")
        if stats.top_contributors:
            lines.append("**Top contributors:** " + ", ".join(f"@{c[0]}" for c in stats.top_contributors))
            lines.append("")

    if repo_input.content:
        lines.append(repo_input.content)
        lines.append("")

    if repo_input.author_name:
        if repo_input.author_url:
            lines.append(f"*- [{repo_input.author_name}]({repo_input.author_url})*")
        else:
            lines.append(f"*- {repo_input.author_name}*")
        lines.append("")

    return lines


def _match_repo_input(config_key: str, repo_inputs: dict[str, RepoInput]) -> Optional[str]:
    """Find matching repo input key, case-insensitive."""
    if config_key in repo_inputs:
        return config_key
    lower = config_key.lower()
    for k in repo_inputs:
        if k.lower() == lower:
            return k
    return None


def generate_sections(
    config: dict,
    data: RangeData,
    repo_inputs: dict[str, RepoInput],
    other_title: str = "",
    other_content: str = "",
    contributors: Optional[ContributorsConfig] = None,
) -> str:
    lines: list[str] = []
    repo_maintainers = contributors.repo_maintainers if contributors else {}

    # Known clients from config, alphabetized by display name
    client_repos = config.get("clients", {})
    active: list[tuple[str, str, str]] = []  # (input_key, config_key, display_name)
    for config_key, display_name in client_repos.items():
        input_key = _match_repo_input(config_key, repo_inputs)
        if input_key is not None:
            active.append((input_key, config_key, display_name))
    active.sort(key=lambda x: (repo_inputs[x[0]].client_name or x[2]).lower())

    placed_repos: set[str] = set()

    for input_key, config_key, display_name in active:
        placed_repos.add(input_key)
        repo_input = repo_inputs[input_key]
        stats = data.repo_stats.get(input_key) or data.repo_stats.get(config_key)
        names = repo_maintainers.get(config_key.lower(), [])
        lines.extend(_render_repo_block(repo_input, display_name, stats, names))

    # Other platforms: repos from "other" config + any unmatched .md files
    other_repos = config.get("other", {})

    # Repos in "other" config that have .md files
    other_active: list[tuple[str, str, str]] = []
    for config_key, display_name in other_repos.items():
        input_key = _match_repo_input(config_key, repo_inputs)
        if input_key is not None:
            other_active.append((input_key, config_key, display_name))
            placed_repos.add(input_key)
    other_active.sort(key=lambda x: (repo_inputs[x[0]].client_name or x[2]).lower())

    # Unmatched repos: .md files not in clients or other config
    unmatched = [
        (r, inp) for r, inp in repo_inputs.items() if r not in placed_repos
    ]
    unmatched.sort(key=lambda x: (x[1].client_name or x[0]).lower())

    has_other = other_repos or other_content or other_active or unmatched
    if has_other:
        section_title = other_title or "Other Platforms"
        lines.append(f"## {section_title}")
        lines.append("")

        # Aggregate stats for all "other" config repos
        other_closed = 0
        other_merged = 0
        other_unique = 0
        other_contributors: dict[str, int] = {}

        for config_key in other_repos:
            input_key = _match_repo_input(config_key, repo_inputs)
            stat_key = input_key or config_key
            stats = data.repo_stats.get(stat_key) or data.repo_stats.get(config_key)
            if stats:
                other_closed += stats.closed_issues
                other_merged += stats.merged_prs
                other_unique += stats.unique_contributors
                for author, count in stats.top_contributors:
                    other_contributors[author] = other_contributors.get(author, 0) + count

        for repo, _ in unmatched:
            stats = data.repo_stats.get(repo)
            if stats:
                other_closed += stats.closed_issues
                other_merged += stats.merged_prs
                other_unique += stats.unique_contributors
                for author, count in stats.top_contributors:
                    other_contributors[author] = other_contributors.get(author, 0) + count

        if other_closed or other_merged or other_unique:
            lines.append(f"_{other_closed} issues closed \u00b7 {other_merged} PRs merged \u00b7 {other_unique} contributors_")
            lines.append("")

        if other_contributors:
            top_other = sorted(other_contributors.items(), key=lambda x: x[1], reverse=True)[:3]
            lines.append("**Top contributors:** " + ", ".join(f"@{c[0]}" for c in top_other))
            lines.append("")

        # Prose from current/other.md
        if other_content:
            lines.append(other_content)
            lines.append("")

        # Individual blocks for "other" repos with .md files
        for input_key, config_key, display_name in other_active:
            repo_input = repo_inputs[input_key]
            stats = data.repo_stats.get(input_key) or data.repo_stats.get(config_key)
            names = repo_maintainers.get(config_key.lower(), [])
            lines.extend(_render_repo_block(repo_input, display_name, stats, names))

        # Individual blocks for unmatched repos
        for repo, repo_input in unmatched:
            fallback_name = repo.replace("jellyfin-", "").replace("-", " ").title()
            if not fallback_name.startswith("Jellyfin"):
                fallback_name = f"Jellyfin {fallback_name}"
            stats = data.repo_stats.get(repo)
            names = repo_maintainers.get(repo.lower(), [])
            lines.extend(_render_repo_block(repo_input, fallback_name, stats, names))

    return "\n".join(lines).rstrip()


def fill_template(template_path: str, placeholders: dict[str, str]) -> str:
    with open(template_path) as f:
        content = f.read()

    for key, value in placeholders.items():
        content = content.replace(f"{{{{{key}}}}}", value)

    return content


# MARK: - Archiving

def archive_current(current_dir: str, archive_dir: str, run_date: datetime) -> None:
    year = run_date.strftime("%Y")
    month_day = run_date.strftime("%m-%d")
    folder = f"{month_day}-state-of-the-fin"
    dest = os.path.join(archive_dir, year, folder)
    os.makedirs(dest, exist_ok=True)

    for filename in os.listdir(current_dir):
        if filename == ".gitkeep":
            continue
        src = os.path.join(current_dir, filename)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest, filename))
            os.remove(src)

    print(f"  Archived current/ to archive/{year}/{folder}/", file=sys.stderr)


# MARK: - CLI

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate State of the Fin blog post from current/ inputs and GitHub stats.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
The script collects data from the last published blog post date to now,
reads repo notes from current/, fetches GitHub stats, fills the template,
and outputs to blog/YEAR/MM-DD-state-of-the-fin/.

Examples:
  %(prog)s                          # Generate report using today's date
  %(prog)s --dry-run                # Preview output without writing files
  %(prog)s --no-archive             # Skip archiving current/ to archive/
  %(prog)s --date 2026-01-06        # Generate as if run on Jan 6, 2026
  %(prog)s --input-dir archive/...  # Read inputs from a different directory
        """,
    )
    parser.add_argument("--no-archive", action="store_true", help="Skip archiving current/ files after generation")
    parser.add_argument("--dry-run", action="store_true", help="Print output to stdout instead of writing files")
    parser.add_argument("--date", type=str, default=None, help="Override run date (YYYY-MM-DD). Defaults to today.")
    parser.add_argument("--input-dir", type=str, default=None, help="Override input directory. Defaults to current/.")
    parser.add_argument("--author", type=str, default=None, help="Override author from repos.json.")

    return parser.parse_args()


# MARK: - Main

def main() -> None:
    args = parse_args()

    config = load_config()
    org = config["org"]
    author = args.author or config["author"]

    if args.date:
        run_date = datetime.strptime(args.date, "%Y-%m-%d")
    else:
        run_date = datetime.now()
    start_date, end_date = auto_date_range(run_date)
    date_str = run_date.strftime("%Y-%m-%d")

    print(f"State of the Fin: {date_str}", file=sys.stderr)
    print(f"  Period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}", file=sys.stderr)

    # Read inputs
    if args.input_dir:
        input_dir = os.path.abspath(args.input_dir)
        print(f"  Input dir: {input_dir}", file=sys.stderr)
    else:
        input_dir = CURRENT_DIR
    repo_inputs, overview_content, other_title, other_content = read_current_inputs(input_dir)
    overview = parse_overview(overview_content)

    if not repo_inputs:
        print("Warning: No repo .md files found in current/. Sections will be empty.", file=sys.stderr)

    # Build the full repo list: clients + other config + any extra from current/
    all_repos: dict[str, str] = {}
    matched_inputs: set[str] = set()

    # Add client repos
    client_repos = config.get("clients", {})
    for config_key, display_name in client_repos.items():
        input_key = _match_repo_input(config_key, repo_inputs)
        if input_key is not None:
            all_repos[input_key] = repo_inputs[input_key].client_name or display_name
            matched_inputs.add(input_key)

    # Add "other" repos from config (always fetch stats for these)
    other_repos = config.get("other", {})
    for config_key, display_name in other_repos.items():
        input_key = _match_repo_input(config_key, repo_inputs)
        key = input_key or config_key
        all_repos[key] = display_name
        if input_key:
            matched_inputs.add(input_key)

    # Add unmatched repos from current/ not in any config
    for repo, inp in repo_inputs.items():
        if repo not in matched_inputs:
            if inp.client_name:
                all_repos[repo] = inp.client_name
            else:
                display_name = repo.replace("jellyfin-", "").replace("-", " ").title()
                if not display_name.startswith("Jellyfin"):
                    display_name = f"Jellyfin {display_name}"
                all_repos[repo] = display_name

    # Connect to GitHub: env var first, then fall back to gh CLI
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        try:
            result = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                token = result.stdout.strip()
                print("  Using token from gh CLI.", file=sys.stderr)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    if not token:
        print("Warning: No GitHub token found. API rate limits will be restricted.", file=sys.stderr)

    gh = Github(
        auth=Auth.Token(token) if token else None,
        per_page=100,
    )

    contributors_config = load_contributors()
    collector = DataCollector(gh, org, contributors_config)
    range_data = collector.collect_range_data(start_date, end_date, all_repos)

    # Build placeholders
    placeholders = {
        "DATE": date_str,
        "AUTHOR": author,
        "INTRODUCTION": overview.get("introduction", "[INTRODUCTION]"),
        "PROJECT_UPDATES": overview.get("project updates", "[PROJECT UPDATES]"),
        "ACTIVITY": generate_activity(range_data),
        "RELEASES": generate_releases(range_data),
        "DEVELOPMENT_UPDATES": overview.get("development updates", "[DEVELOPMENT UPDATES]"),
        "SECTIONS": generate_sections(config, range_data, repo_inputs, other_title, other_content, contributors_config),
        "SIGNOFF": overview.get("sign off", f"\\- {author} and the Jellyfin Team"),
    }

    mdx_content = fill_template(TEMPLATE_PATH, placeholders)

    if args.dry_run:
        print(mdx_content)
        return

    # Write report
    year = run_date.strftime("%Y")
    month_day = run_date.strftime("%m-%d")
    report_dir = os.path.join(BLOG_DIR, year, f"{month_day}-state-of-the-fin")
    os.makedirs(report_dir, exist_ok=True)

    output_path = os.path.join(report_dir, "index.mdx")
    with open(output_path, "w") as f:
        f.write(mdx_content)

    print(f"\nGenerated: {output_path}", file=sys.stderr)

    # Archive
    if not args.no_archive:
        archive_current(CURRENT_DIR, ARCHIVE_DIR, run_date)


if __name__ == "__main__":
    main()
