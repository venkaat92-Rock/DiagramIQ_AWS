# DiagramIQ · AWS

**DiagramIQ's AI Convert, rebuilt for the AWS cloud.** Upload an image of a
process diagram → a vision model on **Amazon Bedrock** (Claude Opus 4.8) reads
its full structure (swimlanes, labelled tasks, gateways, flows) → review the
rendered preview → approve → download Signavio-ready **BPMN 2.0 XML**.

This is the cloud twin of the DiagramIQ desktop app
([Diagram.IQ](https://github.com/venkaat92-Rock/Diagram.IQ)), built for an
AWS environment with **Amplify + Bedrock + IAM** access.

---

## Architecture

```
Browser (Amplify Hosting — static frontend)
  │  1. downscale image client-side, base64
  ▼
API Gateway HTTP API  /convert  /feedback     (created by Amplify backend, CDK)
  │
  ▼
Lambda "diagramiq-bedrock-proxy"              (created by Amplify backend)
  │  IAM role: bedrock:InvokeModel  ← NO API keys anywhere
  ▼
Amazon Bedrock — Converse API (vision)        us.anthropic.claude-opus-4-5-20251101-v1:0
  │  returns structured process JSON {lanes, nodes(col,row), flows}
  ▼
Browser builds the BPMN 2.0 XML + SVG preview (bpmnBuilder.js / svgPreview.js)
  → user reviews → feedback Re-do loop → Approve → .bpmn download
```

There are two other inputs behind the same gateway:

```
POST /analyze /sop /notes /excel-to-bpmn /visio … (21 routes in all)
  → Lambda "diagramiq-python-analyzer"       the desktop app's BPMN engine, ported
POST /celonis/status  /celonis/discover
  → Lambda "diagramiq-celonis"               a Celonis tenant, read over MCP,
                                             turned into reviewable process steps
```

Design choices:
- **No secrets in the browser or repo.** The Lambda's IAM role authorises
  Bedrock. Nothing to rotate, nothing to leak.
- **BPMN building runs client-side** (a JS port of the desktop emitter), so the
  Lambda is a thin, auditable AI proxy and the preview is instant.
- Layout follows the approved DiagramIQ framework: collaboration+participant
  pool, content-fit swimlane bands, orthogonal routing (loop-backs under the
  pool), gateway labels below the diamond.

## Repo layout

```
amplify/backend.ts                        Amplify Gen 2 backend (CDK: HTTP API + IAM)
amplify/functions/bedrock-proxy/          Lambda: /convert (vision) + /feedback
amplify/functions/celonis/                Lambda: /celonis/* (process mining over MCP)
frontend/index.html · app.js              UI (upload → AI Convert → review → approve)
frontend/bpmnBuilder.js                   structured model → BPMN 2.0 XML + layout
frontend/svgPreview.js                    layout → SVG preview
frontend/celonis.js                       the Celonis panel and its evidence block
amplify.yml                               Amplify build pipeline (backend + static frontend)
```
## Deploy (one time, ~15 minutes)

### 0. Prerequisites
- This repo pushed to your GitHub (`DiagramIQ_AWS`).
- AWS account access with **Amplify**, **Bedrock**, and **IAM** permissions.

### 1. Activate the Bedrock model (the "API activation")
1. AWS Console → **Amazon Bedrock** → **Model access** (bottom-left).
2. Enable **Anthropic → Claude Opus 4.8** (and optionally Claude 3.5 Sonnet as
   a fallback) in your region (e.g. `us-east-1`).
3. Wait until status shows **Access granted**.

> The app calls models through the **Converse API**. The UI has a dropdown of
> verified vision models (tested in a Genpact AWS account, us-west-2) plus a
> "Custom model ID…" option for any other inference profile:
>
> | Dropdown entry | Inference profile ID | Notes |
> |---|---|---|
> | Claude Opus 4.5 (Anthropic) — (priority 1) | `us.anthropic.claude-opus-4-5-20251101-v1:0` | **default** — best quality in testing ("fantastic") |
> | Claude Haiku 4.5 (Anthropic) — (priority 2) | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | fast, close to expectation |
> | Nova 2 Lite (Amazon) — (priority 3) | `us.amazon.nova-2-lite-v1:0` | fast, auto-enabled |
> | Claude Sonnet 4.5 (Anthropic) | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | strong middle tier |
> | Pixtral Large (Mistral) | `us.mistral.pixtral-large-2502-v1:0` | strong on diagrams |
> | Llama 4 Maverick (Meta) | `us.meta.llama4-maverick-17b-instruct-v1:0` | strong multimodal |
> | Llama 4 Scout (Meta) | `us.meta.llama4-scout-17b-instruct-v1:0` | lighter Llama 4 |
> | Nova Pro (Amazon) | `us.amazon.nova-pro-v1:0` | baseline |
> | Custom… | any inference profile ID | dated `us.…-vN:0` profile form works best |
>
> Change the backend default in `amplify/functions/bedrock-proxy/resource.ts`.

### 2. Deploy with Amplify
1. AWS Console → **AWS Amplify** → **Create new app** → **GitHub** → authorise
   → pick **`DiagramIQ_AWS`**, branch **`main`**.
2. Amplify auto-detects `amplify.yml` (backend + frontend). Accept defaults —
   Amplify creates its service role; the pipeline runs
   `npx ampx pipeline-deploy`, which provisions the Lambda, the HTTP API, and
   the IAM policy, then publishes the static frontend.
3. First build takes ~5–8 min. When it's green, open the Amplify domain
   (`https://main.xxxxxxxx.amplifyapp.com`).

That's it — the frontend reads the generated `amplify_outputs.json` to find the
API endpoint automatically. No manual URL wiring.

### 3. Use it
1. Open the Amplify URL → **⬆ Image** (or drag & drop) → **✨ AI Convert**.
2. Review the preview + "Understood: N lanes · N tasks…" line.
3. Wrong somewhere? Type feedback → **⟲ Re-do** (the AI revises the model).
4. **✓ Approve & Download .bpmn** → import in Signavio via
   *Import / Export → Import BPMN 2.0 XML*.

---

## Connecting Celonis (process mining → BPMN)

Every other input to DiagramIQ is somebody's account of a process: a diagram
they drew, an SOP they wrote, a transcript of them describing it. Process
mining is the one input that is evidence — the event log says which activities
actually ran, in what order, how often, and who performed them.

The ⛏ **Celonis (mining)** button reads that from a Celonis tenant and turns it
into the same reviewable step list every other input produces, so it lands in
the usual review grid, gets approved by a person, and is drawn by the usual
builder.

### How it talks to Celonis

There are two doors into a tenant, and which one a customer has is not our
choice. DiagramIQ reads through either, behind one interface, so the prompt and
the review grid never know which it got:

| Door | What it is | When it applies |
|---|---|---|
| **MCP** | The **Agent Tools** asset published from Celonis Studio | Someone has published one, with read tools on it |
| **KM** | The **Knowledge Model REST API** | Always, if the tenant has a Knowledge Model — no Studio work needed |

The second one matters more than it sounds. A tenant has a Knowledge Model long
before anybody publishes an Agent Tools asset from it, and publishing one needs
a person with Studio permissions. Without the KM door, those tenants would be
told "not connected" while their process knowledge sat right there.

```
Browser  →  Lambda "diagramiq-celonis"
              │  1. OAuth2 client_credentials  →  {team}/oauth2/token
              │     (the two doors need different scopes; it mints the right one)
              │  2a. MCP: initialize + list_tools  →  what this tenant published
              │  2b. KM:  the same four tools, over the REST API
              │  3. Bedrock Converse, with those tools, until it has the process
              ▼
            discovery definition  →  review grid  →  Approve  →  BPMN
```

Over the KM door the four tools are the ones a published Agent Tools asset
offers by default, so the model speaks one vocabulary either way:

| Tool | Over the REST API |
|---|---|
| `get_knowledge_model()` | `GET /knowledge-models/{km}/kpis` + `/records` + each record's attributes |
| `search_metadata(query)` | matches that model's KPI ids, names and descriptions |
| `load_data(columns, …)` | `GET /knowledge-models/{km}/data?kpis=…&fields=…` — a column with a dot is `RECORD.field`, one without is a KPI id |
| `search_data(query, attribute)` | distinct values of one attribute, filtered |

Celonis publishes limits on that API — 50 rows a page, 5,000 in all, 20
requests a second. DiagramIQ clamps the page size and paces its calls rather
than discovering the limits by being refused, and a request beyond the total
ceiling comes back with the reason instead of a bare 400.

**It never falls back silently.** If the MCP asset is configured and fails to
open, that is an error, not a cue to read the REST API instead — the two see
different things (an asset is a curated tool set; the API is everything), so
swapping them would change the answer and hide the reason the asset failed,
usually an unpublished package or a missing "use" permission.

Two things about this are deliberate:

- **The tool names are discovered, never assumed.** What an Agent Tools asset
  publishes is the customer's choice, so the function asks the server what it
  has and hands the list to the model. A hard-coded tool name would work in
  exactly one tenant.
- **It is read-only, twice over.** Any tool whose name suggests it changes
  something (`trigger`, `action`, `execute`, `write`, `create`, …) is withheld
  from the model, and refused again if the model asks for it by name. The panel
  shows you which tools were withheld. DiagramIQ reads a process; it has no
  business starting an automation in your tenant.

### What to set, and where

In Celonis you need an **application key** (or an OAuth client), and then
whichever of the two doors your tenant has.

Everything goes in Parameter Store — not in the stack, and not in an Amplify
secret. The stack reads them at runtime, so a deployment of DiagramIQ never
depends on a Celonis credential existing, and rotating one needs no redeploy.

**Always:**

```bash
REGION=us-west-2
P="aws ssm put-parameter --region $REGION --overwrite"
$P --name /diagramiq/celonis/team_url --type String \
   --value "https://<team>.<realm>.celonis.cloud"
$P --name /diagramiq/celonis/client_id --type String \
   --value "<application key id>"
$P --name /diagramiq/celonis/client_secret --type SecureString \
   --value "<application key secret>"
```

**For the MCP door** — the OAuth client needs the `mcp-asset.tools:execute`
scope *and* "use" permission on the asset's package, and the package has to be
published:

```bash
$P --name /diagramiq/celonis/mcp_url --type String \
   --value "<the MCP Server URL Studio shows for the published Agent Tools asset>"
```

**For the Knowledge Model door** — the OAuth client needs the
`intelligence.knowledge-models:read` scope:

```bash
$P --name /diagramiq/celonis/km_id --type String \
   --value "<workspace>.<knowledge model key>-km"
```

Set both and it uses the MCP asset; set either one on its own and it uses that.

Optional:

```bash
# mcp | km | auto. Default auto: it takes the Agent Tools asset where one is
# configured, because an asset is curated by someone who knows the tenant.
$P --name /diagramiq/celonis/source --type String --value "auto"
# Only if the Knowledge Model API is not at {team_url}/intelligence/api.
$P --name /diagramiq/celonis/km_base_url --type String \
   --value "https://<team>.<realm>.celonis.cloud/intelligence/api"
# Only if your tenant uses different scopes.
$P --name /diagramiq/celonis/scope --type String \
   --value "mcp-asset.tools:execute"
$P --name /diagramiq/celonis/km_scope --type String \
   --value "intelligence.knowledge-models:read"
# Widen or narrow the read-only guard. Comma-separated; matched as substrings
# of the tool name, case-insensitively.
$P --name /diagramiq/celonis/blocked_tool_words --type String \
   --value "trigger,action,orchestration,resume,execute,write,create,update,delete,publish"
```

Until those exist, the panel says so and names the parameters it is missing —
the feature is dormant, not broken, and nothing else in the app is affected.

The function's IAM role can read **only** `/diagramiq/celonis/*`. That matters
more here than elsewhere: this is the one function that feeds data from another
company's system to a model, and a role that could read every parameter in the
account would be a credential-harvesting tool waiting for a prompt injection.

### Using it

1. ⛏ **Celonis (mining)** → **Check connection**. This does the handshake and
   nothing else: it says which door it read through, lists the read-only tools
   available and names any it withheld. If the credentials are wrong, you find
   out here rather than in the middle of a mining run.
2. Name the process (and optionally say what to look for — "the main path
   only", "show me where invoices get reworked").
3. **Read & draw.** The model explores the tenant and comes back with steps.
   Expect this to take a minute: it is a dozen round trips, not one call.
4. The review grid opens with an **evidence block** above it: which tools were
   read, which calls failed, what the model decided for itself (which variants
   it dropped, which of several data models it picked), and the case count
   behind each activity. Read that before approving — a mined process is a
   claim about your business, and the counts are what make it checkable.
5. Approve. From there it is the normal path: BPMN, validation, uplift, export.

### What it cannot do yet

Nothing here has been run against a live Celonis tenant — there were no
credentials available when it was built. Both doors, the OAuth flow, the MCP
handshake, the four-tool contract, the tool-use loop, the API's limits, the
read-only guard and the whole browser path are exercised end-to-end against a
faithful stub (`npm run test:celonis`, 60 checks), but the **tool names and
result shapes of a real tenant have not been confirmed**. That is also why
nothing in the code depends on them: the first run against a real tenant
discovers whatever it publishes. If it cannot read something, the panel and the
evidence block say which tool failed and why.

---

## Local development

```bash
npm ci
npx ampx sandbox        # deploys a personal cloud sandbox, writes amplify_outputs.json
cp amplify_outputs.json frontend/
cd frontend && python -m http.server 5173   # or any static server
```
Requires AWS credentials locally (e.g. `aws configure` / SSO) with Bedrock +
CloudFormation permissions.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `AccessDeniedException … bedrock:InvokeModel` | Model not enabled — Bedrock → Model access → enable Claude Opus 4.8 in the app's region. |
| `The provided model identifier is invalid` | Use the inference-profile id (`us.anthropic.claude-opus-4-8-v1:0`), or switch the model field to `us.anthropic.claude-3-5-sonnet-20241022-v2:0`. |
| Frontend says "Backend not connected" | The backend build step didn't run — check the Amplify build logs (backend phase) and that the app was created as a Gen 2 app from this repo. |
| CORS error in browser console | The HTTP API allows `*`; if your org restricts it, set your Amplify domain in `amplify/backend.ts` → `corsPreflight.allowOrigins`. |

## Roadmap (parity with desktop DiagramIQ)
- Visio `.vsdx` import (port of the bullet-proof desktop parser)
- PDF input (render first page client-side via pdf.js)
- Signavio validation rules + AI Uplift
- Bedrock **Agents** option for the text pipelines (InvokeAgent)

## Accounts, sign-in and the activity log

People are let in by a super admin; there is no self sign-up. Cognito emails
each new address an invitation carrying a temporary password, and the first
sign-in forces the person to choose their own.

### Turning it on (two steps, and the order matters)

1. **Deploy.** The user pool, the audit table and the admin console ship with
   `REQUIRE_AUTH=false`, so the tool keeps working for everyone while you set
   up. Nothing is enforced yet — this is deliberate: a mail-delivery problem
   must not be able to lock the administrator out of a running tool.

2. **Create the first super admin.** In the AWS console, Cognito → the
   `amplify...` user pool → *Create user*: email address, *Send an email
   invitation*. Then Groups → `superadmin` → add that user. Sign in to
   DiagramIQ with the temporary password, set a real one, and the **⚙ Admin**
   button appears in the header. Everyone after that can be invited from there.

3. **Enforce it.** Set the Amplify environment variable
   `DIAGRAMIQ_REQUIRE_AUTH=true` (Amplify console → Hosting → Environment
   variables) and redeploy. From then on every API call needs a valid token,
   and the sign-in gate covers the tool.

### Email delivery

Cognito's built-in mailer sends the invitations and is capped at **50 messages
a day**, from `no-reply@verificationemail.com` — fine for a pilot, and it does
land in spam sometimes. For real use, point the pool at SES with a verified
sender (Cognito → Messaging → Email provider). SES starts in a sandbox where
you may only mail verified addresses, so request production access before
inviting anyone outside your own domain.

### What is logged

One row per request in the `diagramiq-audit-log` DynamoDB table: who, which
route, when, how long it took, the outcome, and a shallow summary of the input
— filename, process name, the size of what was uploaded. **Never the document
itself**: a customer's process material should not acquire a second copy for
the sake of a log. Admin actions (invite, disable, role change) are logged the
same way. Rows expire after 400 days via TTL, and the table survives a stack
teardown on purpose.

Read it in the app: **⚙ Admin → Activity log**.

## Security notes
- Sign-in is Cognito; see above. Until `DIAGRAMIQ_REQUIRE_AUTH=true` the API is
  **unauthenticated**, which is the shipping default so that deploying the
  login layer cannot lock you out before you have an account.
- The Lambda role's Bedrock policy is `resources: ['*']`; tighten it to the
  specific model/inference-profile ARNs if your org requires least privilege.
- The Celonis function is the one that handles another company's data, so three
  things are deliberate about it: its Celonis credentials live in SSM (read at
  runtime, never in the stack or the function's environment block), its role
  can read only `/diagramiq/celonis/*`, and the tools it offers the model are
  filtered to read-only ones and refused again at call time. Its audit rows
  record who asked and for which process, never what was mined.
- All three endpoints (`/convert`, the engine, `/celonis/*`) are published as
  Function URLs with `authType: NONE` because the HTTP API's 29-second ceiling
  is fatal to the long AI passes. The gate is the handler's own token check, so
  `DIAGRAMIQ_REQUIRE_AUTH=true` is what closes the door — on all three.
