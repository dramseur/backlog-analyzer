#!/usr/bin/env python3
"""
Backlog Analyzer - Agile Coach AI
Analyzes a Jira backlog (from CSV export or Jira Cloud API) and produces
a structured coaching report, JSON data, and interactive HTML dashboard.

Usage:
    # Simplest: provide a board ID (auto-discovers sprints and builds tiered analysis):
    python backlog_analyzer.py --board-id 1131 --dashboard report.html

    # From CSV export:
    python backlog_analyzer.py --csv path/to/export.csv

    # From Jira Cloud API (by board name):
    python backlog_analyzer.py --jira-url https://your-org.atlassian.net --board "AIP Pipelines"

    # From Jira Cloud API (by team name):
    python backlog_analyzer.py --jira-url https://your-org.atlassian.net --team "AIP Pipelines" --project RHOAIENG

    Environment variables for Jira API:
        JIRA_URL       - Jira Cloud base URL (alternative to --jira-url)
        JIRA_EMAIL     - Email for Jira API authentication
        JIRA_API_TOKEN - API token for Jira API authentication
"""

import csv
import json
import os
import sys
import argparse
import urllib.request
import urllib.parse
import urllib.error
import base64
from datetime import datetime, timedelta
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import re
from typing import Optional


# ---------------------------------------------------------------------------
# Jira Cloud Field ID Mapping
# ---------------------------------------------------------------------------
# These are the custom field IDs for Red Hat's Atlassian Cloud instance.
# Override via environment variables if your instance differs.

JIRA_FIELD_STORY_POINTS = os.environ.get("JIRA_FIELD_STORY_POINTS", "customfield_10028")
JIRA_FIELD_EPIC_LINK = os.environ.get("JIRA_FIELD_EPIC_LINK", "customfield_10014")
JIRA_FIELD_TEAM = os.environ.get("JIRA_FIELD_TEAM", "customfield_10001")
JIRA_FIELD_SPRINT = os.environ.get("JIRA_FIELD_SPRINT", "customfield_10020")


# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------

@dataclass
class BacklogItem:
    key: str
    summary: str
    issue_type: str
    status: str
    priority: str
    assignee: str
    reporter: str
    created: Optional[datetime]
    updated: Optional[datetime]
    description: str
    story_points: Optional[float]
    epic_link: str
    labels: list
    components: list
    sprints: list
    has_acceptance_criteria: bool
    outward_links: list
    inward_links: list
    parent_id: str
    resolution: str


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_date(val: str) -> Optional[datetime]:
    val = val.strip()
    if not val:
        return None
    for fmt in ("%Y/%m/%d %I:%M %p", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(val, fmt)
        except ValueError:
            continue
    return None


def parse_float(val: str) -> Optional[float]:
    val = val.strip()
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def collect_multi_columns(row: dict, col_name: str) -> list:
    """Collect values from duplicate column names (Jira exports repeat column headers)."""
    values = []
    # csv.DictReader handles duplicate keys by overwriting, so we need the raw row
    # We stored extras during parsing
    if hasattr(row, '_multi'):
        for k, v in row._multi:
            if k == col_name and v.strip():
                values.append(v.strip())
    return values


def load_csv(path: str) -> tuple[list[BacklogItem], dict]:
    """Load a Jira CSV export into BacklogItem objects and extract metadata."""
    items = []
    meta = {"project_name": "", "team": ""}
    with open(path, newline='', encoding='utf-8-sig') as f:
        # Read raw to handle duplicate column headers
        reader = csv.reader(f)
        headers = next(reader)

        for raw_row in reader:
            if len(raw_row) < len(headers):
                raw_row.extend([''] * (len(headers) - len(raw_row)))

            row = dict(zip(headers, raw_row))
            # Build multi-value lookup
            multi = defaultdict(list)
            for h, v in zip(headers, raw_row):
                if v.strip():
                    multi[h].append(v.strip())

            # Capture project/team metadata from first row
            if not meta["project_name"]:
                meta["project_name"] = row.get('Project name', '').strip()
                team_val = row.get('Custom field (Team)', '').strip()
                # Only use the custom field if it looks like a name, not a numeric ID
                if team_val and not team_val.isdigit():
                    meta["team"] = team_val

            desc = row.get('Description', '')
            ac_keywords = ['acceptance criteria', 'given ', 'when ', 'then ',
                           'definition of done', 'expected result', 'expected behavior']
            has_ac = any(kw in desc.lower() for kw in ac_keywords)

            # Collect link columns
            outward = []
            inward = []
            for h, vals in multi.items():
                if 'outward issue link' in h.lower():
                    outward.extend(vals)
                elif 'inward issue link' in h.lower():
                    inward.extend(vals)

            item = BacklogItem(
                key=row.get('Issue key', ''),
                summary=row.get('Summary', ''),
                issue_type=row.get('Issue Type', ''),
                status=row.get('Status', ''),
                priority=row.get('Priority', ''),
                assignee=row.get('Assignee', '').strip(),
                reporter=row.get('Reporter', '').strip(),
                created=parse_date(row.get('Created', '')),
                updated=parse_date(row.get('Updated', '')),
                description=desc,
                story_points=parse_float(row.get('Custom field (Story Points)', '')),
                epic_link=row.get('Custom field (Epic Link)', '').strip(),
                labels=multi.get('Labels', []),
                components=multi.get('Component/s', []),
                sprints=multi.get('Sprint', []),
                has_acceptance_criteria=has_ac,
                outward_links=outward,
                inward_links=inward,
                parent_id=row.get('Parent id', '').strip(),
                resolution=row.get('Resolution', '').strip(),
            )
            items.append(item)

    # Derive team name: custom field > CSV filename > project name > fallback
    if not meta["team"]:
        basename = os.path.splitext(os.path.basename(path))[0]
        # Strip common suffixes like "- Full Backlog Export", "Backlog", "Export"
        clean = basename
        for suffix in [' - Full Backlog Export', ' - Backlog Export', 'Backlog', 'Export', '_']:
            clean = clean.replace(suffix, '')
        clean = clean.strip(' -_')
        meta["team"] = clean if clean else meta["project_name"] or "Unknown Team"

    return items, meta


# ---------------------------------------------------------------------------
# Jira Cloud API
# ---------------------------------------------------------------------------

class JiraClient:
    """Lightweight Jira Cloud REST API client using only stdlib."""

    def __init__(self, base_url: str, email: str, api_token: str):
        self.base_url = base_url.rstrip('/')
        credentials = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, data: dict = None) -> dict:
        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else ""
            if e.code == 401:
                print(f"Jira API authentication failed (401). Check JIRA_EMAIL and JIRA_API_TOKEN.",
                      file=sys.stderr)
                print(f"  URL: {url}", file=sys.stderr)
            elif e.code == 403:
                print(f"Jira API permission denied (403). Your token may lack access to this resource.",
                      file=sys.stderr)
            else:
                print(f"Jira API error {e.code}: {error_body}", file=sys.stderr)
            raise

    def get(self, path: str) -> dict:
        return self._request("GET", path)

    def post(self, path: str, data: dict) -> dict:
        return self._request("POST", path, data)

    def search_issues(self, jql: str, fields: list[str], max_results: int = 100) -> list[dict]:
        """Search issues using the v3 search/jql endpoint with pagination."""
        all_issues = []
        start_at = 0
        while True:
            path = (f"/rest/api/3/search/jql?"
                    f"jql={urllib.parse.quote(jql)}"
                    f"&startAt={start_at}"
                    f"&maxResults={max_results}"
                    f"&fields={','.join(fields)}")
            result = self.get(path)
            issues = result.get("issues", [])
            all_issues.extend(issues)
            total = result.get("total", 0)
            print(f"  Fetched {len(all_issues)}/{total} issues...", file=sys.stderr)
            if len(all_issues) >= total or not issues:
                break
            start_at += len(issues)
        return all_issues

    def find_board_by_name(self, name: str) -> Optional[dict]:
        """Find an agile board by name (fuzzy match)."""
        encoded = urllib.parse.quote(name)
        result = self.get(f"/rest/agile/1.0/board?name={encoded}&maxResults=10")
        boards = result.get("values", [])
        if not boards:
            return None
        # Prefer exact match, fall back to first result
        for b in boards:
            if b["name"].lower() == name.lower():
                return b
        return boards[0]

    def get_board_jql(self, board_id: int) -> str:
        """Get the filter JQL associated with a board."""
        board = self.get(f"/rest/agile/1.0/board/{board_id}/configuration")
        filter_id = board.get("filter", {}).get("id")
        if filter_id:
            filt = self.get(f"/rest/api/3/filter/{filter_id}")
            return filt.get("jql", "")
        return ""

    def get_board_name(self, board_id: int) -> str:
        """Get the display name of a board."""
        result = self.get(f"/rest/agile/1.0/board/{board_id}")
        return result.get("name", f"Board {board_id}")

    def get_board_sprints(self, board_id: int, states: list[str] = None) -> list[dict]:
        """Get sprints for a board, optionally filtered by state (active, future, closed)."""
        params = "maxResults=50"
        if states:
            params += f"&state={','.join(states)}"
        result = self.get(f"/rest/agile/1.0/board/{board_id}/sprint?{params}")
        return result.get("values", [])

    def get_sprint_issue_keys(self, sprint_id: int) -> list[str]:
        """Get all issue keys in a sprint."""
        all_keys = []
        start_at = 0
        while True:
            result = self.get(
                f"/rest/agile/1.0/sprint/{sprint_id}/issue"
                f"?startAt={start_at}&maxResults=100&fields=summary"
            )
            issues = result.get("issues", [])
            all_keys.extend(i["key"] for i in issues)
            total = result.get("total", 0)
            if start_at + len(issues) >= total or not issues:
                break
            start_at += len(issues)
        return all_keys


