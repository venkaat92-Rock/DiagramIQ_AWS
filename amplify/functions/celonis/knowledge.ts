/**
 * One interface over the two ways into a Celonis tenant.
 *
 * Whichever door is open, the rest of the function sees the same thing: a list
 * of read-only tools, and a way to call one by name. The model is never told
 * which door it is using, because the answer should not depend on it.
 *
 *   MCP  — the Agent Tools asset. Its tools are whatever the customer chose to
 *          publish, so they are discovered and passed through untouched.
 *   KM   — the Knowledge Model REST API, wrapped as the four tools the Celonis
 *          Agent Tools asset publishes by default:
 *
 *            get_knowledge_model()   the KPIs and records, with ids
 *            search_metadata(query)  find a KPI or attribute by name
 *            load_data(columns, …)   KPI values, broken down by attributes
 *            search_data(query, …)   find values of one attribute
 *
 *          Those four names are not invented here: they are the vocabulary a
 *          published asset already speaks, so a prompt that works against one
 *          door works against the other.
 *
 * Why the second door exists at all: a tenant has a Knowledge Model long
 * before anybody publishes an Agent Tools asset from it, and publishing one is
 * a Studio task that needs a person with the right permissions. Without this
 * path, those tenants are told "not connected" when their process knowledge is
 * sitting right there.
 */
import {
  type CelonisConfig, type Connected, type DiscoveredTool, type SourceKind,
  callTool, connect, getToken, isBlocked,
} from './celonis.js';

export type Source = {
  kind: SourceKind;
  /** What to show a reviewer: which door, and into what. */
  label: string;
  tools: DiscoveredTool[];
  blocked: string[];
  call: (name: string, args: Record<string, unknown>) => Promise<string>;
  close: () => Promise<void>;
};

/* ── the Knowledge Model REST API ───────────────────────────────────────────*/

/** Celonis publishes these limits on the Knowledge Model API: 50 rows a page,
 *  5,000 rows in all, 20 requests a second, 6,000 a day. The first two are
 *  clamped on the way out rather than discovered by being refused; the third
 *  is spaced out below. The daily budget is the tenant's to watch — a single
 *  mining run is a dozen calls, so it is not this function that will spend it,
 *  but a loop that somehow did would be visible in the tool-call list. */
const PAGE_MAX = 50;
const ROWS_MAX = 5000;
const MIN_GAP_MS = 55;          // ~18 requests a second, under the 20 ceiling

const KM_TOOLS: DiscoveredTool[] = [
  {
    name: 'get_knowledge_model',
    description: 'The Knowledge Model: its KPIs and its records, with the id, '
      + 'display name and description of each. Call this first — it is how you '
      + 'learn what this tenant actually holds and what its things are called.',
    schema: { type: 'object', properties: {} },
  },
  {
    name: 'search_metadata',
    description: 'Find KPIs or record attributes whose id, name or description '
      + 'matches a query, e.g. "activity", "vendor", "duration".',
    schema: {
      type: 'object',
      properties: { query: { type: 'string', description: 'Text to look for.' } },
      required: ['query'],
    },
  },
  {
    name: 'load_data',
    description: 'Read data. "columns" mixes KPI ids and record attributes '
      + 'written RECORD.field — e.g. ["ap_invoice_count", "AP_INVOICE.channel"] '
      + 'gives the invoice count broken down by channel. Attributes alone give '
      + 'their distinct combinations. Use order_by with a column name to rank.',
    schema: {
      type: 'object',
      properties: {
        columns: { type: 'array', items: { type: 'string' },
                   description: 'KPI ids and/or RECORD.field attributes.' },
        order_by: { type: 'string', description: 'A column to sort by.' },
        ascending: { type: 'boolean', description: 'Sort ascending (default true).' },
        limit: { type: 'integer', description: `Rows to return, at most ${PAGE_MAX}.` },
      },
      required: ['columns'],
    },
  },
  {
    name: 'search_data',
    description: 'Find values of one attribute that match a query, e.g. a '
      + 'vendor name or an activity name.',
    schema: {
      type: 'object',
      properties: {
        query: { type: 'string', description: 'Text to look for.' },
        attribute: { type: 'string', description: 'RECORD.field to search in.' },
      },
      required: ['query', 'attribute'],
    },
  },
];

type Km = {
  kpis: { id: string; displayName?: string; description?: string }[];
  records: { id: string; displayName?: string;
             attributes?: { id: string; displayName?: string; description?: string }[] }[];
};

/** A thin client for the Knowledge Model API: one token, re-minted once if the
 *  tenant decides it has expired, and never more than ~18 calls a second. */
class KmClient {
  private token = '';
  private lastCall = 0;

