"""Built-in, zero-dependency BPMN 2.0 rule-based uplift engine.

No AI, no API keys, no network required.  Applies deterministic fixes and
produces a clean, importable BPMN 2.0 XML with a complete diagram layout.

Fix passes (in order):
  1. Naming    — blank / generic labels replaced with meaningful names
  2. Structure — missing start/end events, multi-merge/split tasks, gateway issues
  3. Layout    — BPMNDiagram section rebuilt with a clean BFS-based layout
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

# ── Namespace constants ───────────────────────────────────────────────────────
BPMN_NS   = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMNDI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DC_NS     = "http://www.omg.org/spec/DD/20100524/DC"
DI_NS     = "http://www.omg.org/spec/DD/20100524/DI"

B   = f"{{{BPMN_NS}}}"
BDI = f"{{{BPMNDI_NS}}}"
DC  = f"{{{DC_NS}}}"
DI  = f"{{{DI_NS}}}"

# ── Standard Signavio-compatible shape sizes ──────────────────────────────────
_DIM: Dict[str, Tuple[int, int]] = {
    "startEvent":             (36,  36),
    "endEvent":               (36,  36),
    "intermediateThrowEvent": (36,  36),
    "intermediateCatchEvent": (36,  36),
    "exclusiveGateway":       (50,  50),
    "parallelGateway":        (50,  50),
    "inclusiveGateway":       (50,  50),
    "eventBasedGateway":      (50,  50),
    "complexGateway":         (50,  50),
    "task":                   (120, 80),
    "userTask":               (120, 80),
    "serviceTask":            (120, 80),
    "scriptTask":             (120, 80),
    "sendTask":               (120, 80),
    "receiveTask":            (120, 80),
    "manualTask":             (120, 80),
    "businessRuleTask":       (120, 80),
    "callActivity":           (120, 80),
    "subProcess":             (200, 100),
}
_DEFAULT_DIM = (120, 80)

# ── Tag category sets ─────────────────────────────────────────────────────────
_ACTIVITY_TAGS = {
    "task", "userTask", "serviceTask", "scriptTask", "manualTask",
    "sendTask", "receiveTask", "businessRuleTask", "callActivity",
}
_GATEWAY_TAGS = {
    "exclusiveGateway", "parallelGateway", "inclusiveGateway",
    "eventBasedGateway", "complexGateway",
}
_EVENT_TAGS = {
    "startEvent", "endEvent", "intermediateThrowEvent",
    "intermediateCatchEvent", "boundaryEvent",
}

# ── Vocabulary for unnamed tasks (assigned in topological order) ──────────────
_TASK_VOCAB = [
    "Initiate Process",    "Validate Request",     "Review Submission",
    "Process Application", "Assess Requirement",   "Prepare Documentation",
    "Approve Decision",    "Execute Action",        "Notify Stakeholder",
    "Verify Completion",   "Update Record",         "Finalize Outcome",
    "Coordinate Activity", "Confirm Receipt",       "Close Request",
    "Escalate Issue",      "Resolve Incident",      "Archive Record",
]

_GENERIC_RE = re.compile(
    r'^(Task|Event|Gateway|Node|task|event|gateway|node)\s*\d+$', re.I)

# ── Common BPMN verbs for verb-object name enforcement ─────────────────────
# Expanded in v1.2.7 - the 80-verb v1.2.6 set was missing many ordinary
# process verbs (process, audit, book, save, return, ...) which caused the
# "verb at start" rule to leave names like "Process request" or "Save form"
# untouched even though they were already correct.
_BPMN_VERBS = {
    'accept', 'access', 'acquire', 'add', 'adjust', 'allocate', 'amend',
    'analyse', 'analyze', 'apply', 'approve', 'archive', 'assemble',
    'assess', 'assign', 'attach', 'attend', 'audit', 'authenticate',
    'authorise', 'authorize', 'await', 'block', 'book', 'brief', 'budget',
    'build', 'calculate', 'cancel', 'capture', 'certify', 'change',
    'check', 'clarify', 'classify', 'clear', 'close', 'collect', 'compile',
    'complete', 'configure', 'confirm', 'consolidate', 'contact',
    'coordinate', 'copy', 'correct', 'create', 'credit', 'debit',
    'decline', 'define', 'delegate', 'delete', 'deliver', 'deploy',
    'determine', 'develop', 'diagnose', 'discuss', 'dispatch', 'display',
    'distribute', 'document', 'download', 'draft', 'edit', 'email',
    'enable', 'enforce', 'enter', 'escalate', 'establish', 'evaluate',
    'execute', 'export', 'extract', 'feed', 'fetch', 'file', 'fill',
    'finalise', 'finalize', 'fix', 'flag', 'follow', 'forward', 'fulfil',
    'fulfill', 'generate', 'grant', 'group', 'handle', 'hold',
    'identify', 'implement', 'import', 'inform', 'initiate', 'input',
    # 'invoice' deliberately omitted - in BPMN it's almost always the
    # noun ("approve invoice", "send invoice") and including it caused
    # "Approve invoice" to flip to "Invoice approve".
    'inspect', 'install', 'investigate', 'issue', 'label',
    'link', 'list', 'load', 'lock', 'log', 'maintain', 'manage', 'mark',
    'match', 'measure', 'meet', 'merge', 'modify', 'monitor', 'move',
    # 'order' deliberately omitted - in BPMN process models it's almost
    # always a noun ("place order", "cancel order"), not a verb. Including
    # it would cause "Order place" to be left as-is instead of reordered.
    'notify', 'obtain', 'open', 'organise', 'organize', 'output',
    'package', 'pay', 'perform', 'pick', 'place', 'plan', 'post',
    'prepare', 'present', 'print', 'prioritise', 'prioritize', 'process',
    'procure', 'produce', 'provide', 'publish', 'pull',
    # 'purchase' deliberately omitted - in BPMN it's almost always part
    # of a noun phrase like "purchase order". Including it caused names
    # such as "Approve purchase order" and "Complete a purchase order"
    # to be flipped to nonsense. The actual verb in those names is the
    # first word; the last-verb pivot rule handles them correctly once
    # 'purchase' is out of the dict.
    'push', 'raise', 'reach', 'read', 'reassign', 'receive',
    'reconcile', 'record', 'redirect', 'refer', 'register', 'reject',
    'release', 'remove', 'rename', 'repair', 'replace', 'reply', 'report',
    'request', 'require', 'reroute', 'reserve', 'resolve', 'respond',
    'restore', 'retrieve', 'return', 'review', 'revise', 'route', 'run',
    'save', 'scan', 'schedule', 'screen', 'search', 'select', 'send',
    'separate', 'set', 'settle', 'share', 'ship', 'show', 'sign',
    'sort', 'specify', 'split', 'stage', 'start', 'stop', 'store',
    'submit', 'sync', 'tag', 'take', 'test', 'track', 'train', 'transfer',
    'transform', 'transmit', 'trigger', 'unblock', 'unlock', 'unpack',
    'update', 'upload', 'validate', 'verify', 'view', 'vote', 'wait',
    'withdraw', 'write',
}


def _clean_name(name: str) -> str:
    """Clean newlines, CamelCase joins, and excess whitespace from a name."""
    # Replace newlines/carriage returns with spaces
    name = name.replace('\n', ' ').replace('\r', ' ')
    # Split CamelCase joins (e.g. "RequirementsSpecify" → "Requirements Specify")
    name = re.sub(r'([a-z])([A-Z])', r'\1 \2', name)
    # Collapse multiple spaces and strip
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def _verb_object_fix(name: str) -> str:
    """Reorder a task name to verb-object format.

    v1.2.9 algorithm:

      * If 1 word, return as-is.
      * If 2 words AND the first word is a verb, prefer the first-verb
        form (handles "Approve invoice", "Send notification" - even when
        the second word is also in the verb dict).
      * Otherwise find the LAST verb in the name (BPMN tasks often have
        the verb last, e.g. "Purchase order check") and pivot the name
        around it: take the verb (and any verb tail like "and quantify")
        to the front, demote the rest to the object suffix.
      * If no verb is found anywhere, return unchanged.

    Examples:
      "Purchase order check"               -> "Check purchase order"
      "Order place"                        -> "Place order"
      "Approve invoice"                    -> "Approve invoice"        (unchanged)
      "Process request"                    -> "Process request"        (unchanged)
      "Requirements specify and quantify"  -> "Specify and quantify requirements"
    """
    words = name.split()
    if len(words) < 2:
        return name

    first_is_verb = words[0].lower() in _BPMN_VERBS

    # 2-word names: if the first word is a verb, that's good enough.
    # Avoids spurious flips like "Approve invoice" -> "Invoice approve".
    if len(words) == 2 and first_is_verb:
        return name

    # 3+ words OR 2-word name where the first is NOT a verb: pivot
    # around the LAST verb.
    last_verb_idx = -1
    for i, w in enumerate(words):
        if w.lower() in _BPMN_VERBS:
            last_verb_idx = i

    if last_verb_idx <= 0:
        # No verb at all, or only at position 0 (genuine verb-first).
        return name

    # Walk back through any "and X" / "or X" connectors to keep
    # multi-verb tails like "specify and quantify" intact.
    _CONNECTORS = {"and", "or", "&"}
    tail_start = last_verb_idx
    while tail_start - 2 >= 0:
        prev_prev = words[tail_start - 2].lower()
        prev      = words[tail_start - 1].lower()
        if prev in _CONNECTORS and prev_prev in _BPMN_VERBS:
            tail_start -= 2
        else:
            break

    if tail_start == 0:
        return name

    verb_part = " ".join(words[tail_start:])
    noun_part = " ".join(words[:tail_start])
    return verb_part[0].upper() + verb_part[1:] + " " + noun_part.lower()


def _lt(full_tag: str) -> str:
    """Return local name from Clark notation `{ns}local`."""
    return full_tag.split("}")[-1] if "}" in full_tag else full_tag


def _is_generic(name: str) -> bool:
    n = name.strip()
    return not n or bool(_GENERIC_RE.match(n))


def _int_suffix(s: str) -> int:
    m = re.search(r'(\d+)$', s)
    return int(m.group(1)) if m else 0


# ═══════════════════════════════════════════════════════════════════════════════
# Public entry point
# ═══════════════════════════════════════════════════════════════════════════════

def uplift_local(
    xml_content: str,
    issues,                                  # List[ValidationIssue] — duck-typed
    process_name: str,
    on_chunk:    Optional[Callable[[str], None]],
    on_complete: Optional[Callable[[str], None]],
    on_error:    Optional[Callable[[str], None]],
) -> None:
    """Apply built-in rule-based BPMN uplift. Calls *on_complete* with result XML."""
    try:
        if on_chunk:
            on_chunk("Built-in engine: applying naming fixes...\n")
        result = _do_uplift(xml_content, process_name)
        if on_chunk:
            on_chunk("Built-in engine: applying structural fixes...\n")
            on_chunk("Built-in engine: rebuilding diagram layout...\n")
            on_chunk(f"Built-in engine: done — {len(result):,} chars output.\n\n")
        if on_complete:
            on_complete(result)
    except Exception as exc:
        if on_error:
            on_error(f"Built-in Uplift failed: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
# Core pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def _do_uplift(xml_content: str, process_name: str) -> str:
    # Register all common BPMN namespace prefixes so serialisation is clean.
    # Use "" (default) for the BPMN model namespace when the input file does
    # the same (Signavio exports use xmlns="..." with no prefix).
    _uses_default_ns = (
        'xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL"' in xml_content
    )
    if _uses_default_ns:
        ET.register_namespace("",       BPMN_NS)
    else:
        ET.register_namespace("bpmn",   BPMN_NS)
    ET.register_namespace("bpmndi", BPMNDI_NS)
    # Signavio exports use omgdc/omgdi prefixes — preserve them if present
    _uses_omg_prefix = 'xmlns:omgdc=' in xml_content
    if _uses_omg_prefix:
        ET.register_namespace("omgdc", DC_NS)
        ET.register_namespace("omgdi", DI_NS)
    else:
        ET.register_namespace("dc",    DC_NS)
        ET.register_namespace("di",    DI_NS)
    ET.register_namespace("xsi",    "http://www.w3.org/2001/XMLSchema-instance")
    # Signavio-specific namespace prefixes
    ET.register_namespace("signavio", "http://www.signavio.com")
    ET.register_namespace("i18n",
                          "http://www.omg.org/spec/BPMN/non-normative/extensions/i18n/1.0")

    root = ET.fromstring(xml_content)

    processes = list(root.iter(f"{B}process"))
    if not processes:
        raise ValueError("No <bpmn:process> element found in the uploaded file.")

    for proc in processes:
        _uplift_process(proc, process_name)

    _rebuild_di(root)

    ET.indent(root, space="  ")
    xml = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")

    # v1.2.4 - run the same Signavio-compatibility normaliser the AI
    # uplift paths use, so the built-in rule engine produces output that
    # imports cleanly into Signavio (single-quoted XML decl, missing i18n
    # namespace, missing BPMNShape per node, etc. all get fixed in one pass).
    try:
        from .signavio_normalize import normalize_for_signavio
        xml = normalize_for_signavio(xml)
    except Exception:
        # Never let a normaliser bug break a working uplift; fall back to
        # the raw ET output if anything goes wrong.
        pass
    return xml


# ═══════════════════════════════════════════════════════════════════════════════
# Process-level uplift
# ═══════════════════════════════════════════════════════════════════════════════

def _uplift_process(proc: ET.Element, process_name: str) -> None:
    # Only set process name if it's completely empty — never overwrite existing names
    existing_name = (proc.get("name") or "").strip()
    if not existing_name and process_name:
        proc.set("name", process_name)

    elems, flows = _collect(proc)

    # Pass 1: fix naming
    _fix_naming(elems, flows)

    # Pass 2: fix structure (returns refreshed view after mutations)
    elems, flows = _fix_structure(proc, elems, flows)

    # Write final names back to their XML elements
    for info in elems.values():
        xml_el = info.get("xml_el")
        if xml_el is not None:
            xml_el.set("name", info["name"])


# ── Collect process graph ─────────────────────────────────────────────────────

def _collect(proc: ET.Element):
    """Return (elems, flows) dicts from a process element's direct children."""
    elems: Dict[str, dict] = {}
    flows: Dict[str, dict] = {}

    for child in proc:
        lt  = _lt(child.tag)
        eid = child.get("id")
        if not eid:
            continue

        if lt == "sequenceFlow":
            src = child.get("sourceRef", "")
            tgt = child.get("targetRef", "")
            if src and tgt:
                flows[eid] = {"source": src, "target": tgt, "xml_el": child}
        elif lt in (_ACTIVITY_TAGS | _GATEWAY_TAGS | _EVENT_TAGS):
            elems[eid] = {
                "local_tag": lt,
                "name":      child.get("name", "") or "",
                "xml_el":    child,
                "incoming":  [],
                "outgoing":  [],
            }

    for fid, fl in flows.items():
        if fl["source"] in elems:
            elems[fl["source"]]["outgoing"].append(fid)
        if fl["target"] in elems:
            elems[fl["target"]]["incoming"].append(fid)

    return elems, flows


