"""Read-only, isolated showcase using the actual application and synthetic records."""
import argparse
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def make_app(runtime):
    # Refuse reuse: no existing database or personal data is ever imported.
    runtime.mkdir(parents=True, exist_ok=False)
    home = runtime / "home"
    home.mkdir()
    (runtime / "projects").mkdir()
    for key in ("HOME", "USERPROFILE", "CODEX_HOME", "APPDATA", "LOCALAPPDATA"):
        os.environ[key] = str(home / key.lower())
        Path(os.environ[key]).mkdir()
    for key in ("DSH_HOME", "DSH_AGENTS_HOME", "XDG_CONFIG_HOME", "OPENCODE_CONFIG_DIR"):
        os.environ[key] = str(home / key.lower())
    os.environ.pop("OPENCODE_CONFIG", None)
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    from fastapi.staticfiles import StaticFiles
    from app.agents import acp_client
    from app.domain.employees import EmployeeInput
    from app.domain.teams import TeamInput
    from app.domain.models import ProjectCreate, SessionCreate, MessageCreate, RunStatus
    from app.main import create_app

    config = runtime / "agents.toml"
    config.write_text("\n".join(
        f'[agents.{name}]\nexecutable = "showcase-disabled"\nargs = []\nenabled = true\n'
        for name in ("codex", "opencode", "codebuddy", "deepseek")), encoding="utf-8")
    acp_client.CONFIG_PATH = config

    class NoAgent:
        def __init__(self, *args, **kwargs):
            (runtime / "agent-start-attempt.txt").write_text("Blocked", encoding="utf-8")
            raise RuntimeError("Agent execution is disabled in the showcase")

    app = create_app(runtime / "data/showcase.db", client_factory=NoAgent)
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            storage, employees, teams = application.state.storage, application.state.employees, application.state.teams
            profiles = {}
            for data in json.loads((HERE / "roles.json").read_text()):
                profile = employees.create(EmployeeInput(**data))
                profiles[data["name"]] = profile
                skill_dir = runtime / "data/employees" / profile["employee_id"] / "skills/evidence-handoff"
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text('''---
name: evidence-handoff
description: Prepare a traceable handoff with sources, checks and decision limits.
---
# Evidence-based handoff

## When to use
Use after a research, analysis, design or learning task is ready for another role.

## Procedure
1. Restate the decision, audience, scope and acceptance criteria.
2. Link each important claim to a source ID or a reproducible calculation.
3. Separate observations, interpretations and recommendations.
4. Record counterevidence, missing inputs and what could reverse the decision.
5. Name the next owner, output files, actual checks and unresolved questions.

## Example
Claim: the sample ticket rate is 17.3%. Source: weekly.csv.
Method: 109 tickets / 630 fulfilled orders. Limit: tickets are not unique customers.
Next owner: Operations Lead. Check coverage before making any real decision.

## Failure cases
Do not invent a source, imply a causal effect or label an unchecked result accepted.
This skill is authored synthetic showcase material, not a record of Agent learning.
''', encoding="utf-8")
            metadata = []
            for spec in json.loads((HERE / "teams.json").read_text()):
                path = runtime / "projects" / spec["project"]
                shutil.copytree(HERE / "projects" / spec["project"], path)
                team = await teams.save(TeamInput(name=spec["name"], member_ids=[profiles[n]["employee_id"] for n in spec["members"]], leader_id=profiles[spec["leader"]]["employee_id"]))
                project = await storage.create_project(ProjectCreate(path=str(path), team_id=team["id"]), team_snapshot=await teams.snapshot(team["id"]))
                sessions = []
                for name in spec["members"]:
                    session = await teams.create_session(SessionCreate(project_id=project["id"], employee_id=profiles[name]["employee_id"], leader_session_id=sessions[0]["id"] if sessions else None))
                    sessions.append(session)
                    content, filename = narrative(spec["project"], name)
                    briefs = {
                        "atlas-product-launch": "Prepare Atlas for a local concept review. Keep Explore / Align / Deliver, make ownership explicit, cover narrow screens and avoid implying that signup is live.",
                        "field-notes-research": "Which handoff should we improve first? Compare the six supplied excerpts, retain the single-owner counterexample and recommend a small study before making a product decision.",
                        "northstar-operations": "Prepare our six-week operating review from weekly.csv. Recompute margin and ticket rate, separate trends from causes, then assign the missing-data checks before any capacity decision.",
                        "learning-path-workshop": "Design a 90-minute workshop on evidence-first collaboration. Connect each objective to an exercise and assessment, using the research excerpts and operations CSV as practice material."
                    }
                    prompt = f"[SYNTHETIC SHOWCASE] {briefs[spec['project']]} Your role: {name}."
                    run, _ = await storage.create_run(session["id"], MessageCreate(message=prompt, request_id=str(uuid4())))
                    await storage.transition_run(run["id"], {RunStatus.QUEUED}, RunStatus.RUNNING, {})
                    await storage.append_event(run["id"], "session.title", {"title": name + " / " + spec["name"]})
                    await storage.append_event(run["id"], "agent.plan", {"entries": [{"content": text, "status": "completed", "priority": "medium"} for text in ("Read the brief and acceptance criteria", "Compare evidence and competing explanations", "Prepare files, checks and a clear handoff")]})
                    file = path / filename
                    await storage.append_event(run["id"], "agent.tool", {"toolCallId": str(uuid4()), "title": "Review supplied artifact / authored fixture", "kind": "read", "status": "completed", "locations": [{"path": str(file)}], "content": [{"type": "content", "content": {"type": "text", "text": "Synthetic event fixture; no tool or Agent was executed."}}]})
                    await storage.append_event(run["id"], "agent.tool", {"toolCallId": str(uuid4()), "title": "Illustrative artifact change / authored fixture", "kind": "edit", "status": "completed", "content": [{"type": "diff", "path": str(file), "oldText": "", "newText": file.read_text(encoding="utf-8")}]})
                    await storage.append_event(run["id"], "agent.text", {"channel": "message", "text": content})
                    await storage.transition_run(run["id"], {RunStatus.RUNNING}, RunStatus.COMPLETED, {"result": content})
                metadata.append({"project": project, "team": team, "sessions": sessions})
            (runtime / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
            yield
    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def read_only(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            return JSONResponse({"detail": "Showcase is read-only; Agent execution disabled"}, status_code=403)
        return await call_next(request)

    static = app.router.routes.pop()
    @app.get("/")
    async def index():
        html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("</head>", '<script src="/showcase-locale.js" defer></script></head>'))

    @app.get("/showcase-locale.js")
    async def locale():
        return FileResponse(HERE / "locale-en.js", media_type="text/javascript")
    app.mount("/artifacts", StaticFiles(directory=runtime / "projects", html=True))
    app.router.routes.append(static)
    return app


def narrative(project, name):
    prefix = f"### {name} / Handoff\n\n*Authored synthetic showcase. No Agent execution.*\n\n"
    specialist = {
        ("field-notes-research", "Evidence Analyst"): ("""#### Coding and claim audit
| Code | Evidence | Interpretation |
| --- | --- | --- |
| Context recovery | SYN-01 | Brief may be incomplete |
| Acceptance clarity | SYN-03 | Checklist may reduce revisions |
| Traceability | SYN-02, SYN-06 | Definitions and source IDs matter |
| Negative case | SYN-05 | Preserve a single-owner path |

**Audit result.** The claim that every task needs multiple roles is unsupported. SYN-05 directly challenges it. No excerpt establishes a causal effect.

**Competing explanation.** Handoff friction could reflect unclear acceptance rather than too few collaborators. Observe both before changing team size.

**Handoff to Research Lead.** Use exploratory confidence; do not count synthetic excerpts as real participants. See `evidence-matrix.md`.
""", "evidence-matrix.md"),
        ("learning-path-workshop", "Research Lead"): ("""#### Exercise design / Challenge the claim
**Prompt:** An attractive chart says support quality improved. What evidence would justify that conclusion?

1. Read the six-week CSV and data dictionary.
2. Recompute tickets / orders and identify its denominator.
3. List missing returns, channel mix and unique-customer counts.
4. Write one alternative explanation and one follow-up observation.

| Evidence card | Acceptance |
| --- | --- |
| Claim and source | Links to a supplied row or ID |
| Calculation | Another learner can reproduce it |
| Limitation | Changes the strength of the conclusion |

**Facilitator handoff.** Compare two answers against the rubric. Offer a written response path and avoid scoring confidence as competence.
""", "rubric.md"),
    }
    if (project, name) in specialist:
        content, filename = specialist[(project, name)]
        return prefix + content, filename
    if project == "northstar-operations":
        return prefix + """#### Six-week operating review
| Metric | Result | Definition |
| --- | ---: | --- |
| Revenue | $49,100 | Six sample weeks |
| Fulfilled orders | 630 | Sum of weekly counts |
| Gross margin | 42.77% | Direct cost only |
| Tickets / orders | 17.30% | Not unique customers |

**Interpretation.** Revenue rises while ticket counts fall. This is descriptive co-movement; the dataset does not establish causation.

**Handoff.** Data Analyst checks denominators; Operations Lead reviews capacity; Evidence Analyst tests seasonality and mix. Keep returns and acquisition spend explicit as missing inputs.

**Files:** `weekly.csv`, `metrics.json`, `dashboard.html`, `operating-review.md`.
""", "operating-review.md"
    if project == "field-notes-research":
        return prefix + """#### Improve the handoff first
| Finding | Sources | Confidence |
| --- | --- | --- |
| Repeated context needs a clearer brief | SYN-01, SYN-03 | Exploratory |
| Review needs traceable definitions | SYN-02, SYN-06 | Exploratory |
| Small tasks may need only one owner | SYN-05 | Counterexample |

**Recommendation.** Prototype a short card with goal, owner, acceptance criteria and evidence links. Keep the single-owner path.

**Next study.** Observe three real handoffs with consent before choosing an intervention. Six authored excerpts cannot establish prevalence or customer demand.

**Handoff:** evidence matrix to the reviewer; decision memo to the coordinator.
""", "evidence-matrix.md"
    if project == "learning-path-workshop":
        return prefix + """#### Evidence-first collaboration / 90 minutes
| Stage | Activity | Observable output |
| --- | --- | --- |
| Frame | Compare two briefs | Goal + acceptance |
| Check | Audit a sample claim | Source + limitation |
| Explain | Peer review a decision | Reproducible finding |
| Hand off | Revise and transfer | Owner + next action |

**Assessment.** Score framing, evidence, handoff and reflection against the supplied rubric. Judge observable work rather than confidence.

**Facilitation.** Offer an asynchronous written path. Use the synthetic interview and operations datasets; collect no participant records.

**Files:** `agenda.md`, `rubric.md`, `concept-map.svg`.
""", "agenda.md"
    variants = {
        "Product Strategist": """#### Launch direction
**Audience:** small teams coordinating research, decisions and delivery.

| Owner | Deliverable | Acceptance |
| --- | --- | --- |
| Design | Content hierarchy | Clear primary path |
| Frontend | Responsive page | Working local anchors |
| Review | Independent findings | Reproducible evidence |

**Decision.** Keep Explore → Align → Deliver. Omit signup and adoption claims until a real service and evidence exist.

**Handoff.** Review the local concept against the brief. A completed round does not mean production readiness.
""",
        "Experience Designer": """#### Experience specification
**Hierarchy:** concise promise → workflow cards → operating principles.

1. Give Explore, Align and Deliver equal visual weight.
2. Keep primary actions as local navigation anchors.
3. Use semantic headings, readable contrast and keyboard access.
4. Collapse the card grid on narrow screens.

**States.** No account form or backend state is implied. Long copy wraps within each card.

**Handoff to Frontend.** Keep the green/cream palette and generous spacing; preserve the synthetic label.
""",
        "Frontend Engineer": """#### Local page handoff
`index.html` contains the complete concept, including CSS and local anchor navigation. No build step, tracking or external assets.

| Area | Implementation |
| --- | --- |
| Hero | Clear statement and two anchors |
| Workflow | Three semantic cards |
| Responsive | One-column layout below 700px |
| Footer | Explicit concept limitations |

**Review boundary.** Rendering demonstrates the supplied page. Real user testing, signup and production delivery remain outside this concept.

**Next owner:** Quality Reviewer, using `release-checklist.md`.
"""}
    return prefix + variants.get(name, "#### Review handoff\nRead the brief, check each acceptance item and preserve source links. Record reproducible findings and limitations in `release-checklist.md`. No signup or production readiness is claimed.\n"), "release-checklist.md"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8876)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(make_app(args.runtime.resolve()), host="127.0.0.1", port=args.port)
