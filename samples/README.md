# Sample documents

## `SOP-PR-014 Purchase Requisition to Purchase Order.docx`

A synthetic standard operating procedure for testing **⬆ Notes / Document**. The
process and the company are invented; nothing here comes from a real client.

It is built to exercise every part of the document reader at once:

| What it contains | What it tests |
|---|---|
| 8 numbered headings | section structure survives extraction |
| A document-control table, a definitions table and a revision history | reference sections that must **not** become process steps |
| A roles and responsibilities table | joining a participant to a step named elsewhere |
| A 6-column step table (Step / Activity / Responsible / System / Input / Output) | the procedure in tabular form, one row per step |
| Bulleted out-of-scope items | list markers |
| Clauses 5.1–5.9 with thresholds (£5,000, £25,000, 2 and 5 business days) | decision criteria kept verbatim, numbers not rounded |
| An embedded flowchart, Figure 1 | the figure reaching the vision pass with its position marked |
| Named systems — Coupa, SAP S/4HANA, ServiceNow | systems landing in `it_systems`, not in the activity name |

### What a good result looks like

Upload it under **⬆ Notes / Document**. The status line should report
`Read 5 tables, 1 figure, 8 sections.` and the review grid should open with the
procedure steps — *not* with "Review revision history" or "Define PR" as
activities, which is what happens when reference sections leak into the
extraction.

Worth checking in the grid:

- **Raise purchase requisition** sits against *Requester* and *Coupa*.
- **Approve requisition** sits against *Finance Manager*, and its dependency
  carries the £5,000 threshold rather than a paraphrase of it.
- **Vet supplier** appears, since it is a branch that only the clauses and the
  figure describe — the step table lists it, but the condition that triggers it
  is only in clause 5.7 and in Figure 1.

### Making a PDF version

Open it in Word and save as PDF. The text and tables come through; the figure
does not, because pulling raster images out of a PDF needs an image library that
cannot be vendored into the Lambda. A PDF whose flowchart is vector still works
— those labels are text, and pypdf reads them.