def _infer_gateway_question(gw_info: dict, flows: Dict[str, dict]) -> str:
    """Try to derive a gateway question from its outgoing sequence flow labels.

    E.g. flows labelled "Work equipment in stock" / "Work equipment not in stock"
    → gateway name "Work equipment in stock?"
    """
    labels: List[str] = []
    for fid in gw_info.get("outgoing", []):
        fl = flows.get(fid)
        if fl:
            xml_el = fl.get("xml_el")
            if xml_el is not None:
                lbl = _clean_name(xml_el.get("name", ""))
                if lbl:
                    labels.append(lbl)
    if not labels:
        return ""
    # Sort by length — shortest (positive) form comes first
    labels.sort(key=len)
    base = labels[0].rstrip("?").strip()
    if base:
        return base + "?"
    return ""


# ── Pass 1: Naming ────────────────────────────────────────────────────────────

def _fix_naming(elems: Dict[str, dict], flows: Dict[str, dict]) -> None:
    """Clean names, enforce verb-object on tasks, fill blanks."""
    # Pre-pass 1: clean all names (newlines, CamelCase joins, whitespace)
    for info in elems.values():
        info["name"] = _clean_name(info["name"])

    # Pre-pass 2: fix verb-object ordering for named tasks
    for info in elems.values():
        if info["local_tag"] in _ACTIVITY_TAGS and info["name"].strip():
            info["name"] = _verb_object_fix(info["name"])

    # Pre-pass 3: clean sequence flow labels too
    for fl in flows.values():
        xml_el = fl.get("xml_el")
        if xml_el is not None:
            old = xml_el.get("name", "")
            cleaned = _clean_name(old)
            if cleaned != old:
                xml_el.set("name", cleaned)

    order    = _topo_order(elems, flows)
    task_idx = 0
    gw_idx   = 0

    for eid in order:
        info = elems.get(eid)
        if info is None:
            continue
        lt   = info["local_tag"]
        name = info["name"]

        if lt in _ACTIVITY_TAGS and _is_generic(name):
            info["name"] = _TASK_VOCAB[task_idx % len(_TASK_VOCAB)]
            task_idx += 1

        elif lt in _EVENT_TAGS and _is_generic(name):
            if lt == "startEvent":
                info["name"] = "Start"
            elif lt == "endEvent":
                info["name"] = "End"
            elif "catch" in lt.lower():
                info["name"] = "Event Received"
            else:
                info["name"] = "Process Complete"

        elif lt in _GATEWAY_TAGS and _is_generic(name):
            gw_idx += 1
            n_out = len(info["outgoing"])
            if "parallel" in lt.lower():
                info["name"] = "Fork" if n_out > 1 else "Join"
            elif "event" in lt.lower():
                # Event-based gateways route on events, not decisions
                info["name"] = ""
            elif "exclusive" in lt.lower():
                if n_out > 1:
                    # Try to infer question from outgoing flow labels
                    info["name"] = _infer_gateway_question(info, flows) \
                                   or f"Decision {gw_idx}?"
                else:
                    info["name"] = f"Merge {gw_idx}"
            elif "inclusive" in lt.lower():
                info["name"] = f"Choice {gw_idx}?" if n_out > 1 else f"Merge {gw_idx}"
            else:
                info["name"] = f"Gateway {gw_idx}"


