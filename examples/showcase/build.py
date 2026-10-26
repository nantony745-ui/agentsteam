"""Build fictional, reproducible showcase files without using any model service."""
import csv
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def put(name, content):
    target = ROOT / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content.strip() + "\n", encoding="utf-8")


ROLES = [
    ("Product Strategist", "codex", "Product direction, scope and launch coordination",
     "Turn an ambiguous brief into a decision question, audience, outcome metrics and explicit non-goals. Coordinate specialists, resolve dependencies and keep a decision log. Delegate implementation and independent review rather than owning every file.",
     "A one-page brief, owner map, measurable acceptance criteria, a launch checklist and a decision with its rationale."),
    ("Experience Designer", "codebuddy", "Information architecture, interaction design and accessibility",
     "Map the user journey and information hierarchy. Specify loading, empty, error, validation and success states. Review keyboard access, long content, narrow layouts and visual consistency with existing components. Explain the cost of each design tradeoff.",
     "User-flow notes, component-state specifications, content hierarchy and an accessibility checklist."),
    ("Frontend Engineer", "opencode", "Responsive interfaces, state transitions and browser verification",
     "Read the existing structure and styles before implementing an agreed design. Cover responsive layouts, keyboard focus, disabled controls and failure states. Coordinate API assumptions with the owner. Keep business rules separate from decorative markup.",
     "Working local pages, a file-level change summary and actual browser verification, with untested conditions named."),
    ("Quality Reviewer", "codex", "Independent acceptance review and release readiness",
     "Inspect the stated acceptance criteria, boundary cases, integrity and accessibility. Separate reproducible defects from optional suggestions. Wait for a review handoff before judging another member's unfinished work. Do not silently implement the fixes you review.",
     "Prioritized findings, reproduction steps, expected versus observed behavior, file references and a clear acceptance recommendation."),
    ("Technical Writer", "deepseek", "User guidance, terminology and source traceability",
     "Identify the reader and their next action. Verify commands and prerequisites against supplied files. Separate implemented behavior from planned work. Use concrete examples and portable paths. Remove personal identifiers and obsolete deployment records from publishable material.",
     "A concise first-use guide, worked examples, troubleshooting, checked links and a source trail for important claims."),
    ("Research Lead", "codex", "Research framing, competing explanations and decision support",
     "Start from a decision question and an evidence threshold. Decompose it into answerable subquestions. Delegate evidence coding and quantitative checks. Track contradictory findings and distinguish observation, inference and recommendation. Preserve uncertainty instead of inventing precision.",
     "A research brief, evidence matrix, competing explanations, limitations and a decision memo. Synthetic interviews must stay labeled synthetic."),
    ("Evidence Analyst", "deepseek", "Source quality, interview coding and claim auditing",
     "Assess provenance, date, coverage and incentives before using a source. Code excerpts with a shared rubric and preserve source identifiers. Look for negative cases. Never treat repeated statements from one source as independent corroboration or invent a missing citation.",
     "A claim-to-source matrix, confidence labels, counterevidence and unresolved questions, with direct quotations separated from interpretation."),
    ("Data Analyst", "codex", "Metric definitions, reproducible calculations and visual explanation",
     "Inspect the data dictionary, missing values, units and denominators before analysis. Recompute metrics from source tables. Explain chart axes and coverage. Separate descriptive change from causal explanations, and report rounding rather than implying unsupported precision.",
     "A validated metric table, calculation notes, readable charts and a list of analytical limitations. Sample records are never production evidence."),
    ("Operations Lead", "codebuddy", "Operating reviews, capacity planning and accountable follow-through",
     "Review demand, capacity, quality and cost together. Turn signals into a small action register with owners, review dates and escalation conditions. Require denominator checks and feasibility evidence before recommending a change. Track whether actions resolve the original problem.",
     "A weekly operating review, an accountable action register, competing explanations and the evidence required before any real decision."),
    ("Knowledge Architect", "deepseek", "Learning design, knowledge organization and practical assessment",
     "Start with learner outcomes and prior knowledge. Sequence concepts into a practical learning path. Connect each objective to an activity and assessment. Check timing, prerequisite gaps and accessibility. Organize references so a new reader can work without tribal knowledge.",
     "A workshop agenda, facilitator notes, concept map, exercises and an assessment rubric grounded in observable work."),
]