  constructor(private cfg: CelonisConfig) {}

  private get base() {
    return `${this.cfg.kmBaseUrl}/knowledge-models/${encodeURIComponent(this.cfg.kmId)}`;
  }

  private async pace() {
    const wait = MIN_GAP_MS - (Date.now() - this.lastCall);
    if (wait > 0) await new Promise((r) => setTimeout(r, wait));
    this.lastCall = Date.now();
  }

  async get(path: string, params: Record<string, string> = {}): Promise<unknown> {
    if (!this.token) this.token = await getToken(this.cfg, this.cfg.kmScope);
    const url = new URL(this.base + path);
    for (const [k, v] of Object.entries(params)) if (v) url.searchParams.set(k, v);

    const send = async () => {
      await this.pace();
      return fetch(url, { headers: { authorization: `Bearer ${this.token}`,
                                     accept: 'application/json' } });
    };

    let resp = await send();
    if (resp.status === 401) {
      // The token expired mid-run. Mint one more and try again exactly once —
      // a second 401 is a permissions problem, not an expiry, and retrying it
      // would just burn the tenant's request budget.
      this.token = await getToken(this.cfg, this.cfg.kmScope);
      resp = await send();
    }
    if (!resp.ok) {
      const body = (await resp.text()).slice(0, 400);
      if (resp.status === 403) {
        throw new Error(`Celonis refused this read (HTTP 403). The OAuth client `
          + `needs the '${this.cfg.kmScope}' scope and permission on knowledge `
          + `model '${this.cfg.kmId}'. ${body}`);
      }
      if (resp.status === 404) {
        throw new Error(`No such object on knowledge model '${this.cfg.kmId}' `
                        + `(HTTP 404): ${url.pathname}${url.search}. ${body}`);
      }
      throw new Error(`Celonis returned HTTP ${resp.status} for `
                      + `${url.pathname}${url.search}: ${body}`);
    }
    return resp.json();
  }

  /** The KPIs and records, read once per request and kept for the run: the
   *  model calls get_knowledge_model and then search_metadata over the same
   *  thing, and paying for it twice helps nobody. */
  private kmCache: Km | null = null;
  async knowledgeModel(): Promise<Km> {
    if (this.kmCache) return this.kmCache;
    const [kpis, records] = await Promise.all([
      this.get('/kpis') as Promise<unknown>,
      this.get('/records') as Promise<unknown>,
    ]);
    this.kmCache = {
      kpis: asList(kpis).map((k) => pick(k, ['id', 'displayName', 'description'])) as Km['kpis'],
      records: asList(records).map((r) => pick(r, ['id', 'displayName'])) as Km['records'],
    };
    return this.kmCache;
  }

  /** One record's attributes. Needed because /records lists the records but
   *  not what is in them, and a column name is RECORD.field. */
  async recordAttributes(recordId: string) {
    const body = await this.get(`/records/${encodeURIComponent(recordId)}`);
    const attrs = asList((body as Record<string, unknown>)?.attributes ?? body);
    return attrs.map((a) => pick(a, ['id', 'displayName', 'description']));
  }

  async data(columns: string[], orderBy: string, ascending: boolean, limit: number) {
    // A column with a dot is RECORD.field; anything else is a KPI id. That is
    // the API's own split, and getting it wrong is a 400 with no explanation.
    const kpis = columns.filter((c) => !c.includes('.'));
    const fields = columns.filter((c) => c.includes('.'));
    return this.get('/data', {
      kpis: kpis.join(','),
      fields: fields.join(','),
      ...(orderBy ? { sort: orderBy, order: ascending ? 'asc' : 'desc' } : {}),
      limit: String(Math.min(Math.max(1, limit || PAGE_MAX), PAGE_MAX)),
    });
  }
}

const asList = (v: unknown): Record<string, unknown>[] => {
  if (Array.isArray(v)) return v as Record<string, unknown>[];
  if (v && typeof v === 'object') {
    // The API wraps collections differently per endpoint; take the first array
    // in the body rather than guessing the key.
    for (const value of Object.values(v as Record<string, unknown>)) {
      if (Array.isArray(value)) return value as Record<string, unknown>[];
    }
  }
  return [];
};

const pick = (o: Record<string, unknown>, keys: string[]) => {
  const out: Record<string, unknown> = {};
  for (const k of keys) if (o?.[k] !== undefined && o[k] !== null) out[k] = o[k];
  // Keep anything that looks like an id even under another name, so a tenant
  // whose API spells it differently is not silently read as empty.
  if (out.id === undefined && o?.name !== undefined) out.id = o.name;
  return out;
};

const text = (v: unknown) => JSON.stringify(v, null, 1).slice(0, 40000);