# ── Pass 2: Structure ─────────────────────────────────────────────────────────

def _fix_structure(
    proc:  ET.Element,
    elems: Dict[str, dict],
    flows: Dict[str, dict],
) -> tuple:
    """Apply structural BPMN fixes in-place on *proc*. Returns refreshed (elems, flows)."""

    existing = [_int_suffix(k) for k in list(elems) + list(flows)]
    ctr = [max(existing, default=0) + 1]

    def _nid(prefix: str) -> str:
        uid = f"{prefix}_{ctr[0]:03d}"
        ctr[0] += 1
        while uid in elems or uid in flows:
            uid = f"{prefix}_{ctr[0]:03d}"
            ctr[0] += 1
        return uid

    def _add_elem(local_tag: str, name: str) -> str:
        eid    = _nid(local_tag[:3].lower())
        xml_el = ET.SubElement(proc, f"{B}{local_tag}", {"id": eid, "name": name})
        elems[eid] = {
            "local_tag": local_tag, "name": name, "xml_el": xml_el,
            "incoming": [], "outgoing": [],
        }
        return eid

    def _add_flow(src: str, tgt: str) -> str:
        fid    = _nid("flo")
        xml_el = ET.SubElement(
            proc, f"{B}sequenceFlow", {"id": fid, "sourceRef": src, "targetRef": tgt}
        )
        flows[fid] = {"source": src, "target": tgt, "xml_el": xml_el}
        if src in elems:
            elems[src]["outgoing"].append(fid)
        if tgt in elems:
            elems[tgt]["incoming"].append(fid)
        return fid

    def _remove_flow(fid: str) -> None:
        fl = flows.pop(fid, None)
        if fl is None:
            return
        for eid, key in [(fl["source"], "outgoing"), (fl["target"], "incoming")]:
            info = elems.get(eid)
            if info and fid in info[key]:
                info[key].remove(fid)
        try:
            proc.remove(fl["xml_el"])
        except ValueError:
            pass

    def _remove_elem(eid: str) -> None:
        info = elems.pop(eid, None)
        if info and info.get("xml_el") is not None:
            try:
                proc.remove(info["xml_el"])
            except ValueError:
                pass

    # ── Fix 1: add missing start event ───────────────────────────────────────
    starts = [eid for eid, i in elems.items() if i["local_tag"] == "startEvent"]
    if not starts:
        no_inc = [
            eid for eid, i in elems.items()
            if not i["incoming"] and i["local_tag"] not in _EVENT_TAGS
        ]
        target = no_inc[0] if no_inc else (list(elems)[0] if elems else None)
        if target:
            se = _add_elem("startEvent", "Start")
            _add_flow(se, target)

    # ── Fix 2: add missing end event ─────────────────────────────────────────
    ends = [eid for eid, i in elems.items() if i["local_tag"] == "endEvent"]
    if not ends:
        no_out = [
            eid for eid, i in elems.items()
            if not i["outgoing"] and i["local_tag"] not in _EVENT_TAGS
        ]
        source = no_out[-1] if no_out else (list(elems)[-1] if elems else None)
        if source:
            ee = _add_elem("endEvent", "End")
            _add_flow(source, ee)

    # ── Fix 3: remove trivial gateways (1 in + 1 out) ──────────────────────
    # Only remove if the gateway has a generic name — a named gateway with
    # 1-in/1-out may be intentional (e.g. routing gateway kept for clarity).
    for eid in list(elems):
        info = elems.get(eid)
        if not info or info["local_tag"] not in _GATEWAY_TAGS:
            continue
        if len(info["incoming"]) == 1 and len(info["outgoing"]) == 1:
            if not _is_generic(info["name"]):
                continue  # Preserve named gateways
            src = flows[info["incoming"][0]]["source"]
            tgt = flows[info["outgoing"][0]]["target"]
            _remove_flow(info["incoming"][0])
            _remove_flow(info["outgoing"][0])
            _add_flow(src, tgt)
            _remove_elem(eid)

    # ── Fix 4: task with multiple outgoing flows → insert exclusive gateway ──
    for eid in list(elems):
        info = elems.get(eid)
        if not info or info["local_tag"] not in _ACTIVITY_TAGS:
            continue
        if len(info["outgoing"]) <= 1:
            continue
        # Insert an exclusive gateway after this task
        gw_id = _add_elem("exclusiveGateway", f"{info['name']}?")
        out_flows = list(info["outgoing"])
        for fid in out_flows:
            tgt = flows[fid]["target"]
            _remove_flow(fid)
            _add_flow(gw_id, tgt)
        _add_flow(eid, gw_id)

    # ── Fix 5: task with multiple incoming flows → insert exclusive gateway ──
    for eid in list(elems):
        info = elems.get(eid)
        if not info or info["local_tag"] not in _ACTIVITY_TAGS:
            continue
        if len(info["incoming"]) <= 1:
            continue
        # Insert an exclusive gateway before this task
        gw_id = _add_elem("exclusiveGateway", "Merge")
        in_flows = list(info["incoming"])
        for fid in in_flows:
            src = flows[fid]["source"]
            _remove_flow(fid)
            _add_flow(src, gw_id)
        _add_flow(gw_id, eid)

    # ── Fix 6: XOR gateways with multiple outgoing should have a name ────────
    for eid in list(elems):
        info = elems.get(eid)
        if not info:
            continue
        if "exclusive" in info["local_tag"].lower() and len(info["outgoing"]) > 1:
            if not info["name"].strip() or info["name"].strip() == "Merge":
                info["name"] = f"Decision?"

    # ── Fix 7: start events should not have incoming flows ────────────────────
    for eid in list(elems):
        info = elems.get(eid)
        if not info or info["local_tag"] != "startEvent":
            continue
        for fid in list(info["incoming"]):
            _remove_flow(fid)

    # ── Fix 8: end events should not have outgoing flows ──────────────────────
    for eid in list(elems):
        info = elems.get(eid)
        if not info or info["local_tag"] != "endEvent":
            continue
        for fid in list(info["outgoing"]):
            _remove_flow(fid)

    return elems, flows


