# Backlog Analyzer

An AI-powered Agile Coach that analyzes Jira backlogs for quality, sprint readiness, and AI-readiness — then generates an interactive HTML dashboard with coaching insights.

![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)
![No Dependencies](https://img.shields.io/badge/dependencies-none-green.svg)

## Quick Start

The simplest way to run it — just provide your Jira board ID:

```bash
export JIRA_URL=https://your-org.atlassian.net
export JIRA_EMAIL=you@example.com
export JIRA_API_TOKEN=your-api-token

python backlog_analyzer.py --board-id 1131
```

This will:
1. Fetch all issues from the board
2. Auto-discover active and planned sprints
3. Categorize items into tiers (active sprint / planned sprint / backlog)
4. Generate three output files named after your board:
   - `your_board_name_report.md` — Markdown report
   - `your_board_name_data.json` — Structured JSON data
   - `your_board_name_dashboard.html` — Self-contained interactive dashboard

## Features

### 12-Section Interactive Dashboard
- **Executive Summary** — Type breakdown, quality metrics, readiness score
- **Backlog at a Glance** — Three-tier sprint view with per-tier coaching
- **Key Concerns** — Poorly defined items, stale items, missing epics
- **Recommended Actions** — Prioritized immediate, short-term, and automatable actions
- **Story Size & Decomposition** — Point distribution, splitting recommendations
- **Dependency Signals** — Link analysis, bottleneck detection
- **Backlog Organization** — Status/priority distribution, duplicate detection
- **Readiness Score** — Composite 1-5 score across 5 quality factors
- **AI Refinement Readiness** — How ready items are for AI-assisted refinement
- **AI Code Gen Readiness** — How ready items are for AI code generation
- **Coaching Insights** — Story writing templates, refinement session agendas
- **All Items** — Searchable, filterable table of every backlog item

### Three-Tier Sprint Analysis
When using `--board-id`, the tool automatically discovers sprints and provides tier-specific coaching:

| Tier | Tone | Focus |
|------|------|-------|
| **Active Sprint** | Urgent — execute NOW | Blockers, unestimated work, missing assignments |
| **Planned Sprint** | Preparatory — must meet Definition of Ready | AC gaps, estimation gaps, dependency risks |
| **Backlog** | Strategic — groom, close, or refine | Stale items, missing epics, prioritization |

### Scoring System
- **Backlog Readiness (1-5):** Description quality, acceptance criteria, story points, freshness, epic linkage
- **AI Refinement Readiness (1-5):** Description depth, AC presence, epic context
- **AI Code Gen Readiness (1-5):** Technical context, testable AC, bounded scope, component mapping, no blockers, estimated

## Input Modes

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

## Requirements

- Python 3.10+
- No external dependencies (uses only Python standard library)

## License

MIT
