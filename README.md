# Backlog Analyzer

An AI-powered Agile Coach that analyzes Jira backlogs for quality, sprint readiness, and AI-readiness — then generates an interactive HTML dashboard with actionable coaching insights.

No configuration files. No dependencies beyond Python. Just point it at a board and get a full analysis.

![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)
![No Dependencies](https://img.shields.io/badge/dependencies-none-green.svg)

## Quick Start

```bash
export JIRA_URL=https://your-org.atlassian.net
export JIRA_EMAIL=you@example.com
export JIRA_API_TOKEN=your-api-token

python backlog_analyzer.py --board-id <your-board-id>
```

That's it. The tool will:
1. Fetch all issues from the board
2. Auto-discover active and planned sprints
3. Categorize items into tiers (active sprint / planned sprint / backlog)
4. Generate three output files auto-named from your board:

```
Using board ID: 1131
Board name: Training Kubeflow
Searching Jira...
Loaded 100 items from Jira API
Discovering sprints for tier analysis...
  Active: Sprint 27 (17 items)
  Planned: Sprint 28 (11 items)
  Backlog: 72 items
Auto-naming outputs: training_kubeflow_*
Report written to: training_kubeflow_report.md
JSON written to: training_kubeflow_data.json
Dashboard written to: training_kubeflow_dashboard.html
```

| Output | What's Inside |
|--------|---------------|
| **HTML Dashboard** | Interactive 12-section dashboard with charts, filters, and drill-downs |
| **Markdown Report** | Text-based report suitable for sharing in Slack, email, or wikis |
| **JSON Data** | Structured data for integration with other tools or custom reporting |

## What It Analyzes

### Backlog Quality (per item)
- Does it have a meaningful description?
- Are acceptance criteria defined?
- Is it estimated with story points?
- Is it linked to an epic or strategic goal?
- Is it stale (untouched for 6+ months)?

### Sprint Readiness (three tiers)
The tool auto-discovers your active and planned sprints and categorizes every board item into one of three tiers, each with different coaching severity:

| Tier | What It Asks | Coaching Tone |
|------|-------------|---------------|
| **Active Sprint** | Can the team execute on this right now? | Urgent — flag blockers immediately |
| **Planned Sprint** | Does this meet the Definition of Ready? | Preparatory — close gaps before sprint starts |
| **Backlog** | Should this still exist? Is it groomed? | Strategic — prune, prioritize, or refine |

### AI Readiness (two scores)
- **AI Refinement Readiness** — Can AI tools generate useful subtasks, acceptance criteria, or story splits from this item?
- **AI Code Gen Readiness** — Is this item well-defined enough for AI-assisted code generation?

### Backlog Hygiene
- Duplicate and overlapping item detection
- Dependency bottleneck identification
- Priority distribution gaps
- Epic linkage gaps (work not traceable to strategic goals)
- Story size distribution and decomposition recommendations

## The Dashboard

The interactive HTML dashboard has 12 sections navigable by tabs:

1. **Executive Summary** — Item counts, type breakdown, quality metrics at a glance
2. **Backlog at a Glance** — Three-tier sprint view with per-tier metrics, risks, and coaching
3. **Key Concerns** — Poorly defined items, stale items, bugs needing triage
4. **Recommended Actions** — Immediate, short-term, and automatable improvements
5. **Story Size & Decomposition** — Point distribution, items needing splitting
6. **Dependency Signals** — Issue links, blocking chains, cross-team dependencies
7. **Backlog Organization** — Status/priority distribution, potential duplicates
8. **Readiness Score** — Composite 1–5 score across 5 quality factors
9. **AI Refinement Readiness** — Score distribution, items ready for AI-assisted refinement
10. **AI Code Gen Readiness** — Score distribution, items ready for AI code generation
11. **Coaching Insights** — Story writing templates, refinement session agendas, splitting patterns
12. **All Items** — Searchable, filterable, sortable table of every backlog item

The dashboard is fully self-contained — a single HTML file with no external dependencies. Open it in any browser, share it via email or Slack, or host it on any web server.

## Three Scoring Systems

### Backlog Readiness Score (1–5)
Measures whether items are ready for sprint planning.

| Factor | Weight |
|--------|--------|
| Description quality | 20% |
| Acceptance criteria present | 20% |
| Story point estimate | 20% |
| Freshness (updated recently) | 20% |
| Epic/goal linkage | 20% |

### AI Refinement Readiness (1–5)
Measures whether AI tools can meaningfully assist with refinement.

| Factor | What It Checks |
|--------|---------------|
| Description depth | Enough context for AI to understand intent |
| Acceptance criteria | Structured criteria AI can decompose |
| Epic context | Business context for generating relevant subtasks |

### AI Code Gen Readiness (1–5)
Measures whether items are well-defined enough for AI code generation.

| Factor | What It Checks |
|--------|---------------|
| Technical context | Component, API, or system references |
| Testable AC | Criteria that map to test cases |
| Bounded scope | Clear in-scope/out-of-scope boundaries |
| Component mapping | Tied to a codebase area |
| No blockers | Not blocked by dependencies |
| Estimated | Has a size estimate indicating understood scope |

## Who It's For

- **Scrum Masters / Agile Coaches** — Get an objective health check of any team's backlog
- **Product Owners** — Identify which items need refinement before they're sprint-ready
- **Engineering Managers** — Spot systemic backlog hygiene issues across teams
- **Teams exploring AI-assisted development** — Know which items are ready for AI tools and which need more detail first

## All Input Modes

### Board ID (recommended)
```bash
python backlog_analyzer.py --board-id 1131
```

### CSV Export
```bash
python backlog_analyzer.py --csv path/to/jira-export.csv --dashboard report.html
```

### Board Name
```bash
python backlog_analyzer.py --board "My Board Name" --dashboard report.html
```

### Team + Project
```bash
python backlog_analyzer.py --team "My Team" --project MYPROJ --dashboard report.html
```

### Project + Component
```bash
python backlog_analyzer.py --project MYPROJ --component "My Component" --dashboard report.html
```

## Output Options

| Flag | Description |
|------|-------------|
| `--dashboard FILE` | Generate interactive HTML dashboard |
| `--output FILE` / `-o FILE` | Write markdown report to file (default: stdout) |
| `--json FILE` / `-j FILE` | Write structured JSON data |
| `--template FILE` | Use custom dashboard HTML template |

When using `--board-id` with no explicit output flags, all three files are auto-generated with names derived from the board name.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `JIRA_URL` | Yes (for API modes) | Jira Cloud base URL |
| `JIRA_EMAIL` | Yes (for API modes) | Email for API authentication |
| `JIRA_API_TOKEN` | Yes (for API modes) | [API token](https://id.atlassian.com/manage-profile/security/api-tokens) for authentication |
| `JIRA_FIELD_STORY_POINTS` | No | Custom field ID for Story Points (default: `customfield_10028`) |
| `JIRA_FIELD_EPIC_LINK` | No | Custom field ID for Epic Link (default: `customfield_10014`) |
| `JIRA_FIELD_TEAM` | No | Custom field ID for Team (default: `customfield_10001`) |
| `JIRA_FIELD_SPRINT` | No | Custom field ID for Sprint (default: `customfield_10020`) |

## Technical Details

- **Zero dependencies** — Uses only Python standard library (`urllib`, `json`, `csv`, `argparse`)
- **Self-contained dashboard** — Single HTML file with embedded Chart.js and all data inline
- **Jira Cloud API v3** — Works with any Atlassian Cloud instance
- **Custom field mapping** — Configurable via environment variables for different Jira setups

## License

MIT