# ═══════════════════════════════════════════════════════════════════════════════
# DI section rebuild
# ═══════════════════════════════════════════════════════════════════════════════

def _read_existing_di(root: ET.Element):
    """Read all existing DI data from the BPMNDiagram section.

    Returns:
        existing_pos: {elementId: (x, y, w, h)} for shapes
        existing_edges: {flowId: [(x1,y1), (x2,y2), ...]} waypoints
        extra_shapes: list of (bpmnElement, ET.Element) for pool/lane/non-process shapes
        extra_edges: list of (bpmnElement, ET.Element) for message flow edges
        diagram_attrs: dict of attributes from the BPMNDiagram element
        plane_attrs: dict of attributes from the BPMNPlane element
        label_styles: list of ET.Element for BPMNLabelStyle elements
    """
    existing_pos: Dict[str, Tuple[float, float, float, float]] = {}
    existing_edges: Dict[str, List[Tuple[float, float]]] = {}
    extra_shapes: List[ET.Element] = []
    extra_edges: List[ET.Element] = []
    label_styles: List[ET.Element] = []
    diagram_attrs: dict = {}
    plane_attrs: dict = {}

    # Collect IDs of process-level flow nodes and sequence flows
    process_elem_ids: set = set()
    process_flow_ids: set = set()
    for proc in root.iter(f"{B}process"):
        for child in proc:
            lt = _lt(child.tag)
            eid = child.get("id")
            if not eid:
                continue
            if lt == "sequenceFlow":
                process_flow_ids.add(eid)
            elif lt in (_ACTIVITY_TAGS | _GATEWAY_TAGS | _EVENT_TAGS):
                process_elem_ids.add(eid)

    for diag in root.iter(f"{BDI}BPMNDiagram"):
        diagram_attrs = dict(diag.attrib)
        for plane in diag.iter(f"{BDI}BPMNPlane"):
            plane_attrs = dict(plane.attrib)
            for child in plane:
                child_tag = _lt(child.tag)
                bpmn_el = child.get("bpmnElement", "")

                if child_tag == "BPMNShape":
                    bounds = child.find(f"{DC}Bounds")
                    if bounds is not None:
                        try:
                            x = float(bounds.get("x", "0"))
                            y = float(bounds.get("y", "0"))
                            w = float(bounds.get("width", "120"))
                            h = float(bounds.get("height", "80"))
                            existing_pos[bpmn_el] = (x, y, w, h)
                        except (ValueError, TypeError):
                            pass
                    # Keep pool/lane/participant shapes as extra
                    if bpmn_el and bpmn_el not in process_elem_ids:
                        extra_shapes.append(child)

                elif child_tag == "BPMNEdge":
                    waypoints = []
                    for wp in child.iter(f"{DI}waypoint"):
                        try:
                            wx = float(wp.get("x", "0"))
                            wy = float(wp.get("y", "0"))
                            waypoints.append((wx, wy))
                        except (ValueError, TypeError):
                            pass
                    if bpmn_el and waypoints:
                        existing_edges[bpmn_el] = waypoints
                    # Keep message flow edges as extra
                    if bpmn_el and bpmn_el not in process_flow_ids:
                        extra_edges.append(child)

        # Collect label styles
        for ls in diag:
            if _lt(ls.tag) == "BPMNLabelStyle":
                label_styles.append(ls)

    return (existing_pos, existing_edges, extra_shapes, extra_edges,
            diagram_attrs, plane_attrs, label_styles)