def _parse_jira_datetime(val) -> Optional[datetime]:
    """Parse ISO 8601 datetime strings from Jira Cloud API."""
    if not val:
        return None
    if isinstance(val, str):
        # Jira returns: "2024-03-15T10:30:00.000+0000" or similar
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                     "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(val[:26], fmt[:len(fmt)])
            except ValueError:
                continue
        # Fallback: just parse the date portion
        try:
            return datetime.strptime(val[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return None


def _extract_text_from_adf(adf_node) -> str:
    """Extract plain text from Atlassian Document Format (ADF) content."""
    if not adf_node or not isinstance(adf_node, dict):
        return ""
    text_parts = []
    if adf_node.get("type") == "text":
        text_parts.append(adf_node.get("text", ""))
    for child in adf_node.get("content", []):
        text_parts.append(_extract_text_from_adf(child))
    return " ".join(text_parts)


def _safe_str(val, key=None) -> str:
    """Safely extract a string from a Jira field value (which may be a dict)."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        # Common patterns: {"name": "..."}, {"displayName": "..."}, {"value": "..."}
        return val.get("name", val.get("displayName", val.get("value", val.get("key", ""))))
    return str(val)


def load_from_jira(client: JiraClient, jql: str, team_name: str) -> tuple[list['BacklogItem'], dict]:
    """Load backlog items from Jira Cloud API."""
    fields = [
        "summary", "issuetype", "status", "priority", "assignee", "reporter",
        "created", "updated", "description", "labels", "components",
        "resolution", "issuelinks", "parent",
        JIRA_FIELD_STORY_POINTS, JIRA_FIELD_EPIC_LINK, JIRA_FIELD_SPRINT,
    ]

    print(f"Searching Jira: {jql}", file=sys.stderr)
    issues = client.search_issues(jql, fields)

    items = []
    project_name = ""
    ac_keywords = ['acceptance criteria', 'given ', 'when ', 'then ',
                   'definition of done', 'expected result', 'expected behavior']

    for issue in issues:
        f = issue.get("fields", {})
        key = issue.get("key", "")

        if not project_name and "-" in key:
            project_name = key.split("-")[0]

        # Extract description text from ADF
        desc_field = f.get("description")
        if isinstance(desc_field, dict):
            desc = _extract_text_from_adf(desc_field)
        else:
            desc = desc_field or ""

        has_ac = any(kw in desc.lower() for kw in ac_keywords)

        # Extract issue links (handle both camelCase from Jira API and snake_case from MCP tools)
        outward = []
        inward = []
        for link in f.get("issuelinks", []) or []:
            link_type = link.get("type", {}).get("name", "")
            out_issue = link.get("outwardIssue") or link.get("outward_issue")
            in_issue = link.get("inwardIssue") or link.get("inward_issue")
            if out_issue:
                outward.append(f"{link_type}: {out_issue.get('key', '')}")
            if in_issue:
                inward.append(f"{link_type}: {in_issue.get('key', '')}")

        # Extract components
        components = [c.get("name", "") for c in (f.get("components") or [])]

        # Extract labels
        labels = f.get("labels") or []

        # Extract sprint names
        sprints = []
        sprint_field = f.get(JIRA_FIELD_SPRINT) or []
        for s in sprint_field:
            if isinstance(s, dict):
                sprints.append(s.get("name", ""))
            elif isinstance(s, str):
                sprints.append(s)

        # Story points
        sp_val = f.get(JIRA_FIELD_STORY_POINTS)
        story_points = float(sp_val) if sp_val is not None else None

        # Epic link
        epic_link = _safe_str(f.get(JIRA_FIELD_EPIC_LINK))

        # Parent (for subtasks / child issues in next-gen projects)
        parent = f.get("parent", {})
        parent_id = parent.get("key", "") if parent else ""

        item = BacklogItem(
            key=key,
            summary=f.get("summary", ""),
            issue_type=_safe_str(f.get("issuetype")),
            status=_safe_str(f.get("status")),
            priority=_safe_str(f.get("priority")),
            assignee=_safe_str(f.get("assignee")),
            reporter=_safe_str(f.get("reporter")),
            created=_parse_jira_datetime(f.get("created")),
            updated=_parse_jira_datetime(f.get("updated")),
            description=desc,
            story_points=story_points,
            epic_link=epic_link,
            labels=labels,
            components=components,
            sprints=sprints,
            has_acceptance_criteria=has_ac,
            outward_links=outward,
            inward_links=inward,
            parent_id=parent_id,
            resolution=_safe_str(f.get("resolution")),
        )
        items.append(item)

    meta = {
        "project_name": project_name,
        "team": team_name,
    }
    return items, meta


def build_jql(project: str = None, team: str = None, board_jql: str = None,
              component: str = None, status_exclude: list[str] = None) -> str:
    """Build a JQL query string from parameters."""
    clauses = []

    if board_jql:
        # Strip any ORDER BY from the board's JQL before wrapping it
        board_jql_clean = re.sub(r'\s+ORDER\s+BY\s+.*$', '', board_jql, flags=re.IGNORECASE)
        clauses.append(f"({board_jql_clean})")

    if project:
        clauses.append(f'project = "{project}"')

    if team:
        # Team field is an Atlassian Team type - search by team name
        clauses.append(f'"Team[Team]" = "{team}"')

    if component:
        clauses.append(f'component = "{component}"')

    if status_exclude is not None:
        for status in status_exclude:
            clauses.append(f'status != "{status}"')
    elif status_exclude is None:
        # Default: exclude Done/Closed items to focus on active backlog
        clauses.append('statusCategory != "Done"')

    jql = " AND ".join(clauses)
    jql += " ORDER BY priority ASC, updated DESC"
    return jql


# ---------------------------------------------------------------------------
# Analysis Functions
# ---------------------------------------------------------------------------

def days_since(dt: Optional[datetime], ref: datetime) -> Optional[int]:
    if dt is None:
        return None
    return (ref - dt).days


def score_ai_readiness(item: BacklogItem) -> int:
    """Score 1-5 how ready an item is for AI-assisted work."""
    score = 1
    desc_len = len(item.description.strip())

    # Description quality
    if desc_len > 500:
        score += 1
    if desc_len > 200:
        score += 1

    # Acceptance criteria present
    if item.has_acceptance_criteria:
        score += 1

    # Linked to epic / has context
    if item.epic_link or item.parent_id:
        score += 1

    return min(score, 5)


def score_ai_code_gen_readiness(item: BacklogItem) -> tuple[int, list[str]]:
    """Score 1-5 how ready an item is for AI-automated code generation.
    Returns (score, list_of_met_criteria).
    """
    criteria_met = []
    desc = item.description.strip()
    desc_lower = desc.lower()

    # 1. Technical context in description (component names, file paths, API endpoints, code references)
    tech_patterns = [
        '.py', '.go', '.java', '.js', '.ts', '.yaml', '.yml', '.json',
        'api/', '/api', 'endpoint', 'func ', 'function ', 'method ',
        'class ', 'struct ', 'interface ', 'module ', 'package ',
        'http://', 'https://', 'get ', 'post ', 'put ', 'delete ',
        'config', 'controller', 'handler', 'service', 'repository',
        'database', 'schema', 'migration', 'dockerfile', 'pipeline',
        'cmd/', 'pkg/', 'src/', 'lib/', 'test/', 'spec/',
        '()', '->', '=>', 'import ', 'from ', 'return ',
    ]
    has_technical = any(p in desc_lower for p in tech_patterns)
    if has_technical:
        criteria_met.append("Technical context")

    # 2. Testable acceptance criteria (stricter - requires structured format)
    strict_ac_patterns = ['given ', 'when ', 'then ', 'expected result',
                          'expected behavior', 'expected output', 'should return',
                          'should fail', 'should succeed', 'returns ', 'responds with']
    has_strict_ac = sum(1 for p in strict_ac_patterns if p in desc_lower) >= 2
    if has_strict_ac:
        criteria_met.append("Testable acceptance criteria")

    # 3. Bounded scope (not too large, not an epic/initiative)
    is_bounded = (
        item.issue_type not in ('Epic', 'Initiative') and
        (item.story_points is None or item.story_points <= 5)
    )
    if is_bounded:
        criteria_met.append("Bounded scope")

    # 4. Component/label mapping (has at least one component or label)
    has_component = bool(item.components) or bool(item.labels)
    if has_component:
        criteria_met.append("Component/label mapped")

    # 5. No blocking dependencies
    has_blockers = any('block' in link.lower() for link in item.inward_links)
    if not has_blockers:
        criteria_met.append("No blocking dependencies")

    # 6. Has story point estimate (scoped and estimated)
    if item.story_points is not None:
        criteria_met.append("Estimated")

    # Score: base 1, +1 for each criterion met (6 criteria, cap at 5)
    # Weight: technical context and AC are most important
    score = 1
    if has_technical:
        score += 1
    if has_strict_ac:
        score += 1
    if is_bounded and has_component:
        score += 1
    if not has_blockers and item.story_points is not None:
        score += 1

    return min(score, 5), criteria_met


def classify_concerns(item: BacklogItem, ref_date: datetime) -> list[str]:
    """Return list of concern tags for an item."""
    concerns = []

    # Too large
    if item.story_points and item.story_points >= 8:
        concerns.append("too_large")

    # Missing description
    if len(item.description.strip()) < 30:
        concerns.append("missing_description")

    # Missing acceptance criteria
    if not item.has_acceptance_criteria:
        concerns.append("missing_acceptance_criteria")

    # Stale (not updated in 180+ days)
    age = days_since(item.updated, ref_date)
    if age and age > 180:
        concerns.append("stale")

    # Undefined priority
    if item.priority in ('Undefined', ''):
        concerns.append("undefined_priority")

    # Not linked to epic
    if not item.epic_link and not item.parent_id:
        concerns.append("no_epic_link")

    # No story points
    if item.story_points is None:
        concerns.append("no_story_points")

    return concerns


# ---------------------------------------------------------------------------
# Report Generation
# ---------------------------------------------------------------------------

def generate_report(items: list[BacklogItem], ref_date: datetime) -> str:
    lines = []

    def h1(t): lines.append(f"\n# {t}\n")
    def h2(t): lines.append(f"\n## {t}\n")
    def h3(t): lines.append(f"\n### {t}\n")
    def p(t): lines.append(f"{t}\n")
    def bullet(t): lines.append(f"- {t}")
    def nl(): lines.append("")

    # Pre-compute analysis
    type_counts = Counter(i.issue_type for i in items)
    status_counts = Counter(i.status for i in items)
    priority_counts = Counter(i.priority for i in items)

    has_desc = sum(1 for i in items if len(i.description.strip()) > 30)
    has_ac = sum(1 for i in items if i.has_acceptance_criteria)
    has_sp = sum(1 for i in items if i.story_points is not None)
    has_epic = sum(1 for i in items if i.epic_link or i.parent_id)
    has_assignee = sum(1 for i in items if i.assignee)

    stale_items = [i for i in items if days_since(i.updated, ref_date) and days_since(i.updated, ref_date) > 180]
    large_items = [i for i in items if i.story_points and i.story_points >= 8]
    no_desc_items = [i for i in items if len(i.description.strip()) < 30]
    no_ac_items = [i for i in items if not i.has_acceptance_criteria]
    no_epic_items = [i for i in items if not i.epic_link and not i.parent_id]
    undef_priority_bugs = [i for i in items if i.issue_type == 'Bug' and i.priority in ('Undefined', '')]

    # AI readiness scores
    ai_scores = {i.key: score_ai_readiness(i) for i in items}
    avg_ai = sum(ai_scores.values()) / len(ai_scores) if ai_scores else 0

    # Concern classification
    all_concerns = {i.key: classify_concerns(i, ref_date) for i in items}
    concern_counts = Counter()
    for concerns in all_concerns.values():
        concern_counts.update(concerns)

    # Backlog readiness score
    readiness_factors = []
    readiness_factors.append(has_desc / len(items))       # description coverage
    readiness_factors.append(has_ac / len(items))          # acceptance criteria
    readiness_factors.append(has_sp / len(items))          # story points
    readiness_factors.append(1 - len(stale_items) / len(items))  # freshness
    readiness_factors.append(has_epic / len(items))        # epic linkage
    raw_readiness = sum(readiness_factors) / len(readiness_factors)
    readiness_score = round(1 + raw_readiness * 4, 1)  # Scale 1-5

    # Items with dependencies
    items_with_deps = [i for i in items if i.outward_links or i.inward_links]

    # SP distribution
    sp_values = [i.story_points for i in items if i.story_points is not None]
    sp_counter = Counter(sp_values)

    # ===================================================================
    # 1. EXECUTIVE SUMMARY
    # ===================================================================
    h1("Backlog Analysis Report")
    p(f"**Analysis Date:** {ref_date.strftime('%Y-%m-%d')}")
    p(f"**Total Backlog Items:** {len(items)}")
    nl()

    h2("1. Executive Summary")
    nl()
    p(f"The backlog contains **{len(items)} items** ({type_counts.get('Story',0)} Stories, "
      f"{type_counts.get('Task',0)} Tasks, {type_counts.get('Bug',0)} Bugs, "
      f"{type_counts.get('Initiative',0)} Initiatives, {type_counts.get('Sub-task',0)} Sub-tasks).")
    nl()

    p("**Overall Quality Assessment:**")
    bullet(f"**Description Coverage:** {has_desc}/{len(items)} items ({has_desc*100//len(items)}%) have meaningful descriptions")
    bullet(f"**Acceptance Criteria:** Only {has_ac}/{len(items)} items ({has_ac*100//len(items)}%) contain acceptance criteria")
    bullet(f"**Story Points:** {has_sp}/{len(items)} items ({has_sp*100//len(items)}%) are estimated")
    bullet(f"**Epic Linkage:** {has_epic}/{len(items)} items ({has_epic*100//len(items)}%) are linked to epics or parents")
    bullet(f"**Stale Items:** {len(stale_items)}/{len(items)} ({len(stale_items)*100//len(items)}%) not updated in 6+ months")
    nl()

    p("**Backlog Readiness Score:** {:.1f} / 5.0".format(readiness_score))
    p("**Average AI-Readiness Score:** {:.1f} / 5.0".format(avg_ai))
    nl()

    p("**Top Concerns:**")
    top_concerns = concern_counts.most_common(5)
    concern_labels = {
        "no_epic_link": "Items not linked to epics/goals",
        "missing_acceptance_criteria": "Items missing acceptance criteria",
        "stale": "Stale items (not updated in 6+ months)",
        "undefined_priority": "Items with undefined priority",
        "no_story_points": "Items without story point estimates",
        "missing_description": "Items with missing/minimal descriptions",
        "too_large": "Items too large for a single sprint",
    }
    for concern, count in top_concerns:
        bullet(f"**{concern_labels.get(concern, concern)}:** {count} items ({count*100//len(items)}%)")
    nl()

    # ===================================================================
    # 2. KEY BACKLOG ITEM CONCERNS
    # ===================================================================
    h2("2. Key Backlog Item Concerns")
    nl()

    # Large items
    if large_items:
        h3("Too Large / Needs Splitting")
        p(f"**{len(large_items)} items** are estimated at 8+ story points and likely need decomposition:")
        nl()
        p("| Key | Type | SP | Summary |")
        p("|-----|------|-----|---------|")
        for i in sorted(large_items, key=lambda x: x.story_points or 0, reverse=True):
            p(f"| {i.key} | {i.issue_type} | {i.story_points:.0f} | {i.summary[:80]} |")
        nl()

    # Missing descriptions
    if no_desc_items:
        h3("Poorly Defined / Lacking Detail")
        p(f"**{len(no_desc_items)} items** have missing or minimal descriptions:")
        nl()
        p("| Key | Type | Summary |")
        p("|-----|------|---------|")
        for i in no_desc_items:
            p(f"| {i.key} | {i.issue_type} | {i.summary[:80]} |")
        nl()

    # Bugs with undefined priority
    if undef_priority_bugs:
        h3("Bugs with Undefined Priority")
        p(f"**{len(undef_priority_bugs)} bugs** have no priority set -- these need triage:")
        nl()
        p("| Key | Summary |")
        p("|-----|---------|")
        for i in undef_priority_bugs:
            p(f"| {i.key} | {i.summary[:80]} |")
        nl()

    # Stale items (top 15)
    if stale_items:
        stale_sorted = sorted(stale_items, key=lambda x: days_since(x.updated, ref_date) or 0, reverse=True)
        h3("Stale Items (Not Updated in 6+ Months)")
        p(f"**{len(stale_items)} items** ({len(stale_items)*100//len(items)}% of backlog) have not been updated in over 6 months. "
          f"Top {min(15, len(stale_sorted))} oldest:")
        nl()
        p("| Key | Days Since Update | Type | Summary |")
        p("|-----|-------------------|------|---------|")
        for i in stale_sorted[:15]:
            age = days_since(i.updated, ref_date)
            p(f"| {i.key} | {age} | {i.issue_type} | {i.summary[:60]} |")
        nl()

    # No epic link
    h3("Items Not Linked to Epics or Goals")
    p(f"**{len(no_epic_items)} items** ({len(no_epic_items)*100//len(items)}%) are not linked to any epic or parent, "
      f"making it difficult to trace work to strategic objectives.")
    nl()

    # Critical items requiring immediate refinement
    h3("Critical Items Requiring Immediate Refinement")
    critical = []
    for i in items:
        c = all_concerns[i.key]
        # Flag items with 4+ concerns as critical
        if len(c) >= 4:
            critical.append((i, c))
    critical.sort(key=lambda x: len(x[1]), reverse=True)

    if critical:
        p(f"**{len(critical)} items** have 4+ quality issues and need immediate attention:")
        nl()
        p("| Key | Type | Concerns | Summary |")
        p("|-----|------|----------|---------|")
        for i, c in critical[:20]:
            p(f"| {i.key} | {i.issue_type} | {', '.join(c)} | {i.summary[:50]} |")
        nl()
    else:
        p("No items flagged with 4+ simultaneous concerns.")
        nl()

    # ===================================================================
    # 3. STORY SIZE & DECOMPOSITION SIGNALS
    # ===================================================================
    h2("3. Story Size & Decomposition Signals")
    nl()

    p("**Story Point Distribution:**")
    nl()
    p("| Points | Count | % of Estimated |")
    p("|--------|-------|----------------|")
    for sp_val in sorted(sp_counter.keys()):
        cnt = sp_counter[sp_val]
        pct = cnt * 100 // len(sp_values) if sp_values else 0
        bar = "#" * (pct // 2)
        p(f"| {sp_val:.0f} | {cnt} | {pct}% {bar} |")
    nl()
    not_estimated = len(items) - len(sp_values)
    p(f"**Not Estimated:** {not_estimated} items ({not_estimated*100//len(items)}%)")
    nl()

    p("**Decomposition Recommendations:**")
    if large_items:
        for i in large_items:
            bullet(f"**{i.key}** ({i.story_points:.0f} SP) - \"{i.summary[:60]}\" -- Consider splitting into smaller deliverables")
    nl()

    # Initiatives that may need breakdown
    initiatives = [i for i in items if i.issue_type == 'Initiative']
    if initiatives:
        p("**Initiatives Needing Further Breakdown:**")
        for i in initiatives:
            bullet(f"**{i.key}** - \"{i.summary[:70]}\"")
        nl()

    # Stories with 5 SP
    medium_large = [i for i in items if i.story_points and i.story_points == 5]
    if medium_large:
        p(f"**{len(medium_large)} items at 5 SP** -- Review these during refinement for potential splitting.")
    nl()

    # ===================================================================
    # 4. DEPENDENCY SIGNALS
    # ===================================================================
    h2("4. Dependency Signals")
    nl()

    p(f"**{len(items_with_deps)} items** ({len(items_with_deps)*100//len(items)}%) have issue links (blocks, depends on, relates to).")
    nl()

    # Items that block others
    blockers = [i for i in items if i.outward_links and any('block' in str(l).lower() for l in i.outward_links)]
    blocked = [i for i in items if i.inward_links and any('block' in str(l).lower() for l in i.inward_links)]

    if items_with_deps:
        p("**Items with the Most Links (potential dependency bottlenecks):**")
        nl()
        dep_sorted = sorted(items_with_deps, key=lambda x: len(x.outward_links) + len(x.inward_links), reverse=True)
        p("| Key | Outward Links | Inward Links | Summary |")
        p("|-----|---------------|--------------|---------|")
        for i in dep_sorted[:10]:
            p(f"| {i.key} | {len(i.outward_links)} | {len(i.inward_links)} | {i.summary[:60]} |")
        nl()

    p("**Recommendations:**")
    bullet("Map dependency chains before sprint planning to avoid blocked work")
    bullet("Prioritize items that unblock the most downstream work")
    bullet("Flag cross-team dependencies early in refinement")
    nl()

    # ===================================================================
    # 5. BACKLOG ORGANIZATION & STRUCTURE
    # ===================================================================
    h2("5. Backlog Organization & Structure")
    nl()

    p("**Status Distribution:**")
    nl()
    p("| Status | Count | % |")
    p("|--------|-------|---|")
    for status, cnt in status_counts.most_common():
        p(f"| {status} | {cnt} | {cnt*100//len(items)}% |")
    nl()

    new_pct = status_counts.get('New', 0) * 100 // len(items)
    if new_pct > 80:
        p(f"**Warning:** {new_pct}% of items are in 'New' status. This suggests items may not be going "
          f"through a proper triage or refinement workflow.")
        nl()

    p("**Priority Distribution:**")
    nl()
    p("| Priority | Count | % |")
    p("|----------|-------|---|")
    for pri, cnt in priority_counts.most_common():
        p(f"| {pri} | {cnt} | {cnt*100//len(items)}% |")
    nl()

    undef_pct = priority_counts.get('Undefined', 0) * 100 // len(items)
    if undef_pct > 40:
        p(f"**Warning:** {undef_pct}% of items have 'Undefined' priority. Prioritization needs attention.")
        nl()

    p("**Observations:**")
    bullet(f"**Epic Linkage Gap:** {len(no_epic_items)}/{len(items)} items lack epic/parent links -- work cannot be traced to strategic goals")
    if len(stale_items) > len(items) // 3:
        bullet(f"**Backlog Hygiene:** {len(stale_items)*100//len(items)}% of the backlog is stale -- a grooming session to archive or close old items is recommended")
    unassigned_bugs = sum(1 for i in items if i.issue_type == 'Bug' and not i.assignee)
    if unassigned_bugs:
        bullet(f"**Unassigned Bugs:** {unassigned_bugs} bugs have no assignee")
    nl()

    # Potential duplicates (simple title similarity)
    p("**Potential Duplicates / Overlapping Items:**")
    p("Items with similar summaries that may overlap (manual review recommended):")
    nl()
    # Simple word-overlap check
    dupes_found = find_potential_duplicates(items)
    if dupes_found:
        for pair_key, pair_summary, other_key, other_summary, sim in dupes_found[:10]:
            bullet(f"**{pair_key}** / **{other_key}** (similarity: {sim:.0%})")
            p(f"  - \"{pair_summary[:60]}\"")
            p(f"  - \"{other_summary[:60]}\"")
        nl()
    else:
        p("No obvious duplicates detected by title similarity.")
        nl()

    # ===================================================================
    # 6. BACKLOG READINESS SCORE
    # ===================================================================
    h2("6. Backlog Readiness Score")
    nl()

    p("| Factor | Score | Details |")
    p("|--------|-------|---------|")
    p(f"| Description Quality | {has_desc*100//len(items)}% | {has_desc}/{len(items)} items have meaningful descriptions |")
    p(f"| Acceptance Criteria | {has_ac*100//len(items)}% | {has_ac}/{len(items)} items have AC |")
    p(f"| Story Point Estimates | {has_sp*100//len(items)}% | {has_sp}/{len(items)} items estimated |")
    p(f"| Freshness | {(len(items)-len(stale_items))*100//len(items)}% | {len(items)-len(stale_items)}/{len(items)} items updated within 6 months |")
    p(f"| Epic Linkage | {has_epic*100//len(items)}% | {has_epic}/{len(items)} items linked to epics |")
    nl()

    score_label = {1: "Poorly defined / not ready", 2: "Below average readiness",
                   3: "Moderately ready", 4: "Good readiness", 5: "Well structured / ready for sprint planning"}
    label = score_label.get(round(readiness_score), score_label[3])
    p(f"### Overall Backlog Readiness Score: {readiness_score:.1f} / 5.0 -- {label}")
    nl()

    # ===================================================================
    # 7. AI-READINESS ASSESSMENT
    # ===================================================================
    h2("7. AI-Readiness Assessment")
    nl()

    p(f"**Average AI-Readiness Score:** {avg_ai:.1f} / 5.0")
    nl()

    ai_dist = Counter(ai_scores.values())
    p("**AI-Readiness Distribution:**")
    nl()
    p("| Score | Count | % | Interpretation |")
    p("|-------|-------|---|----------------|")
    ai_labels = {1: "AI cannot act", 2: "Minimal AI use", 3: "AI can suggest with limited accuracy",
                 4: "AI can generate useful outputs", 5: "AI can fully generate subtasks/AC/splits"}
    for s in range(1, 6):
        cnt = ai_dist.get(s, 0)
        p(f"| {s} | {cnt} | {cnt*100//len(items)}% | {ai_labels[s]} |")
    nl()

    # Items least ready for AI
    low_ai = [(k, v) for k, v in ai_scores.items() if v <= 2]
    if low_ai:
        p(f"**{len(low_ai)} items scored 1-2** (AI cannot meaningfully act). Common missing elements:")
        nl()
        missing_elements = Counter()
        for i in items:
            if ai_scores[i.key] <= 2:
                if not i.has_acceptance_criteria:
                    missing_elements["Missing acceptance criteria"] += 1
                if len(i.description.strip()) < 100:
                    missing_elements["Insufficient description"] += 1
                if not i.epic_link and not i.parent_id:
                    missing_elements["No epic/goal linkage"] += 1
        for elem, cnt in missing_elements.most_common():
            bullet(f"**{elem}:** {cnt} items")
        nl()

    # Top AI-ready items
    high_ai = sorted([(i, ai_scores[i.key]) for i in items if ai_scores[i.key] >= 4],
                      key=lambda x: x[1], reverse=True)
    if high_ai:
        p(f"**{len(high_ai)} items scored 4-5** (AI-ready). These are good candidates for AI-assisted refinement:")
        nl()
        p("| Key | Score | Type | Summary |")
        p("|-----|-------|------|---------|")
        for i, sc in high_ai[:10]:
            p(f"| {i.key} | {sc} | {i.issue_type} | {i.summary[:60]} |")
        nl()

    p("**Recommendations to Improve AI-Readiness:**")
    bullet("Add structured acceptance criteria using Given/When/Then format")
    bullet("Link all items to epics to provide business context")
    bullet("Include clear scope boundaries (in-scope / out-of-scope)")
    bullet("Use a standard story template with Description, AC, Dependencies, and Business Value sections")
    bullet("Add technical context (affected components, APIs, data models) for AI to generate accurate subtasks")
    nl()

    # ===================================================================
    # 8. RECOMMENDED ACTIONS
    # ===================================================================
    h2("8. Recommended Actions")
    nl()

    h3("Immediate Actions (This Sprint)")
    bullet(f"**Triage stale items:** Review and close/archive {len(stale_items)} items not updated in 6+ months")
    bullet(f"**Prioritize bugs:** Set priority on {len(undef_priority_bugs)} bugs with undefined priority")
    bullet(f"**Split large items:** Decompose {len(large_items)} items estimated at 8+ story points")
    bullet(f"**Add descriptions:** Write descriptions for {len(no_desc_items)} items with missing/minimal content")
    nl()

    h3("Short-Term Actions (Next 2-3 Sprints)")
    bullet(f"**Add acceptance criteria:** {len(no_ac_items)} items ({len(no_ac_items)*100//len(items)}%) lack AC")
    bullet(f"**Link to epics:** Connect {len(no_epic_items)} orphaned items to strategic epics/goals")
    bullet(f"**Estimate unpointed items:** {not_estimated} items need story point estimates")
    bullet("**Review potential duplicates:** Merge or link overlapping items identified above")
    nl()

    h3("Automatable Actions")
    p("The following can be automated with Jira automation rules or AI scripts:")
    nl()
    p("| Action | Automation Approach | Effort |")
    p("|--------|---------------------|--------|")
    p("| Detect stale items | Jira automation: flag items not updated in 90/180 days | Low |")
    p("| Missing AC check | Jira automation: label items without 'acceptance criteria' in description | Low |")
    p("| Priority enforcement | Jira workflow: require priority on bug creation | Low |")
    p("| Duplicate detection | AI script: NLP similarity analysis on summaries/descriptions | Medium |")
    p("| Story splitting suggestions | AI script: analyze large stories and suggest decomposition | Medium |")
    p("| AC generation | AI script: draft acceptance criteria from description context | Medium |")
    p("| Stale item notifications | Jira automation: send Slack/email digest of aging items | Low |")
    p("| Epic linkage enforcement | Jira workflow: require epic link before moving to 'Ready' | Low |")
    nl()

    # ===================================================================
    # 9. AGILE COACHING INSIGHTS
    # ===================================================================
    h2("9. Agile Coaching Insights")
    nl()

    h3("Writing Better User Stories")
    p("Many items in this backlog lack the structure needed for effective sprint planning. "
      "Adopt the following template for all new stories:")
    nl()
    p("```")
    p("**As a** [persona],")
    p("**I want** [capability],")
    p("**So that** [business value].")
    p("")
    p("**Acceptance Criteria:**")
    p("- Given [context], When [action], Then [expected outcome]")
    p("- Given [context], When [action], Then [expected outcome]")
    p("")
    p("**Scope:**")
    p("- In scope: [list]")
    p("- Out of scope: [list]")
    p("")
    p("**Dependencies:** [list or 'None']")
    p("**Technical Notes:** [any implementation guidance]")
    p("```")
    nl()

    h3("Improving Acceptance Criteria")
    p(f"Only {has_ac*100//len(items)}% of items have acceptance criteria. This is a major gap. "
      "Without AC, teams cannot validate 'done' and AI tools cannot generate meaningful subtasks.")
    nl()
    p("**Exercise:** In your next refinement session, take the top 5 items by priority and "
      "collaboratively write Given/When/Then acceptance criteria for each. "
      "Time-box to 5 minutes per item.")
    nl()

    h3("Breaking Down Large Items")
    p("Use these splitting patterns for large stories:")
    bullet("**By workflow step:** Split along user journey steps")
    bullet("**By business rule:** Each rule = one story")
    bullet("**By data variation:** Handle each data type separately")
    bullet("**By interface:** API, UI, integration as separate stories")
    bullet("**CRUD operations:** Create, Read, Update, Delete as individual stories")
    nl()

    h3("Maintaining a Healthy Backlog")
    p(f"With {len(stale_items)*100//len(items)}% stale items, the backlog needs regular grooming:")
    bullet("**Monthly grooming:** Review bottom 20% of backlog -- close or archive items that are no longer relevant")
    bullet("**Definition of Ready (DoR):** Establish criteria items must meet before entering a sprint (description, AC, estimate, epic link)")
    bullet("**WIP limits on backlog:** Cap backlog at 2-3 sprints worth of refined work; move the rest to an icebox")
    nl()

    h3("Preparing Items to be AI-Ready")
    p("To maximize AI-assisted automation:")
    bullet("**Structured descriptions:** Use consistent templates so AI can parse reliably")
    bullet("**Tag with components/labels:** Helps AI understand the technical domain")
    bullet("**Link to epics:** Provides business context for AI to generate relevant subtasks")
    bullet("**Include examples:** Concrete examples help AI draft better acceptance criteria")
    bullet("**Define boundaries:** Clear in-scope/out-of-scope prevents AI from over-generating")
    nl()

    h3("Recommended Refinement Session Agenda")
    p("1. **Stale Item Review** (15 min) -- Close or re-prioritize items untouched for 6+ months")
    p("2. **Bug Triage** (10 min) -- Assign priority and owners to undefined-priority bugs")
    p("3. **Large Story Splitting** (20 min) -- Decompose 8+ SP items using splitting patterns")
    p("4. **AC Writing Workshop** (20 min) -- Add Given/When/Then criteria to top 5 items")
    p("5. **Epic Linking** (10 min) -- Connect orphaned items to strategic epics")
    nl()

    return "\n".join(lines)


def generate_tier_analysis(all_items_data: list[dict], tier_map: dict, sprint_info: dict = None) -> tuple[dict, dict]:
    """Generate per-tier metrics and coaching from the all_items data and a tier_map.

    Args:
        all_items_data: List of item dicts (from generate_json_data's all_items).
        tier_map: Dict mapping issue key -> tier id ('active_sprint', 'planned_sprint', 'backlog').
        sprint_info: Optional dict with sprint metadata.

    Returns:
        (tier_analysis dict, tier_coaching dict)
    """
    tier_labels = {
        'active_sprint': 'Active Sprint',
        'planned_sprint': 'Planned Sprint',
        'backlog': 'Backlog',
    }
    # Enrich sprint label with name if available
    if sprint_info:
        si_active = sprint_info.get('active_sprint', {})
        si_planned = sprint_info.get('planned_sprint', {})
        if si_active.get('name'):
            tier_labels['active_sprint'] = f"Active Sprint ({si_active['name']})"
        if si_planned.get('name'):
            tier_labels['planned_sprint'] = f"Planned Sprint ({si_planned['name']})"

    tier_analysis = {}
    for tier_id, tier_label in tier_labels.items():
        items = [i for i in all_items_data if i.get('tier') == tier_id]
        if not items:
            continue
        n = len(items)
        has_ac = sum(1 for i in items if i['has_ac'])
        has_sp = sum(1 for i in items if i['story_points'] is not None)
        has_epic = sum(1 for i in items if i.get('epic_link'))
        stale = sum(1 for i in items if (i.get('days_since_update') or 0) > 180)
        undef_pri = sum(1 for i in items if i['priority'] in ('Undefined', ''))
        unassigned = sum(1 for i in items if i['assignee'] == 'Unassigned')
        concern_total = sum(i['concern_count'] for i in items)
        avg_ai = sum(i['ai_readiness'] for i in items) / n
        avg_codegen = sum(i.get('codegen_readiness', 1) for i in items) / n

        concern_counts = Counter()
        for i in items:
            concern_counts.update(i['concerns'])

        tier_analysis[tier_id] = {
            'label': tier_label,
            'count': n,
            'type_counts': dict(Counter(i['type'] for i in items).most_common()),
            'status_counts': dict(Counter(i['status'] for i in items).most_common()),
            'metrics': {
                'ac_coverage': round(has_ac * 100 / n, 1),
                'sp_coverage': round(has_sp * 100 / n, 1),
                'epic_coverage': round(has_epic * 100 / n, 1),
                'stale_pct': round(stale * 100 / n, 1),
                'undefined_priority_pct': round(undef_pri * 100 / n, 1),
                'unassigned_pct': round(unassigned * 100 / n, 1),
                'avg_concerns': round(concern_total / n, 1),
                'avg_ai_readiness': round(avg_ai, 1),
                'avg_codegen_readiness': round(avg_codegen, 1),
            },
            'top_concerns': [
                {'concern': c, 'count': cnt, 'pct': round(cnt * 100 / n, 1)}
                for c, cnt in concern_counts.most_common(5)
            ],
            'stale_items': [
                {'key': i['key'], 'type': i['type'], 'days': i['days_since_update'], 'summary': i['summary']}
                for i in sorted(items, key=lambda x: x.get('days_since_update') or 0, reverse=True)
                if (i.get('days_since_update') or 0) > 180
            ],
            'critical_items': [
                {'key': i['key'], 'type': i['type'], 'concerns': i['concerns'], 'summary': i['summary']}
                for i in sorted(items, key=lambda x: x['concern_count'], reverse=True)
                if i['concern_count'] >= 4
            ],
        }

    # Generate tier-specific coaching
    tier_coaching = {}
    for tier_id in tier_labels:
        ta = tier_analysis.get(tier_id)
        if not ta:
            continue
        m = ta['metrics']
        coaching = {'context': '', 'severity': '', 'risks': [], 'actions': []}

        if tier_id == 'active_sprint':
            coaching['context'] = 'These items are in the active sprint. The team should be executing on these NOW.'
            coaching['severity'] = 'high'
            if m['ac_coverage'] < 100:
                coaching['risks'].append(f"{100 - m['ac_coverage']:.0f}% of sprint items lack acceptance criteria -- team cannot validate 'done'")
                coaching['actions'].append('Immediately write AC for in-progress items missing them. Time-box to 5 min each during standup.')
            if m['sp_coverage'] < 100:
                coaching['risks'].append(f"{100 - m['sp_coverage']:.0f}% of sprint items are unestimated -- capacity planning is unreliable")
                coaching['actions'].append('Quick-estimate unpointed sprint items in next standup.')
            if m['unassigned_pct'] > 0:
                coaching['risks'].append(f"{m['unassigned_pct']:.0f}% of sprint items are unassigned -- work may not get started")
                coaching['actions'].append('Assign owners to all unassigned sprint items today.')
            if m['stale_pct'] > 0:
                coaching['risks'].append('Stale items found in active sprint -- these were likely pulled in without being refreshed')
                coaching['actions'].append('Review stale sprint items: are they still relevant? Update descriptions and AC before working on them.')
            if m['undefined_priority_pct'] > 30:
                coaching['risks'].append(f"{m['undefined_priority_pct']:.0f}% of sprint items have undefined priority -- team cannot triage mid-sprint blockers")
                coaching['actions'].append('Set priority on all sprint items so the team can make trade-offs if scope pressure emerges.')
            if m['epic_coverage'] < 70:
                coaching['risks'].append(f"Only {m['epic_coverage']:.0f}% of sprint items are linked to epics -- sprint work is disconnected from strategy")

        elif tier_id == 'planned_sprint':
            coaching['context'] = 'These items are planned for the next sprint. They must meet Definition of Ready before the sprint starts.'
            coaching['severity'] = 'medium'
            if m['ac_coverage'] < 80:
                coaching['risks'].append(f"Only {m['ac_coverage']:.0f}% of planned items have AC -- these are not ready to pull into a sprint")
                coaching['actions'].append('Add acceptance criteria to all planned sprint items BEFORE sprint planning.')
            if m['sp_coverage'] < 80:
                coaching['risks'].append(f"Only {m['sp_coverage']:.0f}% of planned items are estimated -- sprint capacity cannot be planned")
                coaching['actions'].append('Estimate all planned items in the next refinement session.')
            if m['unassigned_pct'] > 50:
                coaching['risks'].append(f"{m['unassigned_pct']:.0f}% of planned items are unassigned -- team may not have capacity alignment")
            if m['undefined_priority_pct'] > 50:
                coaching['risks'].append(f"{m['undefined_priority_pct']:.0f}% of planned items have undefined priority")
                coaching['actions'].append('Prioritize all planned items before sprint planning.')
            if ta['critical_items']:
                n_crit = len(ta['critical_items'])
                coaching['risks'].append(f"{n_crit} planned items have 4+ quality issues -- they will slow the sprint down")
                coaching['actions'].append('Do not pull items with 4+ concerns into a sprint. Refine them first or move them back to backlog.')

        elif tier_id == 'backlog':
            coaching['context'] = 'These items sit in the product backlog with no sprint assigned. Focus on grooming, closing stale items, and preparing high-priority items for future sprints.'
            coaching['severity'] = 'low'
            if m['stale_pct'] > 15:
                stale_count = len(ta['stale_items'])
                coaching['risks'].append(f"{m['stale_pct']:.0f}% of backlog items are stale (6+ months) -- backlog is accumulating dead weight")
                coaching['actions'].append(f'Review and close/archive {stale_count} stale items in a dedicated grooming session.')
            if m['ac_coverage'] < 30:
                coaching['risks'].append(f"Only {m['ac_coverage']:.0f}% of backlog items have AC -- bulk refinement needed before items can be sprint-ready")
                coaching['actions'].append('Use AI-assisted refinement to draft AC for backlog items in bulk, then have the team review.')
            if m['undefined_priority_pct'] > 60:
                coaching['risks'].append(f"{m['undefined_priority_pct']:.0f}% of backlog items have undefined priority -- prioritization is missing")
                coaching['actions'].append('Run a priority-setting exercise: stack-rank the backlog by business value in a 30-min session.')
            if m['epic_coverage'] < 50:
                coaching['risks'].append(f"Only {m['epic_coverage']:.0f}% of backlog items are linked to epics -- hard to tell what matters")
                coaching['actions'].append('Link orphaned items to epics or mark them as candidates for closure.')
            coaching['actions'].append('Cap backlog at 2-3 sprints of refined work. Move the rest to an icebox.')

        tier_coaching[tier_id] = coaching

    return tier_analysis, tier_coaching


def generate_json_data(items: list[BacklogItem], ref_date: datetime, team_name: str = "Unknown Team",
                       tier_map: dict = None, sprint_info: dict = None) -> dict:
    """Generate structured JSON data from backlog analysis."""
    type_counts = Counter(i.issue_type for i in items)
    status_counts = Counter(i.status for i in items)
    priority_counts = Counter(i.priority for i in items)

    has_desc = sum(1 for i in items if len(i.description.strip()) > 30)
    has_ac = sum(1 for i in items if i.has_acceptance_criteria)
    has_sp = sum(1 for i in items if i.story_points is not None)
    has_epic = sum(1 for i in items if i.epic_link or i.parent_id)
    has_assignee = sum(1 for i in items if i.assignee)

    stale_items = [i for i in items if days_since(i.updated, ref_date) and days_since(i.updated, ref_date) > 180]
    large_items = [i for i in items if i.story_points and i.story_points >= 8]
    no_desc_items = [i for i in items if len(i.description.strip()) < 30]
    no_ac_items = [i for i in items if not i.has_acceptance_criteria]
    no_epic_items = [i for i in items if not i.epic_link and not i.parent_id]
    undef_priority_bugs = [i for i in items if i.issue_type == 'Bug' and i.priority in ('Undefined', '')]
    items_with_deps = [i for i in items if i.outward_links or i.inward_links]

    ai_scores = {i.key: score_ai_readiness(i) for i in items}
    avg_ai = sum(ai_scores.values()) / len(ai_scores) if ai_scores else 0
    ai_dist = Counter(ai_scores.values())

    codegen_results = {i.key: score_ai_code_gen_readiness(i) for i in items}
    codegen_scores = {k: v[0] for k, v in codegen_results.items()}
    codegen_criteria = {k: v[1] for k, v in codegen_results.items()}
    avg_codegen = sum(codegen_scores.values()) / len(codegen_scores) if codegen_scores else 0
    codegen_dist = Counter(codegen_scores.values())

    all_concerns = {i.key: classify_concerns(i, ref_date) for i in items}
    concern_counts = Counter()
    for concerns in all_concerns.values():
        concern_counts.update(concerns)

    sp_values = [i.story_points for i in items if i.story_points is not None]
    sp_counter = Counter(sp_values)

    readiness_factors = [
        has_desc / len(items),
        has_ac / len(items),
        has_sp / len(items),
        1 - len(stale_items) / len(items),
        has_epic / len(items),
    ]
    raw_readiness = sum(readiness_factors) / len(readiness_factors)
    readiness_score = round(1 + raw_readiness * 4, 1)

    concern_labels = {
        "no_epic_link": "Items not linked to epics/goals",
        "missing_acceptance_criteria": "Items missing acceptance criteria",
        "stale": "Stale items (not updated in 6+ months)",
        "undefined_priority": "Items with undefined priority",
        "no_story_points": "Items without story point estimates",
        "missing_description": "Items with missing/minimal descriptions",
        "too_large": "Items too large for a single sprint",
    }

    critical = [(i, c) for i in items for c in [all_concerns[i.key]] if len(c) >= 4]
    critical.sort(key=lambda x: len(x[1]), reverse=True)

    dupes = find_potential_duplicates(items)

    dep_sorted = sorted(items_with_deps, key=lambda x: len(x.outward_links) + len(x.inward_links), reverse=True)

    # Derived counts for narrative
    not_estimated = len(items) - len(sp_values)
    unassigned_bugs = sum(1 for i in items if i.issue_type == 'Bug' and not i.assignee)
    new_pct = round(status_counts.get('New', 0) * 100 / len(items), 1) if items else 0
    undef_pct = round(priority_counts.get('Undefined', 0) * 100 / len(items), 1) if items else 0
    stale_pct = round(len(stale_items) * 100 / len(items), 1) if items else 0
    initiatives = [i for i in items if i.issue_type == 'Initiative']
    medium_large = [i for i in items if i.story_points and i.story_points == 5]

    # AI missing elements breakdown
    ai_missing = Counter()
    for i in items:
        if ai_scores[i.key] <= 2:
            if not i.has_acceptance_criteria:
                ai_missing["Missing acceptance criteria"] += 1
            if len(i.description.strip()) < 100:
                ai_missing["Insufficient description"] += 1
            if not i.epic_link and not i.parent_id:
                ai_missing["No epic/goal linkage"] += 1

    # Readiness label
    score_labels = {1: "Poorly defined / not ready", 2: "Below average readiness",
                    3: "Moderately ready", 4: "Good readiness", 5: "Well structured / ready for sprint planning"}
    readiness_label = score_labels.get(round(readiness_score), score_labels[3])

    # Organization observations
    observations = []
    observations.append(f"{len(no_epic_items)}/{len(items)} items lack epic/parent links -- work cannot be traced to strategic goals")
    if len(stale_items) > len(items) // 3:
        observations.append(f"{stale_pct}% of the backlog is stale -- a grooming session to archive or close old items is recommended")
    if unassigned_bugs:
        observations.append(f"{unassigned_bugs} bugs have no assignee")
    if new_pct > 80:
        observations.append(f"{new_pct}% of items are in 'New' status -- items may not be going through a proper triage or refinement workflow")
    if undef_pct > 40:
        observations.append(f"{undef_pct}% of items have 'Undefined' priority -- prioritization needs attention")

    result = {
        "meta": {
            "analysis_date": ref_date.strftime("%Y-%m-%d"),
            "total_items": len(items),
            "team": team_name,
        },
        "executive_summary": {
            "type_counts": {k: v for k, v in type_counts.most_common()},
            "quality_metrics": {
                "description_coverage": {"count": has_desc, "total": len(items), "pct": round(has_desc * 100 / len(items), 1)},
                "acceptance_criteria": {"count": has_ac, "total": len(items), "pct": round(has_ac * 100 / len(items), 1)},
                "story_points": {"count": has_sp, "total": len(items), "pct": round(has_sp * 100 / len(items), 1)},
                "epic_linkage": {"count": has_epic, "total": len(items), "pct": round(has_epic * 100 / len(items), 1)},
                "stale_items": {"count": len(stale_items), "total": len(items), "pct": round(len(stale_items) * 100 / len(items), 1)},
            },
            "readiness_score": readiness_score,
            "readiness_label": readiness_label,
            "ai_readiness_avg": round(avg_ai, 1),
            "score_card_info": {
                "backlog_readiness": {
                    "what": "A composite score (1-5) measuring how ready the backlog is for sprint planning. It averages five factors: description quality, acceptance criteria, story point estimates, freshness, and epic linkage.",
                    "why_helpful": "A low readiness score means sprints start with unclear work, leading to mid-sprint scope changes, blocked developers, and missed commitments. Tracking this over time shows whether refinement practices are improving.",
                },
                "ai_readiness": {
                    "what": "The average AI-readiness score (1-5) across all backlog items. Each item is scored based on whether it has enough structured detail (description, acceptance criteria, epic link) for AI tools to act on it.",
                    "why_helpful": "This tells you how much value AI tools can extract from your backlog today. A low score means AI-assisted refinement, subtask generation, and test case drafting will produce poor results until item quality improves.",
                },
                "quality_index": {
                    "what": "The average of four key coverage percentages: description coverage, acceptance criteria, epic linkage, and freshness (inverse of stale). It gives a single number for overall backlog hygiene.",
                    "why_helpful": "A quick health check for the backlog as a whole. If this number is declining sprint over sprint, the team is accumulating low-quality items faster than they are cleaning them up.",
                },
                "critical_items": {
                    "what": "The count of items that have 4 or more concerns flagged (e.g., missing description, no AC, stale, no estimates, no epic link). These items need the most refinement attention.",
                    "why_helpful": "These are the items most likely to cause problems if they enter a sprint. Focusing refinement on critical items first gives the biggest improvement in backlog quality per hour invested.",
                },
            },
            "top_concerns": [
                {"id": c, "label": concern_labels.get(c, c), "count": n, "pct": round(n * 100 / len(items), 1)}
                for c, n in concern_counts.most_common(5)
            ],
        },
        "readiness_factors": [
            {"name": "Description Quality", "pct": round(has_desc * 100 / len(items), 1), "score": round(1 + (has_desc / len(items)) * 4, 1), "detail": f"{has_desc}/{len(items)} items",
             "what": "Scores how many items have a meaningful description (more than 30 characters) on a 1-5 scale. A score of 5 means every item has a description; 1 means almost none do.",
             "why_helpful": "Low description quality means the team is working from assumptions. This leads to rework, misaligned deliverables, and blocks AI tools from generating useful subtasks or test cases."},
            {"name": "Acceptance Criteria", "pct": round(has_ac * 100 / len(items), 1), "score": round(1 + (has_ac / len(items)) * 4, 1), "detail": f"{has_ac}/{len(items)} items",
             "what": "Scores how many items contain acceptance criteria (e.g. Given/When/Then, expected result, definition of done) on a 1-5 scale. AC defines when work is truly complete.",
             "why_helpful": "Without AC, 'done' is subjective. Teams waste time debating completeness during review, QA catches mismatches late, and stakeholders get surprises at demo."},
            {"name": "Story Point Estimates", "pct": round(has_sp * 100 / len(items), 1), "score": round(1 + (has_sp / len(items)) * 4, 1), "detail": f"{has_sp}/{len(items)} items",
             "what": "Scores how many items have story point estimates on a 1-5 scale. A score of 5 means every item is estimated; 1 means almost none are.",
             "why_helpful": "Unestimated items make sprint planning unreliable. The team can't forecast capacity, identify overcommitment early, or spot stories that need splitting before they enter a sprint."},
            {"name": "Freshness", "pct": round((len(items) - len(stale_items)) * 100 / len(items), 1), "score": round(1 + ((len(items) - len(stale_items)) / len(items)) * 4, 1), "detail": f"{len(items) - len(stale_items)}/{len(items)} items",
             "what": "Scores how many items have been updated within the last 6 months on a 1-5 scale. A score of 5 means the backlog is fully current; 1 means most items are stale.",
             "why_helpful": "A backlog full of stale items erodes trust in the tool. Teams stop looking at it for planning, priorities become tribal knowledge, and new members can't tell what matters."},
            {"name": "Epic Linkage", "pct": round(has_epic * 100 / len(items), 1), "score": round(1 + (has_epic / len(items)) * 4, 1), "detail": f"{has_epic}/{len(items)} items",
             "what": "Scores how many items are linked to an epic or parent item on a 1-5 scale. Linkage connects daily work to strategic goals and makes progress visible to stakeholders.",
             "why_helpful": "Without epic linkage, leadership can't see how sprint work maps to objectives. It also makes it impossible to measure progress toward goals or generate meaningful roadmap reports."},
        ],
        "status_distribution": [{"status": s, "count": c, "pct": round(c * 100 / len(items), 1)} for s, c in status_counts.most_common()],
        "priority_distribution": [{"priority": p, "count": c, "pct": round(c * 100 / len(items), 1)} for p, c in priority_counts.most_common()],
        "story_point_distribution": [
            {"points": int(sp), "count": cnt, "pct": round(cnt * 100 / len(sp_values), 1) if sp_values else 0}
            for sp, cnt in sorted(sp_counter.items())
        ],
        "not_estimated_count": not_estimated,
        "large_items": [
            {"key": i.key, "type": i.issue_type, "story_points": i.story_points, "summary": i.summary}
            for i in sorted(large_items, key=lambda x: x.story_points or 0, reverse=True)
        ],
        "no_description_items": [
            {"key": i.key, "type": i.issue_type, "summary": i.summary}
            for i in no_desc_items
        ],
        "undefined_priority_bugs": [
            {"key": i.key, "summary": i.summary}
            for i in undef_priority_bugs
        ],
        "stale_items": [
            {"key": i.key, "type": i.issue_type, "days_since_update": days_since(i.updated, ref_date), "summary": i.summary}
            for i in sorted(stale_items, key=lambda x: days_since(x.updated, ref_date) or 0, reverse=True)
        ],
        "critical_items": [
            {"key": i.key, "type": i.issue_type, "concerns": c, "concern_count": len(c), "summary": i.summary}
            for i, c in critical[:20]
        ],
        "no_epic_items_count": len(no_epic_items),
        "dependency_items": [
            {"key": i.key, "outward": len(i.outward_links), "inward": len(i.inward_links), "summary": i.summary}
            for i in dep_sorted[:15]
        ],
        "items_with_deps_count": len(items_with_deps),
        "duplicates": [
            {"key_a": a_key, "summary_a": a_sum, "key_b": b_key, "summary_b": b_sum, "similarity": round(sim, 2)}
            for a_key, a_sum, b_key, b_sum, sim in dupes[:10]
        ],
        "decomposition": {
            "initiatives": [{"key": i.key, "summary": i.summary} for i in initiatives],
            "medium_large_count": len(medium_large),
        },
        "organization": {
            "observations": observations,
        },
        "ai_readiness": {
            "distribution": [
                {"score": s, "count": ai_dist.get(s, 0), "pct": round(ai_dist.get(s, 0) * 100 / len(items), 1),
                 "label": {1: "AI cannot act", 2: "Minimal AI use", 3: "AI can suggest with limited accuracy",
                           4: "AI can generate useful outputs", 5: "AI can fully generate subtasks/AC/splits"}[s]}
                for s in range(1, 6)
            ],
            "low_score_count": sum(1 for v in ai_scores.values() if v <= 2),
            "missing_elements": [{"element": e, "count": c} for e, c in ai_missing.most_common()],
            "top_ready": [
                {"key": i.key, "score": ai_scores[i.key], "type": i.issue_type, "summary": i.summary}
                for i in sorted(items, key=lambda x: ai_scores[x.key], reverse=True)
                if ai_scores[i.key] >= 4
            ][:15],
            "recommendations": [
                "Add structured acceptance criteria using Given/When/Then format",
                "Link all items to epics to provide business context",
                "Include clear scope boundaries (in-scope / out-of-scope)",
                "Use a standard story template with Description, AC, Dependencies, and Business Value sections",
                "Add technical context (affected components, APIs, data models) for AI to generate accurate subtasks",
            ],
        },
        "code_gen_readiness": {
            "avg_score": round(avg_codegen, 1),
            "distribution": [
                {"score": s, "count": codegen_dist.get(s, 0), "pct": round(codegen_dist.get(s, 0) * 100 / len(items), 1),
                 "label": {1: "Not ready -- lacks technical detail",
                           2: "Minimal -- has some context but missing key elements",
                           3: "Partial -- AI could attempt with significant guidance",
                           4: "Ready -- AI can generate code with review",
                           5: "Fully ready -- AI can autonomously implement"}[s]}
                for s in range(1, 6)
            ],
            "criteria_coverage": {
                "technical_context": sum(1 for c in codegen_criteria.values() if "Technical context" in c),
                "testable_ac": sum(1 for c in codegen_criteria.values() if "Testable acceptance criteria" in c),
                "bounded_scope": sum(1 for c in codegen_criteria.values() if "Bounded scope" in c),
                "component_mapped": sum(1 for c in codegen_criteria.values() if "Component/label mapped" in c),
                "no_blockers": sum(1 for c in codegen_criteria.values() if "No blocking dependencies" in c),
                "estimated": sum(1 for c in codegen_criteria.values() if "Estimated" in c),
            },
            "top_ready": [
                {"key": i.key, "score": codegen_scores[i.key], "type": i.issue_type, "summary": i.summary,
                 "criteria": codegen_criteria[i.key]}
                for i in sorted(items, key=lambda x: codegen_scores[x.key], reverse=True)
                if codegen_scores[i.key] >= 4
            ][:15],
            "not_ready": [
                {"key": i.key, "score": codegen_scores[i.key], "type": i.issue_type, "summary": i.summary,
                 "criteria": codegen_criteria[i.key]}
                for i in sorted(items, key=lambda x: codegen_scores[x.key])
                if codegen_scores[i.key] <= 2
            ][:10],
        },
        "recommended_actions": {
            "immediate": [
                {"action": "Triage stale items", "detail": f"Review and close/archive {len(stale_items)} items not updated in 6+ months"},
                {"action": "Prioritize bugs", "detail": f"Set priority on {len(undef_priority_bugs)} bugs with undefined priority"},
                {"action": "Split large items", "detail": f"Decompose {len(large_items)} items estimated at 8+ story points"},
                {"action": "Add descriptions", "detail": f"Write descriptions for {len(no_desc_items)} items with missing/minimal content"},
            ],
            "short_term": [
                {"action": "Add acceptance criteria", "detail": f"{len(no_ac_items)} items ({round(len(no_ac_items)*100/len(items))}%) lack AC"},
                {"action": "Link to epics", "detail": f"Connect {len(no_epic_items)} orphaned items to strategic epics/goals"},
                {"action": "Estimate unpointed items", "detail": f"{not_estimated} items need story point estimates"},
                {"action": "Review potential duplicates", "detail": "Merge or link overlapping items identified in this analysis"},
            ],
            "automatable": [
                {"action": "Detect stale items", "approach": "Jira automation: flag items not updated in 90/180 days", "effort": "Low"},
                {"action": "Missing AC check", "approach": "Jira automation: label items without 'acceptance criteria' in description", "effort": "Low"},
                {"action": "Priority enforcement", "approach": "Jira workflow: require priority on bug creation", "effort": "Low"},
                {"action": "Duplicate detection", "approach": "AI script: NLP similarity analysis on summaries/descriptions", "effort": "Medium"},
                {"action": "Story splitting suggestions", "approach": "AI script: analyze large stories and suggest decomposition", "effort": "Medium"},
                {"action": "AC generation", "approach": "AI script: draft acceptance criteria from description context", "effort": "Medium"},
                {"action": "Stale item notifications", "approach": "Jira automation: send Slack/email digest of aging items", "effort": "Low"},
                {"action": "Epic linkage enforcement", "approach": "Jira workflow: require epic link before moving to 'Ready'", "effort": "Low"},
            ],
        },
        "coaching_insights": {
            "story_template": "**As a** [persona],\n**I want** [capability],\n**So that** [business value].\n\n**Acceptance Criteria:**\n- Given [context], When [action], Then [expected outcome]\n\n**Scope:**\n- In scope: [list]\n- Out of scope: [list]\n\n**Dependencies:** [list or 'None']\n**Technical Notes:** [any implementation guidance]",
            "ac_guidance": f"Only {round(has_ac*100/len(items))}% of items have acceptance criteria. Without AC, teams cannot validate 'done' and AI tools cannot generate meaningful subtasks. In your next refinement session, take the top 5 items by priority and collaboratively write Given/When/Then acceptance criteria for each. Time-box to 5 minutes per item.",
            "splitting_patterns": [
                {"pattern": "By workflow step", "detail": "Split along user journey steps"},
                {"pattern": "By business rule", "detail": "Each rule = one story"},
                {"pattern": "By data variation", "detail": "Handle each data type separately"},
                {"pattern": "By interface", "detail": "API, UI, integration as separate stories"},
                {"pattern": "CRUD operations", "detail": "Create, Read, Update, Delete as individual stories"},
            ],
            "backlog_health": [
                "Monthly grooming: Review bottom 20% of backlog -- close or archive items that are no longer relevant",
                "Definition of Ready (DoR): Establish criteria items must meet before entering a sprint (description, AC, estimate, epic link)",
                "WIP limits on backlog: Cap backlog at 2-3 sprints worth of refined work; move the rest to an icebox",
            ],
            "ai_ready_tips": [
                "Structured descriptions: Use consistent templates so AI can parse reliably",
                "Tag with components/labels: Helps AI understand the technical domain",
                "Link to epics: Provides business context for AI to generate relevant subtasks",
                "Include examples: Concrete examples help AI draft better acceptance criteria",
                "Define boundaries: Clear in-scope/out-of-scope prevents AI from over-generating",
            ],
            "refinement_agenda": [
                {"step": "Stale Item Review", "time": "15 min", "detail": "Close or re-prioritize items untouched for 6+ months"},
                {"step": "Bug Triage", "time": "10 min", "detail": "Assign priority and owners to undefined-priority bugs"},
                {"step": "Large Story Splitting", "time": "20 min", "detail": "Decompose 8+ SP items using splitting patterns"},
                {"step": "AC Writing Workshop", "time": "20 min", "detail": "Add Given/When/Then criteria to top 5 items"},
                {"step": "Epic Linking", "time": "10 min", "detail": "Connect orphaned items to strategic epics"},
            ],
        },
        "all_items": [
            {
                "key": i.key,
                "summary": i.summary,
                "type": i.issue_type,
                "status": i.status,
                "priority": i.priority,
                "assignee": i.assignee or "Unassigned",
                "story_points": i.story_points,
                "has_ac": i.has_acceptance_criteria,
                "epic_link": i.epic_link or None,
                "ai_readiness": ai_scores[i.key],
                "codegen_readiness": codegen_scores[i.key],
                "concerns": all_concerns[i.key],
                "concern_count": len(all_concerns[i.key]),
                "days_since_update": days_since(i.updated, ref_date),
                "created": i.created.strftime("%Y-%m-%d") if i.created else None,
                "updated": i.updated.strftime("%Y-%m-%d") if i.updated else None,
                "tier": tier_map.get(i.key, "backlog") if tier_map else None,
            }
            for i in items
        ],
    }

    # Add tier analysis if tier_map is provided
    if tier_map:
        tier_analysis, tier_coaching = generate_tier_analysis(
            result["all_items"], tier_map, sprint_info
        )
        result["tier_analysis"] = tier_analysis
        result["tier_coaching"] = tier_coaching
        if sprint_info:
            result["sprint_info"] = sprint_info

    return result


def find_potential_duplicates(items: list[BacklogItem], threshold: float = 0.6) -> list:
    """Find items with similar titles using word overlap (Jaccard similarity)."""
    results = []

    def tokenize(text):
        return set(w.lower().strip('[]()') for w in text.split() if len(w) > 2)

    token_cache = {i.key: tokenize(i.summary) for i in items}

    checked = set()
    for i in items:
        for j in items:
            if i.key >= j.key:
                continue
            pair = (i.key, j.key)
            if pair in checked:
                continue
            checked.add(pair)

            t1, t2 = token_cache[i.key], token_cache[j.key]
            if not t1 or not t2:
                continue
            intersection = len(t1 & t2)
            union = len(t1 | t2)
            sim = intersection / union if union else 0
            if sim >= threshold:
                results.append((i.key, i.summary, j.key, j.summary, sim))

    results.sort(key=lambda x: x[4], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate_dashboard(json_data: dict, template_path: str) -> str:
    """Embed JSON data into the dashboard HTML template to produce a self-contained file."""
    with open(template_path, 'r') as f:
        html = f.read()
    json_str = json.dumps(json_data)
    html = html.replace('const EMBEDDED_DATA = null;',
                        f'const EMBEDDED_DATA = {json_str};')
    return html


def main():
    parser = argparse.ArgumentParser(
        description="Analyze a Jira backlog from CSV export or Jira Cloud API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze from CSV export:
  python backlog_analyzer.py --csv export.csv -o report.md -j data.json --dashboard dashboard_out.html

  # Analyze from Jira Cloud by board name:
  python backlog_analyzer.py --board "AIP Pipelines" -o report.md --dashboard dashboard_out.html

  # Analyze from Jira Cloud by team name:
  python backlog_analyzer.py --team "AIP Pipelines" --project RHOAIENG --dashboard dashboard_out.html

  # Analyze from Jira Cloud by component:
  python backlog_analyzer.py --project RHOAIENG --component "AI Pipelines" --dashboard dashboard_out.html

Environment variables for Jira API:
  JIRA_URL          Jira Cloud base URL (alternative to --jira-url)
  JIRA_EMAIL        Email for API authentication
  JIRA_API_TOKEN    API token for authentication
  JIRA_FIELD_STORY_POINTS  Custom field ID for Story Points (default: customfield_10028)
  JIRA_FIELD_EPIC_LINK     Custom field ID for Epic Link (default: customfield_10014)
  JIRA_FIELD_TEAM          Custom field ID for Team (default: customfield_10001)
  JIRA_FIELD_SPRINT        Custom field ID for Sprint (default: customfield_10020)
""")

    # Input source (mutually informative, not exclusive since --project can combine with others)
    input_group = parser.add_argument_group("Input source (choose one)")
    input_group.add_argument("--csv", dest="csv_path", default=None,
                             help="Path to a Jira CSV export file")
    input_group.add_argument("--board", default=None,
                             help="Jira board name to pull issues from")
    input_group.add_argument("--board-id", type=int, default=None,
                             help="Jira board ID — auto-discovers sprints and builds tiered analysis")
    input_group.add_argument("--team", default=None,
                             help="Team name to filter issues by (uses Team custom field)")

    # Jira API options
    jira_group = parser.add_argument_group("Jira API options")
    jira_group.add_argument("--jira-url", default=None,
                            help="Jira Cloud base URL (or set JIRA_URL env var)")
    jira_group.add_argument("--project", default=None,
                            help="Jira project key (e.g., RHOAIENG) to scope the query")
    jira_group.add_argument("--component", default=None,
                            help="Component name to filter issues by")
    jira_group.add_argument("--include-done", action="store_true", default=False,
                            help="Include items in Done/Closed status (excluded by default)")

    # Output options
    output_group = parser.add_argument_group("Output options")
    output_group.add_argument("--output", "-o", default=None,
                              help="Output markdown file path (default: stdout)")
    output_group.add_argument("--json", "-j", default=None,
                              help="Output JSON file path")
    output_group.add_argument("--dashboard", default=None,
                              help="Output self-contained dashboard HTML file")
    output_group.add_argument("--template", default=None,
                              help="Dashboard HTML template path (default: dashboard.html next to this script)")
    output_group.add_argument("--date", "-d", default=None,
                              help="Reference date YYYY-MM-DD (default: today)")

    # Tier / sprint options
    tier_group = parser.add_argument_group("Tier / sprint options")
    tier_group.add_argument("--tier-json", default=None,
                            help="JSON file with tier categories: {\"active\": [keys], \"planned\": [keys], \"backlog\": [keys]}")
    tier_group.add_argument("--sprint-name", default=None,
                            help="Active sprint name for tier analysis labels")
    tier_group.add_argument("--sprint-goal", default=None,
                            help="Active sprint goal text")
    tier_group.add_argument("--sprint-end", default=None,
                            help="Active sprint end date (YYYY-MM-DD)")
    tier_group.add_argument("--planned-sprint-name", default=None,
                            help="Planned/future sprint name")

    # Legacy positional argument support
    parser.add_argument("legacy_csv_path", nargs="?", default=None,
                        help=argparse.SUPPRESS)

    args = parser.parse_args()

    # Support legacy positional CSV path
    if args.legacy_csv_path and not args.csv_path:
        args.csv_path = args.legacy_csv_path

    ref_date = datetime.strptime(args.date, "%Y-%m-%d") if args.date else datetime.now()

    # These may be populated by --board-id auto-discovery or --tier-json below
    tier_map = None
    sprint_info = None

    # Determine input mode
    if args.csv_path:
        # CSV mode
        print(f"Loading backlog from CSV: {args.csv_path}", file=sys.stderr)
        items, meta = load_csv(args.csv_path)
        print(f"Loaded {len(items)} items", file=sys.stderr)

    elif args.board_id or args.board or args.team or (args.project and args.component):
        # Jira API mode
        jira_url = args.jira_url or os.environ.get("JIRA_URL", "")
        jira_email = os.environ.get("JIRA_EMAIL", "")
        jira_token = os.environ.get("JIRA_API_TOKEN", "")

        if not jira_url:
            print("Error: --jira-url or JIRA_URL environment variable required for Jira API mode",
                  file=sys.stderr)
            sys.exit(1)
        if not jira_email or not jira_token:
            print("Error: JIRA_EMAIL and JIRA_API_TOKEN environment variables required",
                  file=sys.stderr)
            sys.exit(1)

        client = JiraClient(jira_url, jira_email, jira_token)

        # Build the query
        board_jql = None
        team_name = ""
        board_id_for_tiers = None

        if args.board_id:
            board_id_for_tiers = args.board_id
            print(f"Using board ID: {args.board_id}", file=sys.stderr)
            team_name = client.get_board_name(args.board_id)
            print(f"Board name: {team_name}", file=sys.stderr)
            board_jql = client.get_board_jql(args.board_id)
            if board_jql:
                print(f"Board JQL filter: {board_jql}", file=sys.stderr)

        elif args.board:
            print(f"Looking up board: {args.board}", file=sys.stderr)
            board = client.find_board_by_name(args.board)
            if not board:
                print(f"Error: Board '{args.board}' not found", file=sys.stderr)
                sys.exit(1)
            print(f"Found board: {board['name']} (id={board['id']})", file=sys.stderr)
            board_jql = client.get_board_jql(board["id"])
            team_name = args.board
            if board_jql:
                print(f"Board JQL filter: {board_jql}", file=sys.stderr)

        if args.team:
            team_name = args.team

        jql = build_jql(
            project=args.project,
            team=args.team,
            board_jql=board_jql,
            component=args.component,
            status_exclude=[] if args.include_done else None,
        )

        items, meta = load_from_jira(client, jql, team_name or args.component or args.project or "Unknown Team")
        print(f"Loaded {len(items)} items from Jira API", file=sys.stderr)

        # Auto-discover tiers from board sprints when using --board-id
        if board_id_for_tiers and not args.tier_json:
            print("Discovering sprints for tier analysis...", file=sys.stderr)
            sprints = client.get_board_sprints(board_id_for_tiers, states=["active", "future"])
            all_item_keys = {i.key for i in items}
            active_keys = set()
            planned_keys = set()
            sprint_info = {}

            for s in sprints:
                sprint_keys = set(client.get_sprint_issue_keys(s["id"])) & all_item_keys
                if s.get("state") == "active":
                    active_keys |= sprint_keys
                    sprint_info["active_sprint_name"] = s.get("name", "")
                    sprint_info["active_sprint_goal"] = s.get("goal", "")
                    end_date = s.get("endDate", "")
                    if end_date:
                        sprint_info["active_sprint_end"] = end_date[:10]
                    sprint_label = f"Active: {s.get('name', '')}"
                    print(f"  {sprint_label} ({len(sprint_keys)} items)", file=sys.stderr)
                elif s.get("state") == "future":
                    planned_keys |= sprint_keys
                    if "planned_sprint_name" not in sprint_info:
                        sprint_info["planned_sprint_name"] = s.get("name", "")
                    print(f"  Planned: {s.get('name', '')} ({len(sprint_keys)} items)", file=sys.stderr)

            backlog_keys = all_item_keys - active_keys - planned_keys
            print(f"  Backlog: {len(backlog_keys)} items", file=sys.stderr)

            tier_map = {}
            for k in active_keys:
                tier_map[k] = "active_sprint"
            for k in planned_keys:
                tier_map[k] = "planned_sprint"
            for k in backlog_keys:
                tier_map[k] = "backlog"

    else:
        parser.print_help()
        print("\nError: Specify --board-id, --csv, --board, --team, or --project with --component",
              file=sys.stderr)
        sys.exit(1)

    if not items:
        print("Warning: No items found. Check your query parameters.", file=sys.stderr)

    # Auto-generate output filenames when using --board-id and no explicit outputs given
    if args.board_id and not args.output and not args.json and not args.dashboard:
        slug = re.sub(r'[^a-z0-9]+', '_', meta["team"].lower()).strip('_') if meta["team"] else f"board_{args.board_id}"
        args.output = f"{slug}_report.md"
        args.json = f"{slug}_data.json"
        args.dashboard = f"{slug}_dashboard.html"
        print(f"Auto-naming outputs: {slug}_*", file=sys.stderr)

    report = generate_report(items, ref_date)

    if args.output:
        with open(args.output, 'w') as f:
            f.write(report)
        print(f"Report written to: {args.output}", file=sys.stderr)
    else:
        print(report)

    # Build tier map and sprint info
    # tier_map / sprint_info may already be set by --board-id auto-discovery above

    # --tier-json overrides auto-discovered tiers
    if args.tier_json:
        with open(args.tier_json, 'r') as f:
            tier_data = json.load(f)
        tier_map = {}
        for key in tier_data.get("active", []):
            tier_map[key] = "active_sprint"
        for key in tier_data.get("planned", []):
            tier_map[key] = "planned_sprint"
        for key in tier_data.get("backlog", []):
            tier_map[key] = "backlog"
        sprint_info = {}
        if args.sprint_name:
            sprint_info["active_sprint_name"] = args.sprint_name
        if args.sprint_goal:
            sprint_info["active_sprint_goal"] = args.sprint_goal
        if args.sprint_end:
            sprint_info["active_sprint_end"] = args.sprint_end
        if args.planned_sprint_name:
            sprint_info["planned_sprint_name"] = args.planned_sprint_name

    json_data = generate_json_data(items, ref_date, team_name=meta["team"],
                                   tier_map=tier_map, sprint_info=sprint_info)

    if args.json:
        with open(args.json, 'w') as f:
            json.dump(json_data, f, indent=2)
        print(f"JSON written to: {args.json}", file=sys.stderr)

    if args.dashboard:
        template = args.template or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dashboard.html')
        dashboard_html = generate_dashboard(json_data, template)
        with open(args.dashboard, 'w') as f:
            f.write(dashboard_html)
        print(f"Dashboard written to: {args.dashboard}", file=sys.stderr)


if __name__ == "__main__":
    main()
