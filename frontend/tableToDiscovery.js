/* A procedure table → a discovery definition, in the browser.
 *
 * The same mapping the engine does in diagramiq/table_to_process.py, kept in
 * step with it deliberately: the point of this path is that it needs no
 * backend, so it cannot call the backend to do the mapping.
 *
 * A table qualifies only if it has an activity column AND at least one other
 * useful column. That threshold is what stops a definitions, roles,
 * document-control or revision-history table being read as the procedure.
 */

const COLUMNS = [
  ['activity', ['activity', 'task', 'action', 'step description', 'process step',
                'step name', 'what', 'description of step']],
  ['description', ['description', 'detail', 'details', 'how', 'notes', 'guidance',
                   'method', 'work instruction']],
  ['participant', ['responsible', 'role', 'owner', 'actor', 'participant', 'who',
                   'accountable', 'performed by', 'responsibility', 'team',
                   'department', 'function']],
  ['it_systems', ['it system', 'it systems', 'system', 'systems', 'application',
                  'applications', 'tool', 'tools', 'platform', 'software']],
  ['input_document', ['input document', 'input documents', 'input', 'inputs',
                      'source document', 'received']],
  ['output_document', ['output document', 'output documents', 'output', 'outputs',
                       'deliverable', 'produced', 'record']],
  ['templates', ['template', 'templates', 'form', 'forms']],
  ['dependency', ['dependency', 'dependencies', 'condition', 'conditions',
                  'criteria', 'trigger', 'prerequisite', 'when']],
  ['frequency', ['frequency', 'how often', 'volume', 'sla', 'timing', 'timeline']],
  ['pain_points', ['pain point', 'pain points', 'issue', 'issues', 'risk', 'risks',
                   'problem', 'problems']],
];

const STEP_NO = ['step', 'step no', 'step #', '#', 'no', 'no.', 'ref', 'seq',
                 'sequence', 'item'];

const MIN_STEPS = 2;
const FIELDS = ['activity', 'description', 'participant', 'it_systems',
                'input_document', 'output_document', 'templates', 'dependency',
                'frequency', 'pain_points'];

const norm = (cell) =>
  String(cell ?? '').toLowerCase().replace(/[^a-z0-9 ]+/g, ' ').trim();

function matchHeader(cell) {
  const text = norm(cell);
  if (!text) return null;
  if (STEP_NO.includes(text)) return '_step_no';
  let best = null;
  for (const [field, names] of COLUMNS) {
    for (const name of names) {
      // Exact wins outright; otherwise the longest containment, so "input
      // document" is not swallowed by "input".
      if (text === name) return field;
      if (text.includes(name) && (!best || name.length > best[1].length)) {
        best = [field, name];
      }
    }
  }
  return best ? best[0] : null;
}

export function scoreTable(rows) {
  if (!rows || rows.length < MIN_STEPS + 1) return { score: 0, mapping: {} };
  const mapping = {};
  const taken = new Set();
  rows[0].forEach((cell, i) => {
    const field = matchHeader(cell);
    if (field && !taken.has(field)) { mapping[i] = field; taken.add(field); }
  });
  if (!taken.has('activity')) return { score: 0, mapping: {} };
  const useful = [...taken].filter((f) => f !== 'activity' && f !== '_step_no');
  if (!useful.length) return { score: 0, mapping: {} };

  const activityCols = Object.entries(mapping)
    .filter(([, f]) => f === 'activity').map(([i]) => Number(i));
  const filled = rows.slice(1)
    .filter((r) => activityCols.some((i) => (r[i] || '').trim())).length;
  if (filled < MIN_STEPS) return { score: 0, mapping: {} };

  return { score: useful.length * 10 + filled, mapping };
}

/** The best procedure table in a document as a discovery object, or null. */
export function discoveryFromTables(doc, defaultName = '') {
  let best = null;
  let bestScore = 0;
  for (const rows of doc.tables || []) {
    const { score, mapping } = scoreTable(rows);
    if (score > bestScore) { bestScore = score; best = { rows, mapping }; }
  }
  if (!best) return null;

  const steps = [];
  for (const row of best.rows.slice(1)) {
    const step = Object.fromEntries(FIELDS.map((f) => [f, '']));
    for (const [i, field] of Object.entries(best.mapping)) {
      if (field !== '_step_no') step[field] = (row[Number(i)] || '').trim();
    }
    if (!step.activity) continue;
    // A "not part of this procedure" row is a note about scope, not a step.
    if (/\bnot part of this (procedure|process)\b/i.test(row.join(' '))) continue;
    steps.push(step);
  }
  if (steps.length < MIN_STEPS) return null;

  return {
    process_name: defaultName || doc.title || 'Discovered Process',
    trigger_event: '',
    successful_outcome: '',
    unsuccessful_outcome: '',
    steps,
  };
}

/** Discovery steps → the preview model bpmnBuilder draws.
 *
 * Deliberately linear: start, one task per step in order, end, with a lane per
 * distinct participant. Conditions stay in the step's dependency field rather
 * than being turned into gateways — inferring branching from a table is a
 * guess, and the AI pass does it properly from the clause text. A straight
 * chain that is honest beats a branch that is invented.
 */
export function discoveryToModel(discovery) {
  const steps = (discovery.steps || []).filter((s) => (s.activity || '').trim());
  if (!steps.length) return null;

  const lanes = [];
  for (const s of steps) {
    const lane = (s.participant || '').trim() || 'Process';
    if (!lanes.includes(lane)) lanes.push(lane);
  }

  const nodes = [{ id: 'start', type: 'start', name: discovery.trigger_event || 'Start',
                   lane: lanes[0], col: 0, row: 0 }];
  steps.forEach((s, i) => {
    const name = (s.it_systems || '').trim()
      ? `${s.activity} (${s.it_systems})` : s.activity;
    nodes.push({ id: `t${i + 1}`, type: 'task', name,
                 lane: (s.participant || '').trim() || 'Process', col: i + 1, row: 0 });
  });
  nodes.push({ id: 'end', type: 'end', name: discovery.successful_outcome || 'End',
               lane: lanes[lanes.length - 1], col: steps.length + 1, row: 0 });

  const flows = [];
  for (let i = 0; i < nodes.length - 1; i++) {
    // A condition on a step labels the flow into it, so the criterion is
    // visible on the diagram even though it is not modelled as a gateway.
    const target = nodes[i + 1];
    const step = target.id.startsWith('t') ? steps[Number(target.id.slice(1)) - 1] : null;
    flows.push({ from: nodes[i].id, to: target.id,
                 label: (step && step.dependency) ? step.dependency : '' });
  }

  return {
    process_name: discovery.process_name || 'Discovered Process',
    lanes, nodes, flows,
  };
}