def _rebuild_di(root: ET.Element) -> None:
    """Rebuild BPMNDiagram section, preserving existing positions, edges, and pools."""
    # ── Read ALL existing DI data BEFORE removing old diagrams ─────────────
    (existing_pos, existing_edges, extra_shapes, extra_edges,
     diagram_attrs, plane_attrs, label_styles) = _read_existing_di(root)

    for diag in list(root.findall(f"{BDI}BPMNDiagram")):
        root.remove(diag)

    all_elems: Dict[str, dict] = {}
    all_flows: Dict[str, dict] = {}
    proc_id = "Process_1"

    for proc in root.iter(f"{B}process"):
        proc_id = proc.get("id", proc_id)
        for child in proc:
            lt  = _lt(child.tag)
            eid = child.get("id")
            if not eid:
                continue
            if lt == "sequenceFlow":
                src = child.get("sourceRef", "")
                tgt = child.get("targetRef", "")
                if src and tgt:
                    all_flows[eid] = {"source": src, "target": tgt}
            elif lt in (_ACTIVITY_TAGS | _GATEWAY_TAGS | _EVENT_TAGS):
                all_elems[eid] = {"local_tag": lt}

    if not all_elems:
        return

    # ── Decide layout strategy ─────────────────────────────────────────────
    positioned = {eid for eid in all_elems if eid in existing_pos}
    if len(positioned) >= len(all_elems) * 0.5:
        positions = _preserve_layout(all_elems, all_flows, existing_pos)
    else:
        positions = _compute_layout(all_elems, all_flows)

    # ── Build diagram using original attributes where available ────────────
    d_attrs = diagram_attrs or {"id": "BPMNDiagram_1", "name": "DiagramIQ layout"}
    p_attrs = plane_attrs or {"id": "BPMNPlane_1", "bpmnElement": proc_id}

    diagram = ET.SubElement(root, f"{BDI}BPMNDiagram", d_attrs)
    plane = ET.SubElement(diagram, f"{BDI}BPMNPlane", p_attrs)

    # ── Re-add pool/lane/participant shapes (preserved verbatim) ──────────
    for shape_el in extra_shapes:
        plane.append(shape_el)

    # ── Add process element shapes ────────────────────────────────────────
    for eid, (x, y, w, h) in positions.items():
        shape = ET.SubElement(
            plane, f"{BDI}BPMNShape", {"id": f"BPMNShape_{eid}", "bpmnElement": eid}
        )
        ET.SubElement(
            shape, f"{DC}Bounds",
            {"x": str(int(x)), "y": str(int(y)),
             "width": str(int(w)), "height": str(int(h))},
        )

    # ── Add sequence flow edges ───────────────────────────────────────────
    for fid, fl in all_flows.items():
        src_pos = positions.get(fl["source"])
        tgt_pos = positions.get(fl["target"])
        if not src_pos or not tgt_pos:
            continue

        edge = ET.SubElement(
            plane, f"{BDI}BPMNEdge", {"id": f"BPMNEdge_{fid}", "bpmnElement": fid}
        )

        # Always compute clean orthogonal waypoints (original Signavio
        # waypoints often have bends/curves that look untidy in other viewers)
        _add_orthogonal_waypoints(edge, src_pos, tgt_pos)

    # ── Re-add message flow edges (preserved verbatim) ────────────────────
    for edge_el in extra_edges:
        plane.append(edge_el)

    # ── Re-add label styles ───────────────────────────────────────────────
    for ls in label_styles:
        diagram.append(ls)


