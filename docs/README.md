# docs

**DiagramIQ-Overview.pdf** — a five-page explainer for a non-technical audience:
the problem, the solution, what makes it different in the market, and the
commercial case.

`overview-deck.html` is its source; `render-deck.mjs` rebuilds the PDF from it.

## About the numbers

Every effort, cost and margin figure in the deck is computed from four
assumptions, printed on its last page:

| Assumption | Value used |
| --- | --- |
| Manual effort per process map | 6.0 hours |
| Effort with DiagramIQ, including human review | 1.2 hours |
| Processes in a typical engagement | 250 |
| Blended delivery cost per hour | $38 |

They are a model, not measured client outcomes, and the deck says so where a
reader will see it. To move them from illustrative to measured, the deck
proposes a three-week, twenty-process pilot; when that produces real timings,
edit the four values in `overview-deck.html` and re-run the renderer.

The worked example on page 3 is not modelled — those counts come from running
the sample SOP in `samples/` through the engine.
