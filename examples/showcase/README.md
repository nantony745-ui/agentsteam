# English showcase / Reproducible local examples

Four fictional projects, four saved teams and ten detailed teammate profiles demonstrate product work, research, operations and learning design. Every dataset, conversation and tool event is **authored synthetic material**. No Agent produced these records, no model is called and no business data is imported.

The app screenshots render the repository's actual frontend. `locale-en.js` translates DOM labels only in this isolated preview, without adding controls or changing the application source. This does **not** mean the product ships native English internationalization. Artifact screenshots render the real HTML and SVG files below in the browser; they are separate deliverables, not built-in dashboard features.

| Project | Team and roles | Actual files |
| --- | --- | --- |
| [Atlas launch](projects/atlas-product-launch/) | Product Strategist, Experience Designer, Frontend Engineer, Quality Reviewer, Technical Writer | Brief, release checklist, responsive local product page |
| [Field Notes research](projects/field-notes-research/) | Research Lead, Evidence Analyst, Data Analyst, Technical Writer | Six fictional excerpts, evidence matrix, decision memo |
| [Northstar operations](projects/northstar-operations/) | Operations Lead, Data Analyst, Evidence Analyst, Technical Writer | Six-week CSV, data dictionary, computed metrics, SVG chart, HTML dashboard, operating review |
| [Learning workshop](projects/learning-path-workshop/) | Knowledge Architect, Research Lead, Experience Designer, Technical Writer | 90-minute agenda, assessment rubric, concept map |

## Inspect or reuse the materials

Read [roles.json](roles.json) for the complete role definitions: mission, deliverable contract, working protocol, evidence standards and reusable learning. [teams.json](teams.json) defines the four member combinations and leads. You can copy these definitions into your own teammates and teams; the JSON files are demonstration data, not a supported product import format.

`build.py` uses only the Python standard library to regenerate the supplied project files and definitions. It overwrites generated files under this directory. Runtime records stay outside the repository.

```powershell
uv run --locked python examples/showcase/build.py
```

## Start the read-only preview

Use the project's existing locked dependencies. No Agent CLI, login, model API, PostgreSQL or new package is needed. Choose a **new, nonexistent** runtime directory outside the repository for each run. The service refuses to reuse an existing directory.

```powershell
uv run --locked python examples/showcase/serve.py --runtime C:/workspace/agentsteam-showcase-demo --port 8876
```

Open <http://127.0.0.1:8876/>. The service creates a fresh SQLite database, isolated user configuration directories, personal skills, identity snapshots, four team projects and seventeen completed synthetic conversations. It uses the real application storage and frontend. HTTP writes are rejected, the Agent client is disabled, and memory remains unconfigured. Buttons that require writes or runtime detection are deliberately unavailable in this preview.

Direct artifact URLs:

- <http://127.0.0.1:8876/artifacts/atlas-product-launch/index.html>
- <http://127.0.0.1:8876/artifacts/northstar-operations/dashboard.html>
- <http://127.0.0.1:8876/artifacts/learning-path-workshop/concept-map.svg>

Stop with `Ctrl+C`. The generated runtime directory contains only demo data and can be removed after stopping the service. Do not copy its database, browser profile or identity snapshots into the repository.

## Capture the actual browser pages

`capture.cjs` uses Node's built-in WebSocket and Chromium's DevTools protocol. It requires Node 24+ and an **already installed Chromium browser**; it never downloads a browser or package. Leave the preview running, then supply your own Chromium executable path:

```powershell
node examples/showcase/capture.cjs C:/workspace/agentsteam-showcase-demo http://127.0.0.1:8876 "C:/browser/chrome.exe"
```

Screenshots overwrite the showcase PNGs in `docs/images/`. The capture opens existing conversations, previews files and selects saved profiles without sending messages or saving settings. It records browser exceptions, write requests and visible untranslated text in `browser-verification.json` in the runtime directory, and fails if any are found. `agent-start-attempt.txt` is created only if something attempts to instantiate the blocked Agent client.

All screenshots are actual browser captures, not generated images. The synthetic event fixtures demonstrate rendering and file preview; they do not validate real tool execution, delegation, model quality or task acceptance.