def _add_orthogonal_waypoints(edge: ET.Element,
                              src: Tuple[float, float, float, float],
                              tgt: Tuple[float, float, float, float]) -> None:
    """Add clean waypoints between source and target shapes.

    Strategy:
      - Nearly aligned → straight 2-point line (slight slope OK in BPMN)
      - Small vertical offset on horizontal flow → straight connection
      - Significant offset → L-bend (3 points): exit top/bottom, align Y,
        enter left/right — matches Signavio best-practice style.
    """
    sx, sy, sw, sh = src
    tx, ty, tw, th = tgt
    src_cx, src_cy = sx + sw / 2, sy + sh / 2
    tgt_cx, tgt_cy = tx + tw / 2, ty + th / 2
    dx = tgt_cx - src_cx
    dy = tgt_cy - src_cy

    def _wp(x: float, y: float) -> None:
        ET.SubElement(edge, f"{DI}waypoint",
                      {"x": str(int(x)), "y": str(int(y))})

    # Overlapping centres → degenerate straight line
    if abs(dx) < 5 and abs(dy) < 5:
        _wp(src_cx, src_cy)
        _wp(tgt_cx, tgt_cy)
        return

    # Thresholds: a slight slope looks fine in BPMN viewers, so only
    # switch to L-bends when the offset is significant relative to the
    # elements involved.
    v_thresh = max(sh, th) * 0.4
    h_thresh = max(sw, tw) * 0.4

    # ── Primarily horizontal flow ────────────────────────────────────
    if abs(dx) >= abs(dy):
        if dx >= 0:
            x_exit, y_exit = sx + sw, src_cy      # exit right
            x_enter, y_enter = tx, tgt_cy          # enter left
        else:
            x_exit, y_exit = sx, src_cy            # exit left
            x_enter, y_enter = tx + tw, tgt_cy     # enter right

        if abs(dy) <= v_thresh:
            # Small Y offset → straight connection (2 points)
            _wp(x_exit, y_exit)
            _wp(x_enter, y_enter)
        else:
            # Large Y offset → clean L-bend via top/bottom of source
            if dy < 0:
                _wp(src_cx, sy)            # exit top
            else:
                _wp(src_cx, sy + sh)       # exit bottom
            _wp(src_cx, tgt_cy)            # vertical run to target Y
            _wp(x_enter, tgt_cy)           # horizontal run to target
        return

    # ── Primarily vertical flow ──────────────────────────────────────
    if abs(dx) <= h_thresh:
        # Small X offset → straight vertical connection (2 points)
        if dy >= 0:
            _wp(src_cx, sy + sh)           # exit bottom
            _wp(tgt_cx, ty)                # enter top
        else:
            _wp(src_cx, sy)                # exit top
            _wp(tgt_cx, ty + th)           # enter bottom
    else:
        # Large X offset → clean L-bend: exit top/bottom, align Y, enter side
        if dy < 0:
            _wp(src_cx, sy)                # exit top
        else:
            _wp(src_cx, sy + sh)           # exit bottom
        _wp(src_cx, tgt_cy)               # vertical run to target Y
        if dx >= 0:
            _wp(tx, tgt_cy)               # enter target left
        else:
            _wp(tx + tw, tgt_cy)           # enter target right