TEAMS = [
    {"name": "Product Launch Studio", "leader": "Product Strategist", "project": "atlas-product-launch", "members": ["Product Strategist", "Experience Designer", "Frontend Engineer", "Quality Reviewer", "Technical Writer"]},
    {"name": "Research & Evidence Desk", "leader": "Research Lead", "project": "field-notes-research", "members": ["Research Lead", "Evidence Analyst", "Data Analyst", "Technical Writer"]},
    {"name": "Weekly Operations Council", "leader": "Operations Lead", "project": "northstar-operations", "members": ["Operations Lead", "Data Analyst", "Evidence Analyst", "Technical Writer"]},
    {"name": "Learning Design Workshop", "leader": "Knowledge Architect", "project": "learning-path-workshop", "members": ["Knowledge Architect", "Research Lead", "Experience Designer", "Technical Writer"]},
]


def build():
    profiles = []
    for name, harness, role, mission, deliverables in ROLES:
        instructions = f"""MISSION
{mission}

DELIVERABLE CONTRACT
{deliverables}

WORKING PROTOCOL
1. Read the brief, inputs and existing outputs before acting. Identify the audience, decision, scope and acceptance criteria.
2. State what is known, what is assumed and what must be checked. Ask the coordinator when a missing fact could change the result.
3. Respect file ownership and other members' work. Coordinate handoffs explicitly. Team members do not delegate further.
4. Use checks appropriate to the task. Record the actual method, result and limitations. A completed run is not an accepted deliverable.
5. Hand back findings, evidence, output files, verification and open questions. Distinguish proposed decisions from approved decisions.

EVIDENCE AND QUALITY
Use source IDs and concrete examples. Preserve contradictory evidence and uncertainty. Never fabricate metrics, citations, completed tests or customer feedback. All records in this showcase are authored synthetic material, not real Agent execution or production evidence.

REUSABLE LEARNING
After a verified lesson, update a focused personal skill with its trigger, steps, evidence and failure cases. Prefer a small reusable procedure to a diary of this task. Explain what would invalidate the method before applying it elsewhere."""
        profiles.append(dict(name=name, role=role, instructions=instructions, harness=harness, config_values={}))
    put("roles.json", json.dumps(profiles, indent=2))
    put("teams.json", json.dumps(TEAMS, indent=2))
    for team in TEAMS:
        put(f"projects/{team['project']}/README.md", f"# {team['project']}\n\nTeam: **{team['name']}**.\n\nAll records are synthetic showcase material. Files are real local artifacts; their existence does not imply an Agent executed the authored conversation.")
    put("projects/atlas-product-launch/brief.md", """# Atlas / Launch brief
SYNTHETIC SHOWCASE. No real customers or commercial records.

## Decision and audience
Can a small product team communicate a focused offer in one responsive page? The audience is independent teams organizing research, decisions and delivery.

## Scope and acceptance
- A concrete hero statement with local navigation actions.
- Three workflow cards: Explore, Align, Deliver.
- Semantic headings, readable contrast and a narrow-screen layout.
- No signup service, tracking, external requests or unverified adoption claims.

## Ownership
Product Strategist: scope and decisions. Experience Designer: hierarchy and states. Frontend Engineer: local page. Quality Reviewer: independent checks. Technical Writer: handoff guide.
""")
    put("projects/atlas-product-launch/release-checklist.md", """# Local concept / Readiness checklist

- [x] Create the HTML page and working navigation anchors.
- [x] Use semantic sections and a responsive layout.
- [x] Include no account collection, tracking or remote assets.
- [ ] Observe real users before selecting an offer.
- [ ] Implement and verify a real signup service before claiming availability.

This is a local concept review. A screenshot is evidence of the sample layout, not evidence of customer demand or production readiness.
""")
    put("projects/atlas-product-launch/index.html", """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Atlas / Launch concept</title><style>*{box-sizing:border-box}body{margin:0;background:#f5f4ef;color:#243429;font:16px/1.6 system-ui}main,nav{max-width:1200px;margin:auto;padding:28px 40px}nav{display:flex;justify-content:space-between;border-bottom:1px solid #ddd}a{color:inherit;text-decoration:none}.logo{font-size:25px;font-weight:750}.tag{border:1px solid #bfcabb;border-radius:20px;padding:6px 12px;font-size:11px}.hero{padding:38px 0;max-width:850px}h1{font-size:68px;line-height:1.08;letter-spacing:-3px;margin:24px 0}em{color:#52765b;font-style:normal}.sub{font-size:20px;max-width:650px;color:#667468}.button{display:inline-block;background:#35583e;color:white;padding:12px 22px;border-radius:8px;margin:15px 16px 0 0}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:22px;margin:25px 0}.card{background:white;border:1px solid #dde3d8;border-radius:16px;padding:25px}.number{color:#68846d;letter-spacing:2px;font-size:12px}h2{font-size:25px}.muted{color:#68756a}footer{border-top:1px solid #d8dfd4;padding-top:17px;font-size:12px;color:#6b786d}@media(max-width:700px){h1{font-size:42px}.cards{grid-template-columns:1fr}}</style><nav><span class="logo">atlas.</span><div><a href="#workflow">Workflow</a> &nbsp; <a href="#principles">Our approach</a> &nbsp; <span class="tag">SYNTHETIC SHOWCASE</span></div></nav><main><section class="hero"><span class="tag">A PRODUCT LAUNCH EXPLORATION</span><h1>Make room for<br><em>the work that matters.</em></h1><p class="sub">Bring research, decisions and delivery into a shared rhythm. Start with a clear brief and leave with a result you can explain.</p><a class="button" href="#workflow">Explore the workflow ↗</a><a href="#principles">Read the principles →</a></section><section id="workflow" class="cards"><article class="card"><span class="number">01 / EXPLORE</span><h2>Find the right question.</h2><p class="muted">Collect evidence, compare perspectives and make assumptions visible before choosing a direction.</p></article><article class="card"><span class="number">02 / ALIGN</span><h2>Give every role a purpose.</h2><p class="muted">Set ownership, define acceptance and make handoffs clear enough for another teammate to use.</p></article><article class="card"><span class="number">03 / DELIVER</span><h2>Show the work behind it.</h2><p class="muted">Check the result and keep decisions connected to the evidence that supports them.</p></article></section><footer id="principles">LOCAL CONCEPT · Synthetic content · No signup, tracking or production claims · Rendered from repository files</footer></main></html>""")
    put("projects/field-notes-research/research-brief.md", """# Field Notes / Research brief
SYNTHETIC DATASET. Authored excerpts, not real customer interviews.

## Decision question
Which handoffs should a small knowledge-work team improve first?

## Protocol
Code six fictional excerpts with a common rubric. Compare decision impact and look for counterexamples. Use descriptive counts only: this is not a representative sample.

## Questions
Where does context get lost? Which claims lack a source? What makes a result reusable by the next person?

## Outputs
An evidence matrix, competing explanations, a decision memo and explicit limitations.
""")
    put("projects/field-notes-research/interviews.csv", """source_id,role,excerpt,theme
SYN-01,Coordinator,We repeat the goal when a new person joins.,context handoff
SYN-02,Analyst,I cannot find the denominator behind this chart.,metric provenance
SYN-03,Editor,A clear checklist reduces revision loops.,acceptance clarity
SYN-04,Designer,Decisions are buried in long conversations.,decision traceability
SYN-05,Facilitator,Some small tasks work well with one owner.,counterexample
SYN-06,Researcher,Source IDs make findings easier to challenge.,evidence traceability""")
    put("projects/field-notes-research/evidence-matrix.md", """# Evidence matrix
All source IDs refer to fictional excerpts in `interviews.csv`.

| Claim | Sources | Counterevidence / caveat | Confidence |
| --- | --- | --- | --- |
| Structured handoffs may reduce repeated context | SYN-01, SYN-03 | No before/after observation | Exploratory |
| Review needs a traceable evidence trail | SYN-02, SYN-06 | Synthetic sample; no prevalence estimate | Exploratory |
| Every task needs several roles | None | SYN-05 supports a single-owner path | Unsupported |
| A decision log may improve findability | SYN-04 | Usability has not been tested | Hypothesis |

## Next study
Observe three real handoffs with consent. Compare missing information and time-to-context before choosing an intervention. Do not infer market demand from this sample.
""")
    put("projects/field-notes-research/decision-memo.md", """# Decision memo / Improve the handoff first
Status: illustrative recommendation from synthetic material.

## Recommendation
Prototype a short handoff card: goal, context, owner, acceptance criteria and evidence links. Preserve a single-owner path for small tasks.

## Rationale
The sample separates context recovery, metric provenance and decision findability. A small handoff experiment can be tested without redesigning the entire workspace.

## Reversal condition
If real observations show that context is already recovered quickly, prioritize another bottleneck. Shorten or abandon the card if maintenance costs exceed its value.

## Limits
Six authored excerpts are not six real participants. No causal effect, customer preference or market prevalence has been measured.
""")
    rows = [("W01",84,6300,3780,22),("W02",92,7100,4189,20),("W03",101,7900,4582,19),("W04",108,8400,4788,17),("W05",119,9300,5208,16),("W06",126,10100,5555,15)]
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["week","orders","revenue_usd","cost_usd","support_tickets"])
    writer.writerows(rows)
    put("projects/northstar-operations/weekly.csv", buf.getvalue())
    revenue, cost = sum(r[2] for r in rows), sum(r[3] for r in rows)
    orders, tickets = sum(r[1] for r in rows), sum(r[4] for r in rows)
    metrics = dict(dataset="synthetic demonstration only", weeks=6, orders=orders, revenue_usd=revenue, cost_usd=cost, gross_margin_pct=round((1-cost/revenue)*100,2), ticket_rate_pct=round(tickets/orders*100,2))
    put("projects/northstar-operations/metrics.json", json.dumps(metrics, indent=2))
    put("projects/northstar-operations/data-dictionary.md", """# Data dictionary
Synthetic weekly records for a fictional operating review.

- week: ordered reporting period, W01 to W06.
- orders: fulfilled orders, integer count.
- revenue_usd: illustrative recognized revenue, USD.
- cost_usd: direct cost, USD; excludes overhead.
- support_tickets: tickets opened; not unique customers.

Gross margin = (revenue - direct cost) / revenue. Ticket rate = tickets / orders. Neither is a causal measure. The sample excludes seasonality, returns and acquisition spend.
""")
    put("projects/northstar-operations/operating-review.md", f"""# Northstar / Six-week operating review
SYNTHETIC DEMONSTRATION. No real sales or company records.

## Scorecard
| Metric | Value | Definition |
| --- | --- | --- |
| Orders | {orders} | Sum of six weekly counts |
| Revenue | ${revenue:,} | Sum of sample revenue |
| Gross margin | {metrics['gross_margin_pct']}% | Revenue-weighted; direct cost only |
| Ticket rate | {metrics['ticket_rate_pct']}% | Tickets / orders; not unique customers |

## Interpretation
Revenue increases while ticket counts fall. This is descriptive co-movement, not evidence that one caused the other.

## Action register
| Owner | Action | Evidence required |
| --- | --- | --- |
| Data Analyst | Validate denominators and coverage | Returns, channel mix and definitions |
| Operations Lead | Review delivery capacity | Lead time and utilization |
| Evidence Analyst | Test competing explanations | Seasonality and changing mix |
| Technical Writer | Publish the metric glossary | Definition owner sign-off |

## Limits
Small synthetic dataset, no uncertainty estimate and no causal attribution. No production decision is authorized by this report.
""")
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="380"><rect width="1100" height="380" fill="white"/><g font-family="sans-serif" fill="#263c5c"><text x="36" y="35" font-size="21">Revenue by week</text><text x="36" y="59" font-size="12" fill="#7888a0">USD · SYNTHETIC DATA · descriptive comparison only</text>']
    for i, row in enumerate(rows):
        x, height = 65+i*170, row[2]/11000*220
        svg.append(f'<rect x="{x}" y="{315-height}" width="100" height="{height}" rx="7" fill="{["#b9caf6","#a2b9f0","#8aa7eb","#7296e4","#5b82db","#456fd1"][i]}"/><text x="{x+50}" y="{303-height}" text-anchor="middle" font-size="15">${row[2]:,}</text><text x="{x+50}" y="347" text-anchor="middle" font-size="14">{row[0]}</text>')
    put("projects/northstar-operations/revenue.svg", "".join(svg)+"</g></svg>")
    stats = "".join(f'<article class="card"><div class="label">{label}</div><div class="value">{value}</div><small>{note}</small></article>' for label,value,note in [("REVENUE",f"${revenue:,}","Six sample periods · USD"),("FULFILLED ORDERS",str(orders),"Counts summed across periods"),("GROSS MARGIN",f"{metrics['gross_margin_pct']:.1f}%","Weighted by revenue"),("TICKETS / ORDERS",f"{metrics['ticket_rate_pct']:.1f}%","Not unique affected customers")])
    put("projects/northstar-operations/dashboard.html", '''<!doctype html><html lang="en"><meta charset="utf-8"><title>Northstar / Operating review</title><style>*{box-sizing:border-box}body{margin:0;background:#eef2f8;color:#203550;font:15px/1.6 system-ui}main{max-width:1280px;margin:auto;padding:36px 45px}header{display:flex;justify-content:space-between;align-items:start}.tag{background:#dce5f9;color:#4168af;padding:6px 13px;border-radius:20px;font-size:11px}h1{font-size:37px;margin:7px 0}.muted,small{color:#71819a}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:18px;margin:25px 0}.card{background:white;border:1px solid #dee5ef;border-radius:15px;padding:21px}.label{font-size:11px;color:#71809a;font-weight:700;letter-spacing:1px}.value{font-size:33px;font-weight:750;margin:7px 0}.bottom{display:grid;grid-template-columns:1.2fr 1fr;gap:20px;margin-top:20px}h2{font-size:18px;margin:0 0 10px}img{width:100%;display:block}td,th{text-align:left;padding:9px;border-bottom:1px solid #e4eaf2;font-size:12px}table{width:100%;border-collapse:collapse}footer{margin-top:19px;font-size:12px;color:#6d7b92}</style><main><header><div><div class="label">NORTHSTAR / OPERATING REVIEW</div><h1>Six weeks. One clear picture.</h1><div class="muted">Reproducible metrics from the sample CSV, with definitions and limitations.</div></div><span class="tag">SYNTHETIC SHOWCASE DATA</span></header><section class="stats">'''+stats+'''</section><section class="card"><img src="revenue.svg" alt="Six weekly revenue bars, from 6300 to 10100 USD"></section><section class="bottom"><article class="card"><h2>What the sample shows</h2><p>Revenue rises while support ticket counts fall. The two trends occur together; the data does not establish a causal relationship.</p><small>Missing context: acquisition mix, returns, seasonality and capacity.</small></article><article class="card"><h2>Next review / Action register</h2><table><tr><th>Owner</th><th>Next step</th></tr><tr><td>Data Analyst</td><td>Check definitions and coverage</td></tr><tr><td>Operations Lead</td><td>Review delivery capacity</td></tr><tr><td>Evidence Analyst</td><td>Test competing explanations</td></tr></table></article></section><footer>Actual local HTML + SVG · Fictional records only · No external assets or production recommendation</footer></main></html>''')
    put("projects/learning-path-workshop/agenda.md", """# Evidence-first collaboration / Workshop agenda
Synthetic learning project: 90 minutes, no participant records.

| Time | Activity | Observable outcome |
| --- | --- | --- |
| 00-10 | Compare two handoff examples | Identify missing goal, owner and acceptance |
| 10-25 | Mark assumptions in a brief | Separate facts from interpretation |
| 25-45 | Build an evidence card | Link a claim to a source and limitation |
| 45-65 | Peer review with a shared rubric | Produce a reproducible finding |
| 65-80 | Revise the recommendation | Show how evidence changes a decision |
| 80-90 | Reflect and transfer | Choose one practical improvement |

## Facilitator notes
Use the synthetic research excerpts and operations CSV in this showcase. Explain why evidence matters more than presentation confidence. Offer a written alternative for asynchronous participation.
""")
    put("projects/learning-path-workshop/rubric.md", """# Assessment rubric

| Dimension | Developing | Proficient | Strong |
| --- | --- | --- | --- |
| Framing | Goal is vague | Goal and audience are clear | Decision and non-goals are explicit |
| Evidence | Claims lack sources | Key claims link to IDs | Counterevidence and uncertainty are addressed |
| Handoff | Missing next steps | Owner, files and checks are named | Another role can act without rebuilding context |
| Reflection | General impressions | One actionable improvement | Change follows an observed difficulty |

Assess observable work, not confidence. This illustrative rubric is not a validated psychometric instrument.
""")
    put("projects/learning-path-workshop/concept-map.svg", '''<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="430"><rect width="1100" height="430" rx="16" fill="#f6f5ef"/><g font-family="sans-serif" fill="#283b32"><text x="40" y="55" font-size="26" font-weight="700">From a question to a useful handoff</text><text x="40" y="83" font-size="13">Synthetic workshop concept map</text><path d="M240 220H305M490 220H555M740 220H805" stroke="#aec3b4" stroke-width="3"/><rect x="40" y="145" width="200" height="155" rx="16" fill="#deeadf"/><rect x="305" y="145" width="185" height="155" rx="16" fill="#e0e8f7"/><rect x="555" y="145" width="185" height="155" rx="16" fill="#eee2f2"/><rect x="805" y="145" width="245" height="155" rx="16" fill="#f4e7cb"/><g font-size="20" text-anchor="middle"><text x="140" y="190">FRAME</text><text x="397" y="190">CHECK</text><text x="647" y="190">EXPLAIN</text><text x="927" y="190">HAND OFF</text></g><g font-size="14" text-anchor="middle"><text x="140" y="235">Goal + audience</text><text x="140" y="265">Acceptance + scope</text><text x="397" y="235">Sources + definitions</text><text x="397" y="265">Counterevidence</text><text x="647" y="235">Decision + rationale</text><text x="647" y="265">Limits + tradeoffs</text><text x="927" y="235">Owner + files + checks</text><text x="927" y="265">Next action + questions</text></g><text x="40" y="370" font-size="14">Can another teammate use the output without reconstructing the original conversation?</text></g></svg>''')
    print(json.dumps(dict(roles=len(profiles), teams=len(TEAMS), projects=4, metrics=metrics)))


if __name__ == "__main__":
    build()
