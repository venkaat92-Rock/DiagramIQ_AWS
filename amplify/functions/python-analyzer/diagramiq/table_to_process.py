"""A procedure table → a Process Discovery definition, with no model call.

Most SOPs state their procedure twice: once in prose clauses, and once in a
step table with columns like Step / Activity / Responsible / System / Input /
Output. That table is already the discovery schema — the mapping is column to
field, and reading it needs no intelligence at all.

That matters for more than cost. When Bedrock is throttled, unavailable, or
the account is out of Lambda concurrency, the AI pass fails and the user is
left with nothing. A deterministic reader turns that dead end into a usable
result, clearly labelled as the lesser one: it recovers the steps, the roles
and the systems, but not the thresholds and conditions that live in the clause
text, and not the branching a figure shows.

So this is the fallback, never the first choice — and the caller says which
one produced the result, because a reviewer approving a diagram is entitled to
know whether the conditions were read or simply absent.
"""
from __future__ import annotations

import re

# Header synonyms. Matched against a normalised header cell, longest first, so
# "input document" wins over "input" and "it systems" over "systems".
COLUMNS = [
    ('activity',        ['activity', 'task', 'action', 'step description', 'process step',
                         'step name', 'what', 'description of step']),
    ('description',     ['description', 'detail', 'details', 'how', 'notes', 'guidance',
                         'method', 'work instruction']),
    ('participant',     ['responsible', 'role', 'owner', 'actor', 'participant', 'who',
                         'accountable', 'performed by', 'responsibility', 'team',
                         'department', 'function']),
    ('it_systems',      ['it system', 'it systems', 'system', 'systems', 'application',
                         'applications', 'tool', 'tools', 'platform', 'software']),
    ('input_document',  ['input document', 'input documents', 'input', 'inputs',
                         'source document', 'received']),
    ('output_document', ['output document', 'output documents', 'output', 'outputs',
                         'deliverable', 'produced', 'record']),
    ('templates',       ['template', 'templates', 'form', 'forms']),
    ('dependency',      ['dependency', 'dependencies', 'condition', 'conditions',
                         'criteria', 'trigger', 'prerequisite', 'when']),
    ('frequency',       ['frequency', 'how often', 'volume', 'sla', 'timing', 'timeline']),
    ('pain_points',     ['pain point', 'pain points', 'issue', 'issues', 'risk', 'risks',
                         'problem', 'problems']),
]

# A column of 1, 2, 3… or 5.1, 5.2… — the row's ordinal, not a field.
STEP_NO = ['step', 'step no', 'step #', '#', 'no', 'no.', 'ref', 'seq', 'sequence', 'item']

MIN_STEPS = 2


def _norm(cell: str) -> str:
    return re.sub(r'[^a-z0-9 ]+', ' ', str(cell or '').lower()).strip()


def _match_header(cell: str):
    """Which discovery field this header names, or None."""
    text = _norm(cell)
    if not text:
        return None
    if text in STEP_NO:
        return '_step_no'
    best = None
    for field, names in COLUMNS:
        for name in names:
            # Exact first, then containment — "Responsible party" should still
            # match "responsible", but "Input" must not swallow "Input document".
            if text == name:
                return field
            if name in text and (best is None or len(name) > len(best[1])):
                best = (field, name)
    return best[0] if best else None


def score_table(rows: list) -> tuple:
    """(score, mapping) for a table's suitability as a procedure table.

    A procedure table needs an activity column and at least one other field
    worth having. Anything else — definitions, revision history, document
    control — scores zero and is left alone.
    """
    if len(rows) < MIN_STEPS + 1:            # header plus MIN_STEPS rows
        return 0, {}
    mapping = {}
    for i, cell in enumerate(rows[0]):
        field = _match_header(cell)
        if field and field not in mapping.values():
            mapping[i] = field
    fields = set(mapping.values())
    if 'activity' not in fields:
        return 0, {}
    useful = fields - {'activity', '_step_no'}
    if not useful:
        return 0, {}

    body = rows[1:]
    filled = sum(1 for r in body
                 if any(r[i].strip() for i, f in mapping.items() if f == 'activity'))
    if filled < MIN_STEPS:
        return 0, {}
    # More mapped columns and more filled rows is a better candidate; a table
    # where half the activity cells are blank is probably not the procedure.
    return len(useful) * 10 + filled, mapping


def discovery_from_tables(doc, default_name: str = '') -> dict | None:
    """Best procedure table in the document as a discovery dict, or None."""
    best_score, best = 0, None
    for rows in getattr(doc, 'tables', []) or []:
        score, mapping = score_table(rows)
        if score > best_score:
            best_score, best = score, (rows, mapping)
    if not best:
        return None

    rows, mapping = best
    steps = []
    for row in rows[1:]:
        step = {'activity': '', 'description': '', 'participant': '', 'it_systems': '',
                'input_document': '', 'output_document': '', 'templates': '',
                'dependency': '', 'frequency': '', 'pain_points': ''}
        for i, field in mapping.items():
            if field == '_step_no':
                continue
            step[field] = row[i].strip()
        if not step['activity']:
            continue
        # A trailing "not part of this procedure" row is a note about scope,
        # not a step; it appears in role tables more than step tables, but the
        # cost of missing it is a bogus activity in the diagram.
        if re.search(r'\bnot part of this (procedure|process)\b', ' '.join(row), re.I):
            continue
        steps.append(step)

    if len(steps) < MIN_STEPS:
        return None

    return {
        'process_name': default_name or getattr(doc, 'title', '') or 'Discovered Process',
        'trigger_event': '',
        'successful_outcome': '',
        'unsuccessful_outcome': '',
        'steps': steps,
    }
