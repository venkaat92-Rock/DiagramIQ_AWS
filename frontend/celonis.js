/* Celonis → BPMN, in the browser.
 *
 * Process mining answers a question the other inputs cannot: not what someone
 * drew, or wrote down, but what the event log says actually happened — which
 * activities ran, in what order, how often, and who did them. This panel asks
 * the backend to read that from a Celonis tenant and hands the result to the
 * same review grid every other input feeds, because a mined process is a
 * claim about someone's business and a person signs it off before it is drawn.
 *
 * Everything that talks to Celonis lives in the Lambda. This file has no
 * credentials, no tenant URL and no tool names in it — by the time the browser
 * is involved, the answer is already a discovery definition.
 *
 * The dependencies come in from app.js rather than being imported: post(),
 * openReview() and the status line are app state, and threading them through
 * keeps this file testable on its own.
 */

const $ = (id) => document.getElementById(id);

/** One line per tool the tenant published, so a reviewer can see what the
    model was given — and an administrator can see what it was not. */
function renderTools(data) {
  const box = $('celonisTools');
  const tools = data.tools || [];
  const blocked = data.blocked || [];
  const rows = tools.map((t) => {
    const name = typeof t === 'string' ? t : t.name;
    const desc = typeof t === 'string' ? '' : (t.description || '');
    return `<li><code>${esc(name)}</code>${desc ? ` — ${esc(desc.slice(0, 160))}` : ''}</li>`;
  });
  box.innerHTML = [
    tools.length
      ? `<p class="muted">${tools.length} read-only tool(s) published by this tenant:</p>
         <ul class="celonis-tools">${rows.join('')}</ul>`
      : '<p class="muted">This tenant published no read-only tools.</p>',
    blocked.length
      ? `<p class="muted">Withheld as not read-only: ${blocked.map(esc).join(', ')}.
          DiagramIQ reads a process; it never triggers anything in your tenant.</p>`
      : '',
  ].join('');
  box.hidden = false;
}

const esc = (s) => String(s ?? '').replace(/[<>&"]/g,
  (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c]));

/** How often each mined activity actually ran.

    The review grid shows five columns — activity, description, participant,
    systems, dependency — and that is the same grid every other input feeds, so
    widening it for one input would change the sheet for all of them. But the
    counts are the whole reason to mine a process rather than ask somebody to
    draw it: approving a step that ran 2,140 times out of 12,480 cases is a
    different decision from approving one that ran every time. They ride along
    with the step either way (and into the exported workbook); this puts them
    where the approval happens.

    Read from the steps rather than from a separate payload: whatever the
    reviewer is about to approve is what is counted here. */
function renderCounts(steps) {
  const rows = steps.filter((s) => s.frequency || s.pain_points);
  if (!rows.length) return '';
  return `<table class="rev-counts">
    <thead><tr><th>Mined activity</th><th>How often</th><th>What the log shows</th></tr></thead>
    <tbody>${rows.map((s) => `<tr><td>${esc(s.activity)}</td>
      <td>${esc(s.frequency)}</td><td>${esc(s.pain_points)}</td></tr>`).join('')}</tbody>
  </table>`;
}

/** What the model did to get its answer. Shown in the review grid, next to the
    Approve button, because "where did these steps come from" is the question a
    reviewer has to be able to answer before signing them off. */
function renderEvidence(data) {
  const notes = data.notes || [];
  const calls = data.toolCalls || [];
  const used = [...new Set(calls.filter((c) => c.ok).map((c) => c.tool))];
  const failed = calls.filter((c) => !c.ok);
  const bits = [];
  if (used.length) {
    bits.push(`<p><strong>Read from Celonis</strong> using ${esc(used.join(', '))}
      — ${calls.filter((c) => c.ok).length} call(s).</p>`);
  }
  if (failed.length) {
    bits.push(`<p>${failed.length} tool call(s) failed and were worked around:
      ${esc(failed.map((c) => `${c.tool} (${c.error || 'no reason given'})`)
              .slice(0, 4).join('; '))}</p>`);
  }
  if (notes.length) {
    bits.push(`<p><strong>Judgement calls the model recorded</strong> — read these
      before you approve:</p><ul>${notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>`);
  }
  bits.push(renderCounts(data.discovery?.steps || []));
  const box = $('revNotes');
  box.innerHTML = bits.join('');
  box.hidden = !bits.length;
}

export function initCelonis({ post, openReview, setBusy, setStatus,
                              modelId, processName }) {
  const dlg = $('celonisModal');
  if (!dlg) return;
  const msg = $('celonisMsg');

  const say = (text, kind = 'info') => {
    msg.textContent = text;
    msg.dataset.kind = kind;
  };

  $('btnCelonis').addEventListener('click', () => {
    $('celonisProcess').value = processName() || '';
    say('');
    $('celonisTools').hidden = true;
    dlg.showModal();
  });
  $('btnCelonisClose').addEventListener('click', () => dlg.close());
  $('btnCelonisCancel').addEventListener('click', () => dlg.close());

  // "Does this even work" — no model, no mining, just the handshake and the
  // tool list. Worth its own button: when a mining run fails, the first thing
  // anyone needs to know is whether the credentials or the data was the problem.
  $('btnCelonisCheck').addEventListener('click', async () => {
    say('Connecting to Celonis…');
    try {
      const data = await post('/celonis/status', {});
      say(`Connected to ${data.team || 'the tenant'}.`, 'ok');
      renderTools(data);
    } catch (e) {
      $('celonisTools').hidden = true;
      say(describe(e), 'error');
    }
  });

  $('btnCelonisRun').addEventListener('click', async () => {
    const name = $('celonisProcess').value.trim();
    const question = $('celonisQuestion').value.trim();
    if (!name && !question) {
      say('Name the process, or say what to look for.', 'error');
      return;
    }
    setBusy(true);
    say('Reading the process from Celonis — this explores the tenant, so it can '
        + 'take a minute…');
    setStatus('Mining the process from Celonis…');
    try {
      const data = await post('/celonis/discover', {
        processName: name, question, modelId: modelId(),
      });
      const steps = data.discovery?.steps?.length || 0;
      if (!steps) {
        say('Celonis answered, but nothing in it mapped to process steps. '
            + (data.notes?.[0] ? `The model said: ${data.notes[0]}` : '')
            + ' Try naming the process exactly as it appears in your data model.',
            'error');
        setStatus('Celonis returned no steps.', 'warn');
        return;
      }
      dlg.close();
      openReview(data.discovery, `Review — mined from Celonis${name ? `: ${name}` : ''}`,
                 'build');
      renderEvidence(data);
      setStatus(`${steps} step(s) mined from Celonis. Check them against what you know `
                + 'of the process, then Approve to build the BPMN.', 'info');
    } catch (e) {
      say(describe(e), 'error');
      setStatus(`Celonis read failed: ${e.message}`, 'error');
    } finally {
      setBusy(false);
    }
  });
}

/** A 409 from this route means "nobody has connected Celonis yet", which is a
    setup step, not a fault — and the message carries the parameter names to
    set, so it is passed through rather than reworded. */
function describe(err) {
  const m = err?.message || String(err);
  return /not connected yet/i.test(m)
    ? `${m}\nAn administrator sets these once, with the AWS CLI — see the README.`
    : m;
}
