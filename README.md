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
Amazon Bedrock — Converse API (vision)        us.anthropic.claude-opus-4-8-v1:0
  │  returns structured process JSON {lanes, nodes(col,row), flows}
  ▼
Browser builds the BPMN 2.0 XML + SVG preview (bpmnBuilder.js / svgPreview.js)
  → user reviews → feedback Re-do loop → Approve → .bpmn download
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
frontend/index.html · app.js              UI (upload → AI Convert → review → approve)
frontend/bpmnBuilder.js                   structured model → BPMN 2.0 XML + layout
frontend/svgPreview.js                    layout → SVG preview
amplify.yml                               Amplify build pipeline (backend + static frontend)
```

---

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
> | Claude Haiku 4.5 (Anthropic) — (priority) | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | **default** — closest to expectation in testing |
> | Nova 2 Lite (Amazon) — (priority) | `us.amazon.nova-2-lite-v1:0` | fast, auto-enabled |
> | Pixtral Large (Mistral) | `us.mistral.pixtral-large-2502-v1:0` | strong on diagrams |
> | Llama 4 Maverick (Meta) | `us.meta.llama4-maverick-17b-instruct-v1:0` | strong multimodal |
> | Llama 4 Scout (Meta) | `us.meta.llama4-scout-17b-instruct-v1:0` | lighter Llama 4 |
> | Nova Pro (Amazon) | `us.amazon.nova-pro-v1:0` | baseline |
> | Custom… | e.g. `us.anthropic.claude-opus-4-7` | Opus/Sonnet may need the one-time Marketplace enablement (a user with `aws-marketplace:Subscribe` invokes once, e.g. in the Bedrock playground) |
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

## Security notes
- The HTTP API is **unauthenticated** by default (like a demo). For corporate
  use, put it behind Amplify Auth/Cognito or an API key: add an authorizer in
  `amplify/backend.ts`.
- The Lambda role's Bedrock policy is `resources: ['*']`; tighten it to the
  specific model/inference-profile ARNs if your org requires least privilege.