def _preserve_layout(
    all_elems: Dict[str, dict],
    all_flows: Dict[str, dict],
    existing_pos: Dict[str, Tuple[float, float, float, float]],
) -> Dict[str, Tuple[float, float, float, float]]:
    """Keep existing positions and place only new elements near their neighbours."""
    positions: Dict[str, Tuple[float, float, float, float]] = {}

    # Copy all existing positions
    for eid in all_elems:
        if eid in existing_pos:
            positions[eid] = existing_pos[eid]

    # For elements added by structural fixes, place them near neighbours
    new_eids = [eid for eid in all_elems if eid not in existing_pos]
    if not new_eids:
        return positions

    # Build adjacency from flows
    out_adj: Dict[str, List[str]] = {eid: [] for eid in all_elems}
    in_adj:  Dict[str, List[str]] = {eid: [] for eid in all_elems}
    for fl in all_flows.values():
        if fl["source"] in out_adj:
            out_adj[fl["source"]].append(fl["target"])
        if fl["target"] in in_adj:
            in_adj[fl["target"]].append(fl["source"])

    for eid in new_eids:
        lt = all_elems[eid]["local_tag"]
        w, h = _DIM.get(lt, _DEFAULT_DIM)

        # Find positioned neighbours
        neighbours = []
        for src in in_adj.get(eid, []):
            if src in positions:
                neighbours.append(positions[src])
        for tgt in out_adj.get(eid, []):
            if tgt in positions:
                neighbours.append(positions[tgt])

        if neighbours:
            # Place between neighbours
            avg_x = sum(p[0] for p in neighbours) / len(neighbours)
            avg_y = sum(p[1] for p in neighbours) / len(neighbours)
            # Offset slightly to the right of the average
            if in_adj.get(eid) and out_adj.get(eid):
                # Between source and target
                in_pos = [positions[s] for s in in_adj[eid] if s in positions]
                out_pos = [positions[t] for t in out_adj[eid] if t in positions]
                if in_pos and out_pos:
                    ix = sum(p[0] + p[2] for p in in_pos) / len(in_pos)
                    ox = sum(p[0] for p in out_pos) / len(out_pos)
                    avg_x = (ix + ox) / 2 - w / 2
                    avg_y = (sum(p[1] + p[3]/2 for p in in_pos) / len(in_pos)
                             + sum(p[1] + p[3]/2 for p in out_pos) / len(out_pos)) / 2 - h / 2
            positions[eid] = (max(50.0, avg_x), max(50.0, avg_y), float(w), float(h))
        else:
            # No neighbours positioned yet — place at bottom
            max_y = max((p[1] + p[3] for p in positions.values()), default=50)
            positions[eid] = (60.0, max_y + 60.0, float(w), float(h))

    return positions