/** The four tools, over the Knowledge Model API. */
async function openKm(cfg: CelonisConfig): Promise<Source> {
  const km = new KmClient(cfg);
  // Prove the credentials and the knowledge model id before anything else:
  // finding out at the model's third tool call that the id is wrong wastes a
  // minute and reads like the data is missing rather than the config.
  await km.knowledgeModel();

  const blocked = KM_TOOLS.filter((t) => isBlocked(t.name, cfg)).map((t) => t.name);
  const tools = KM_TOOLS.filter((t) => !isBlocked(t.name, cfg));

  const call = async (name: string, args: Record<string, unknown>): Promise<string> => {
    if (isBlocked(name, cfg)) throw new Error(`Refused: '${name}' is not a read-only tool.`);
    if (!tools.some((t) => t.name === name)) {
      throw new Error(`Refused: '${name}' is not one of the published tools.`);
    }

    if (name === 'get_knowledge_model') {
      const model = await km.knowledgeModel();
      // Records without their attributes are half an answer: the model needs
      // RECORD.field to call load_data at all.
      const records = await Promise.all(model.records.slice(0, 12).map(async (r) => {
        try {
          return { ...r, attributes: await km.recordAttributes(String(r.id)) };
        } catch (err) {
          return { ...r, attributes: [],
                   note: `attributes unavailable: ${(err as Error).message.slice(0, 120)}` };
        }
      }));
      return text({ knowledgeModelId: cfg.kmId, kpis: model.kpis, records });
    }

    if (name === 'search_metadata') {
      const q = String(args.query ?? '').toLowerCase().trim();
      if (!q) throw new Error('search_metadata needs a query.');
      const model = await km.knowledgeModel();
      const hit = (o: Record<string, unknown>) =>
        Object.values(o).some((v) => String(v ?? '').toLowerCase().includes(q));
      const kpis = model.kpis.filter((k) => hit(k as Record<string, unknown>));
      const attributes: unknown[] = [];
      for (const r of model.records.slice(0, 12)) {
        try {
          for (const a of await km.recordAttributes(String(r.id))) {
            if (hit(a)) attributes.push({ column: `${r.id}.${a.id}`, ...a });
          }
        } catch { /* a record that will not open is reported by get_knowledge_model */ }
      }
      return text({ query: q, kpis, attributes });
    }

    if (name === 'load_data') {
      const columns = (Array.isArray(args.columns) ? args.columns : [])
        .map((c) => String(c)).filter(Boolean);
      if (!columns.length) throw new Error('load_data needs at least one column.');
      const limit = Number(args.limit ?? PAGE_MAX);
      if (limit > ROWS_MAX) {
        throw new Error(`load_data can return at most ${PAGE_MAX} rows per call `
                        + `(${ROWS_MAX} in all). Narrow the query instead.`);
      }
      return text(await km.data(columns, String(args.order_by ?? ''),
                                args.ascending !== false, limit));
    }

    // search_data
    const attribute = String(args.attribute ?? '');
    const q = String(args.query ?? '').toLowerCase().trim();
    if (!attribute.includes('.')) {
      throw new Error("search_data needs an attribute written RECORD.field.");
    }
    const rows = asList(await km.data([attribute], '', true, PAGE_MAX));
    const values = [...new Set(rows.map((r) => String(Object.values(r)[0] ?? '')))]
      .filter((v) => v && (!q || v.toLowerCase().includes(q)));
    return text({ attribute, query: q, values });
  };

  return {
    kind: 'km',
    label: `Knowledge Model ${cfg.kmId}`,
    tools,
    blocked,
    call,
    close: async () => {},
  };
}

/* ── the Agent Tools asset ──────────────────────────────────────────────────*/

async function openMcp(cfg: CelonisConfig): Promise<Source> {
  const conn: Connected = await connect(cfg);
  return {
    kind: 'mcp',
    label: 'Agent Tools asset (MCP)',
    tools: conn.tools,
    blocked: conn.blocked,
    call: (name, args) => callTool(conn, cfg, name, args),
    close: conn.close,
  };
}

/* ── picking one ────────────────────────────────────────────────────────────*/

/** Open whichever door the configuration points at.
 *
 *  A configured MCP asset that will not open does NOT silently fall through to
 *  the Knowledge Model API. The two read different things — an asset is a
 *  curated set of tools, the API is everything — so quietly swapping them
 *  would change the answer without telling anybody, and the reason the asset
 *  failed (an unpublished package, a missing 'use' permission) would never be
 *  seen. readConfig has already decided; this just does it. */
export async function openSource(cfg: CelonisConfig): Promise<Source> {
  return cfg.source === 'km' ? openKm(cfg) : openMcp(cfg);
}