def _compute_layout(
    all_elems: Dict[str, dict],
    all_flows: Dict[str, dict],
) -> Dict[str, Tuple[float, float, float, float]]:
    """Assign (x, y, w, h) to every element using BFS level-based layout."""
    out_adj: Dict[str, List[str]] = {eid: [] for eid in all_elems}
    in_adj:  Dict[str, List[str]] = {eid: [] for eid in all_elems}
    for fl in all_flows.values():
        if fl["source"] in out_adj:
            out_adj[fl["source"]].append(fl["target"])
        if fl["target"] in in_adj:
            in_adj[fl["target"]].append(fl["source"])

    roots = [
        eid for eid in all_elems
        if not in_adj[eid] or all_elems[eid]["local_tag"] == "startEvent"
    ]
    if not roots:
        roots = [next(iter(all_elems))]

    level: Dict[str, int] = {}
    q = deque()
    for r in roots:
        if r not in level:
            level[r] = 0
            q.append(r)
    # Standard BFS — assign each node exactly once.
    # (Updating levels on re-visit causes infinite loops on cyclic graphs.)
    while q:
        curr = q.popleft()
        for nxt in out_adj.get(curr, []):
            if nxt not in level:
                level[nxt] = level[curr] + 1
                q.append(nxt)

    max_lv = max(level.values(), default=-1)
    for eid in all_elems:
        if eid not in level:
            max_lv += 1
            level[eid] = max_lv

    by_level: Dict[int, List[str]] = {}
    for eid, lv in level.items():
        by_level.setdefault(lv, []).append(eid)

    H_GAP  = 100
    V_GAP  = 60
    MX     = 60
    MY     = 60
    max_w  = max(w for (w, h) in _DIM.values())
    max_h  = max(h for (w, h) in _DIM.values())

    # Center nodes at each level vertically around the midpoint of the tallest column
    max_col_height = max(
        len(nodes) * (max_h + V_GAP) - V_GAP for nodes in by_level.values()
    )

    positions: Dict[str, Tuple[float, float, float, float]] = {}
    for lv in sorted(by_level):
        nodes  = by_level[lv]
        col_x  = MX + lv * (max_w + H_GAP)
        col_height = len(nodes) * (max_h + V_GAP) - V_GAP
        y_offset = MY + (max_col_height - col_height) / 2
        for rank, eid in enumerate(nodes):
            lt   = all_elems[eid]["local_tag"]
            w, h = _DIM.get(lt, _DEFAULT_DIM)
            x    = col_x + (max_w - w) / 2
            y    = y_offset + rank * (max_h + V_GAP) + (max_h - h) / 2
            positions[eid] = (x, y, float(w), float(h))

    return positions


# ── Topological sort (used by naming pass) ────────────────────────────────────

def _topo_order(elems: Dict[str, dict], flows: Dict[str, dict]) -> List[str]:
    out_adj: Dict[str, List[str]] = {eid: [] for eid in elems}
    in_deg:  Dict[str, int]       = {eid: 0  for eid in elems}
    for fl in flows.values():
        if fl["source"] in out_adj and fl["target"] in in_deg:
            out_adj[fl["source"]].append(fl["target"])
            in_deg[fl["target"]] += 1

    q     = deque(eid for eid, d in in_deg.items() if d == 0)
    order: List[str] = []
    while q:
        curr = q.popleft()
        order.append(curr)
        for nxt in out_adj.get(curr, []):
            in_deg[nxt] -= 1
            if in_deg[nxt] == 0:
                q.append(nxt)

    for eid in elems:
        if eid not in order:
            order.append(eid)
    return order
