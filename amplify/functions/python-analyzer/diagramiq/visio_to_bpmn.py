"""Convert Microsoft Visio diagrams (.vsdx / .vsd) to Signavio-ready BPMN 2.0.

The converter extracts only the *semantic graph* from Visio - shapes + their
type, swimlane membership, and the directed connectors between them - and emits
a structurally-complete BPMN 2.0 process with an empty diagram plane. The
existing pipeline then finishes the job:
  * `signavio_normalize.normalize_for_signavio` fabricates every BPMNShape /
    BPMNEdge that is missing and makes the file Signavio-valid, and
  * `bpmn_layout_fixer.fix_layout` (run by the caller in _finalize_creation)
    re-grids it into DiagramIQ's clean swimlane layout.

So we never translate Visio's fragile inch / bottom-left-origin coordinates -
we only need each shape's TYPE, TEXT, lane membership and connections.

`.vsdx` is an OPC package (a ZIP of XML) and is parsed with the standard
library only (`zipfile` + `xml.etree`). Visio emits one of two content
namespaces (2011 vs 2012), so EVERYTHING here matches on the tag's *local
name* and never a hard-coded namespace URI.

Legacy binary `.vsd` cannot be read with the stdlib; it is converted to a
temporary `.vsdx` by driving Microsoft Visio's COM API from PowerShell (no
extra Python dependency) - or LibreOffice if present - and then parsed.

Public API
----------
    build_bpmn_from_visio(file_path: str, process_name: str = "") -> str
"""
from __future__ import annotations

import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# BPMN namespaces - identical to excel_to_bpmn.py
BPMN   = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMNDI = "http://www.omg.org/spec/BPMN/20100524/DI"
DC     = "http://www.omg.org/spec/DD/20100524/DC"
DI     = "http://www.omg.org/spec/DD/20100524/DI"
XSI    = "http://www.w3.org/2001/XMLSchema-instance"

# Relationship namespaces (OPC)
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


class VisioConversionError(Exception):
    """Raised when a Visio file cannot be turned into a usable process."""


# ---------------------------------------------------------------------------
# Raw Visio intermediate model
# ---------------------------------------------------------------------------
@dataclass
class VShape:
    vid: str
    master: str                 # resolved stencil master name (NameU), lower-cased ""
    stype: str                  # @Type: Shape / Group / Foreign / Guide
    text: str
    cx: float = 0.0
    cy: float = 0.0
    w: float = 0.0
    h: float = 0.0
    left: float = 0.0
    bottom: float = 0.0
    right: float = 0.0
    top: float = 0.0
    is_connector: bool = False


@dataclass
class VLane:
    name: str
    cy: float
    bottom: float
    top: float


@dataclass
class VNode:
    id: str                     # emitted BPMN id
    tag: str                    # task / exclusiveGateway / startEvent / ...
    name: str
    lane: str = ""


@dataclass
class VData:
    dor_id: str
    do_id: str
    name: str


@dataclass
class VEdge:
    src: str                    # source VShape vid
    tgt: str                    # target VShape vid


# ---------------------------------------------------------------------------
# Master-name classification keywords
# ---------------------------------------------------------------------------
_CONNECTOR_KW  = ("dynamic connector", "connector", "sequence flow",
                  "message flow", "association", "data flow", "control flow",
                  "line", "arrow")
_LANE_KW       = ("swimlane", "swim lane")
_CONTAINER_KW  = ("cross-functional", "cross functional", "pool", "separator",
                  "phase", "container", "list")
_ANNOTATION_KW = ("annotation", "callout", "legend", "title block", "note",
                  "off-page", "on-page reference")

# flow-node BPMN tags (vs data / non-flow)
_FLOW_TAGS = {"task", "subProcess", "startEvent", "endEvent",
              "intermediateThrowEvent", "intermediateCatchEvent",
              "exclusiveGateway", "parallelGateway", "inclusiveGateway",
              "eventBasedGateway"}
_GW_TAGS   = {"exclusiveGateway", "parallelGateway",
              "inclusiveGateway", "eventBasedGateway"}


# ---------------------------------------------------------------------------
# Small XML helpers (namespace-agnostic - match on local name)
# ---------------------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _kids(el, name: str):
    return [c for c in list(el) if _local(c.tag) == name]


def _first(el, name: str):
    for c in list(el):
        if _local(c.tag) == name:
            return c
    return None


def _iter_local(root, name: str):
    for el in root.iter():
        if _local(el.tag) == name:
            yield el


def _text_of(shape) -> str:
    t = _first(shape, "Text")
    if t is None:
        return ""
    return " ".join("".join(t.itertext()).split()).strip()


def _cells(shape) -> Dict[str, str]:
    """Direct-child <Cell N=.. V=.. F=..> -> {N: V or formula}."""
    out: Dict[str, str] = {}
    for c in _kids(shape, "Cell"):
        n = c.get("N")
        if not n:
            continue
        out[n] = c.get("V") if c.get("V") is not None else (c.get("F") or "")
    return out


def _fnum(d: Dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        return float(d.get(key))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# .vsdx (ZIP) extraction
# ---------------------------------------------------------------------------
def _zip_get(zf: zipfile.ZipFile, name: str) -> Optional[bytes]:
    try:
        return zf.read(name)
    except KeyError:
        # zip entries are case-sensitive; try a case-insensitive match
        low = name.lower()
        for n in zf.namelist():
            if n.lower() == low:
                return zf.read(n)
        return None


def _first_page_xml(zf: zipfile.ZipFile) -> bytes:
    """Return the content XML bytes of the FIRST page."""
    names = zf.namelist()
    # Resolve via pages.xml + its .rels (robust ordering)
    pages = _zip_get(zf, "visio/pages/pages.xml")
    rels  = _zip_get(zf, "visio/pages/_rels/pages.xml.rels")
    if pages is not None and rels is not None:
        try:
            proot = ET.fromstring(pages)
            rid = None
            page_el = _first(proot, "Page") or next(_iter_local(proot, "Page"), None)
            if page_el is not None:
                rel_el = _first(page_el, "Rel")
                if rel_el is not None:
                    # r:id attribute (any namespace)
                    for k, v in rel_el.attrib.items():
                        if _local(k) == "id":
                            rid = v
                            break
            if rid:
                rroot = ET.fromstring(rels)
                for r in _iter_local(rroot, "Relationship"):
                    if r.get("Id") == rid:
                        target = r.get("Target") or ""
                        target = target.lstrip("/")
                        if not target.startswith("visio/"):
                            target = "visio/pages/" + target.split("/")[-1]
                        data = _zip_get(zf, target)
                        if data is not None:
                            return data
        except ET.ParseError:
            pass
    # Fallback: lowest-numbered page*.xml under visio/pages/
    page_files = sorted(
        n for n in names
        if re.match(r"visio/pages/page\d+\.xml$", n, re.IGNORECASE)
    )
    if page_files:
        return zf.read(page_files[0])
    raise VisioConversionError("No drawing page was found inside the Visio file.")


def _masters(zf: zipfile.ZipFile) -> Dict[str, str]:
    """master ID -> stencil name (NameU or Name), lower-cased."""
    out: Dict[str, str] = {}
    data = _zip_get(zf, "visio/masters/masters.xml")
    if data is None:
        return out
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return out
    for m in _iter_local(root, "Master"):
        mid = m.get("ID")
        nm = (m.get("NameU") or m.get("Name") or "").strip().lower()
        if mid is not None:
            out[mid] = nm
    return out


def _n_pages(zf: zipfile.ZipFile) -> int:
    data = _zip_get(zf, "visio/pages/pages.xml")
    if data is None:
        return 1
    try:
        return max(1, sum(1 for _ in _iter_local(ET.fromstring(data), "Page")))
    except ET.ParseError:
        return 1


# ---------------------------------------------------------------------------
# Parse one page -> shapes + edges
# ---------------------------------------------------------------------------
def _parse_page(page_bytes: bytes, masters: Dict[str, str]
                ) -> Tuple[List[VShape], List[VEdge]]:
    root = ET.fromstring(page_bytes)

    shapes: Dict[str, VShape] = {}
    connector_ids: set = set()
    formula_pairs: Dict[str, Dict[str, str]] = {}   # connector vid -> {begin,end}

    def visit(shape_el):
        vid = shape_el.get("ID")
        if vid is None:
            return
        master = ""
        mref = shape_el.get("Master")
        if mref is not None:
            master = masters.get(mref, "")
        if not master:
            master = (shape_el.get("NameU") or shape_el.get("Name") or "").strip().lower()
        stype = shape_el.get("Type") or "Shape"
        cells = _cells(shape_el)

        w = _fnum(cells, "Width"); h = _fnum(cells, "Height")
        pinx = _fnum(cells, "PinX"); piny = _fnum(cells, "PinY")
        locx = _fnum(cells, "LocPinX", w / 2.0); locy = _fnum(cells, "LocPinY", h / 2.0)
        left = pinx - locx; bottom = piny - locy

        has_begin = "BeginX" in cells or "BeginY" in cells
        has_end   = "EndX" in cells or "EndY" in cells
        is_conn = (any(k in master for k in _CONNECTOR_KW) or (has_begin and has_end))

        vs = VShape(
            vid=vid, master=master, stype=stype, text=_text_of(shape_el),
            cx=left + w / 2.0, cy=bottom + h / 2.0, w=w, h=h,
            left=left, bottom=bottom, right=left + w, top=bottom + h,
            is_connector=is_conn,
        )
        shapes[vid] = vs
        if is_conn:
            connector_ids.add(vid)
            # formula-glue fallback (used only if no <Connect> rows exist)
            beg = _sheet_ref(cells.get("BeginX", "")) or _sheet_ref(cells.get("BeginY", ""))
            end = _sheet_ref(cells.get("EndX", "")) or _sheet_ref(cells.get("EndY", ""))
            if beg or end:
                formula_pairs[vid] = {"begin": beg, "end": end}

        # recurse into child shapes (groups / swimlane containers), UNLESS this
        # shape is itself a connector (its inner geometry is not a node)
        if not is_conn:
            sub = _first(shape_el, "Shapes")
            if sub is not None:
                for child in _kids(sub, "Shape"):
                    visit(child)

    top_shapes = _first(root, "Shapes")
    if top_shapes is not None:
        for s in _kids(top_shapes, "Shape"):
            visit(s)

    # --- connectors -> edges (canonical <Connect> first) ---
    conn_ends: Dict[str, Dict[str, str]] = {}
    connects = _first(root, "Connects")
    if connects is not None:
        for c in _kids(connects, "Connect"):
            conn = c.get("FromSheet")          # the connector shape
            endpoint = c.get("ToSheet")        # the node shape
            cell = (c.get("FromCell") or "")
            if not conn or not endpoint:
                continue
            role = "begin" if cell.startswith("Begin") else ("end" if cell.startswith("End") else None)
            if role:
                conn_ends.setdefault(conn, {})[role] = endpoint
                connector_ids.add(conn)

    edges: List[VEdge] = []
    seen_conn: set = set()
    for conn, ends in conn_ends.items():
        b, e = ends.get("begin"), ends.get("end")
        seen_conn.add(conn)
        if b and e and b != e and b in shapes and e in shapes:
            edges.append(VEdge(b, e))
    # formula fallback for connectors with no <Connect> row
    for conn, ends in formula_pairs.items():
        if conn in seen_conn:
            continue
        b, e = ends.get("begin"), ends.get("end")
        if b and e and b != e and b in shapes and e in shapes:
            edges.append(VEdge(b, e))

    return list(shapes.values()), edges


def _sheet_ref(formula: str) -> str:
    """Extract the partner shape id from a Begin/End glue formula like
    '...PNT(Sheet.712!Connections.X1,...)' or 'GUARD(...Sheet.5!...)'."""
    if not formula:
        return ""
    m = re.search(r"Sheet\.?(\d+)", formula)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Shape -> BPMN tag mapping
# ---------------------------------------------------------------------------
def _is_lane(vs: VShape) -> bool:
    return any(k in vs.master for k in _LANE_KW)


def _is_container(vs: VShape) -> bool:
    return any(k in vs.master for k in _CONTAINER_KW)


def _is_annotation(vs: VShape) -> bool:
    return any(k in vs.master for k in _ANNOTATION_KW)


def _map_tag(vs: VShape, indeg: int, outdeg: int) -> Optional[str]:
    """Resolve a Visio shape to a BPMN flow-node/data tag, or None to drop."""
    m = vs.master

    # ---- BPMN-stencil masters (preferred) + flowchart heuristics ----
    if ("data object" in m or "data input" in m or "data output" in m
            or "data store" in m or "document" in m or "multi-document" in m
            or "stored data" in m or "database" in m
            or (m == "data") or m.endswith(" data")):
        return "dataObjectReference"

    if "gateway" in m or "decision" in m or "diamond" in m:
        if "parallel" in m or "fork" in m or "join" in m or m.strip() == "and":
            return "parallelGateway"
        if "inclusive" in m or m.strip() == "or":
            return "inclusiveGateway"
        if "event" in m and "based" in m:
            return "eventBasedGateway"
        return "exclusiveGateway"

    if "intermediate" in m and ("catch" in m or "receive" in m):
        return "intermediateCatchEvent"
    if "intermediate" in m and ("throw" in m or "send" in m):
        return "intermediateThrowEvent"
    if "intermediate" in m:
        return "intermediateThrowEvent"

    is_event_like = (
        "start event" in m or "end event" in m or "terminator" in m
        or "start/end" in m or "start" == m or "end" == m
        or m in ("oval", "ellipse", "rounded rectangle", "circle")
        or "begin" in m or "terminate" in m
    )
    if "start event" in m or (is_event_like and indeg == 0 and outdeg > 0):
        return "startEvent"
    if "end event" in m or (is_event_like and outdeg == 0 and indeg > 0):
        return "endEvent"
    if is_event_like and indeg > 0 and outdeg > 0:
        return "intermediateThrowEvent"

    if ("sub-process" in m or "subprocess" in m or "sub process" in m
            or "predefined process" in m):
        return "task"   # MVP: emit sub-processes as tasks
    if ("task" in m or "activity" in m or "process" in m or "rectangle" in m
            or "operation" in m or "step" in m or m == "box"):
        return "task"

    # ---- fallback: a shape with a label and at least one connection is a task ----
    if vs.text and (indeg + outdeg) > 0:
        return "task"
    return None


# ---------------------------------------------------------------------------
# Lane assignment
# ---------------------------------------------------------------------------
def _assign_lanes(node_shapes: List[VShape], lanes: List[VLane]) -> Dict[str, str]:
    """vid -> lane name. Horizontal swimlanes: assign by vertical containment."""
    out: Dict[str, str] = {}
    if not lanes:
        return out
    for vs in node_shapes:
        inside = [ln for ln in lanes if ln.bottom <= vs.cy <= ln.top]
        if inside:
            # tightest band wins
            inside.sort(key=lambda ln: ln.top - ln.bottom)
            out[vs.vid] = inside[0].name
        else:
            # nearest lane centre
            nearest = min(lanes, key=lambda ln: abs(ln.cy - vs.cy))
            out[vs.vid] = nearest.name
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def build_bpmn_from_visio(file_path: str, process_name: str = "") -> str:
    """Convert a .vsdx/.vsd Visio diagram to Signavio-ready BPMN 2.0 XML."""
    ext = os.path.splitext(file_path)[1].lower()
    tmp_to_delete: Optional[str] = None
    try:
        if ext == ".vsd":
            vsdx_path = _vsd_to_vsdx(file_path)
            tmp_to_delete = vsdx_path
        elif ext == ".vsdx":
            vsdx_path = file_path
        else:
            raise VisioConversionError(
                f"Unsupported Visio file type '{ext}'. Please upload a .vsdx or .vsd file.")

        try:
            zf = zipfile.ZipFile(vsdx_path)
        except zipfile.BadZipFile:
            raise VisioConversionError(
                "This file is not a valid .vsdx (it may be an old binary .vsd "
                "saved with a .vsdx extension). Open it in Visio and use "
                "File > Save As > Visio Drawing (.vsdx).")
        with zf:
            n_pages = _n_pages(zf)
            masters = _masters(zf)
            page_bytes = _first_page_xml(zf)

        shapes, edges = _parse_page(page_bytes, masters)
    except ET.ParseError as exc:
        raise VisioConversionError(
            f"The Visio drawing's XML could not be parsed ({exc}). The file may "
            "be corrupt or from an unsupported Visio version.")
    finally:
        if tmp_to_delete and os.path.exists(tmp_to_delete):
            try:
                os.remove(tmp_to_delete)
            except OSError:
                pass

    # ---- lanes ----
    lanes = [
        VLane(name=(s.text or f"Lane {i + 1}"), cy=s.cy, bottom=s.bottom, top=s.top)
        for i, s in enumerate(sh for sh in shapes if _is_lane(sh))
    ]
    lanes.sort(key=lambda ln: ln.cy, reverse=True)   # top of page first (Y is up)

    # ---- candidate node shapes (not connectors / lanes / containers / annotations) ----
    candidates = [
        s for s in shapes
        if not s.is_connector and not _is_lane(s)
        and not _is_container(s) and not _is_annotation(s)
        and s.stype not in ("Foreign", "Guide")
    ]
    cand_ids = {s.vid for s in candidates}

    # degrees from raw edges (only between candidate shapes)
    indeg: Dict[str, int] = {s.vid: 0 for s in candidates}
    outdeg: Dict[str, int] = {s.vid: 0 for s in candidates}
    for e in edges:
        if e.src in cand_ids and e.tgt in cand_ids:
            outdeg[e.src] += 1
            indeg[e.tgt] += 1

    # ---- classify -> nodes / data objects, assign ids ----
    ctr = {"task": 0, "gw": 0, "start": 0, "end": 0, "ev": 0, "dor": 0}
    vid_to_id: Dict[str, str] = {}
    nodes: List[VNode] = []
    data_objs: Dict[str, VData] = {}        # vid -> VData
    node_shape: Dict[str, VShape] = {}

    for s in candidates:
        tag = _map_tag(s, indeg[s.vid], outdeg[s.vid])
        if tag is None:
            continue
        if tag == "dataObjectReference":
            ctr["dor"] += 1
            d = VData(f"dor_{ctr['dor']}", f"do_{ctr['dor']}", s.text or f"Data {ctr['dor']}")
            data_objs[s.vid] = d
            vid_to_id[s.vid] = d.dor_id
            node_shape[s.vid] = s
            continue
        if tag == "task":
            ctr["task"] += 1; nid = f"task_{ctr['task']}"
        elif tag in _GW_TAGS:
            ctr["gw"] += 1; nid = f"Gateway_{ctr['gw']}"
        elif tag == "startEvent":
            ctr["start"] += 1; nid = f"startEvent_{ctr['start']}"
        elif tag == "endEvent":
            ctr["end"] += 1; nid = f"endEvent_{ctr['end']}"
        else:
            ctr["ev"] += 1; nid = f"{tag}_{ctr['ev']}"
        vid_to_id[s.vid] = nid
        nodes.append(VNode(id=nid, tag=tag, name=s.text))
        node_shape[s.vid] = s

    if not nodes:
        raise VisioConversionError(
            "No process activities were recognised in this Visio diagram. "
            "It may not be a process/flowchart, or its shapes are not connected.")

    # ---- lane assignment (now we know which shapes are real nodes) ----
    vid_lane = _assign_lanes([node_shape[v] for v in node_shape], lanes)
    node_by_id = {n.id: n for n in nodes}
    for vid, nid in vid_to_id.items():
        if nid in node_by_id:
            node_by_id[nid].lane = vid_lane.get(vid, "")

    # ---- split edges into sequence flows vs data associations ----
    seq_edges: List[Tuple[str, str]] = []          # (src_id, tgt_id)
    data_assocs: List[Tuple[str, str, str]] = []   # (kind, task_id, dor_id)
    for e in edges:
        if e.src not in vid_to_id or e.tgt not in vid_to_id:
            continue
        sid, tid = vid_to_id[e.src], vid_to_id[e.tgt]
        s_is_data = e.src in data_objs
        t_is_data = e.tgt in data_objs
        if s_is_data and not t_is_data:
            data_assocs.append(("input", tid, sid))     # data -> task
        elif t_is_data and not s_is_data:
            data_assocs.append(("output", sid, tid))     # task -> data
        elif s_is_data and t_is_data:
            continue                                      # data->data: ignore
        else:
            seq_edges.append((sid, tid))

    # ---- ensure start / end events ----
    _ensure_start_end(nodes, seq_edges)

    # ---- synthesize a single lane if the Visio had no swimlanes ----
    lane_order = [ln.name for ln in lanes]
    if not lane_order:
        only = (process_name or "Process")
        lane_order = [only]
        for n in nodes:
            n.lane = only
    else:
        # any unassigned node -> first lane
        for n in nodes:
            if not n.lane:
                n.lane = lane_order[0]

    pname = (process_name or "").strip() or "Visio Process"
    xml = _emit_bpmn(pname, nodes, seq_edges, list(data_objs.values()),
                     data_assocs, lane_order)

    # Signavio-normalise (fabricates all DI shapes/edges, fixes namespaces).
    try:
        from .signavio_normalize import normalize_for_signavio
        xml = normalize_for_signavio(xml)
    except Exception as exc:   # pragma: no cover - normalize is robust
        print(f"[visio] normalize skipped: {exc}", file=sys.stderr)

    if n_pages > 1:
        print(f"[visio] note: file has {n_pages} pages; converted the first page only.",
              file=sys.stderr)
    return xml


# ---------------------------------------------------------------------------
# Start/end injection
# ---------------------------------------------------------------------------
def _ensure_start_end(nodes: List[VNode], edges: List[Tuple[str, str]]) -> None:
    ids = {n.id for n in nodes}
    indeg = {i: 0 for i in ids}
    outdeg = {i: 0 for i in ids}
    for s, t in edges:
        if s in outdeg:
            outdeg[s] += 1
        if t in indeg:
            indeg[t] += 1

    has_start = any(n.tag == "startEvent" for n in nodes)
    has_end   = any(n.tag == "endEvent" for n in nodes)
    flow_ids = [n.id for n in nodes if n.tag in _FLOW_TAGS and n.tag not in ("startEvent", "endEvent")]
    by_id = {n.id: n for n in nodes}

    if not has_start and flow_ids:
        sources = [i for i in flow_ids if indeg.get(i, 0) == 0]
        if not sources:
            sources = [min(flow_ids, key=lambda i: indeg.get(i, 0))]
        se = VNode(id="startEvent_1", tag="startEvent", name="Start",
                   lane=by_id[sources[0]].lane)
        nodes.append(se)
        for tgt in sources:
            edges.append((se.id, tgt))

    if not has_end and flow_ids:
        sinks = [i for i in flow_ids if outdeg.get(i, 0) == 0]
        if not sinks:
            sinks = [min(flow_ids, key=lambda i: outdeg.get(i, 0))]
        ee = VNode(id="endEvent_1", tag="endEvent", name="End",
                   lane=by_id[sinks[0]].lane)
        nodes.append(ee)
        for src in sinks:
            edges.append((src, ee.id))


# ---------------------------------------------------------------------------
# BPMN emission (mirrors excel_to_bpmn.py structure; empty plane)
# ---------------------------------------------------------------------------
def _emit_bpmn(process_name: str, nodes: List[VNode],
               edges: List[Tuple[str, str]], data_objs: List[VData],
               data_assocs: List[Tuple[str, str, str]],
               lane_order: List[str]) -> str:
    ET.register_namespace("bpmn",   BPMN)
    ET.register_namespace("bpmndi", BPMNDI)
    ET.register_namespace("dc",     DC)
    ET.register_namespace("di",     DI)
    ET.register_namespace("xsi",    XSI)

    defs = ET.Element(f"{{{BPMN}}}definitions", {
        "id": "Definitions_1",
        "targetNamespace": "http://diagramiq",
        f"{{{XSI}}}schemaLocation": (
            "http://www.omg.org/spec/BPMN/20100524/MODEL "
            "http://www.omg.org/spec/BPMN/2.0/20100501/BPMN20.xsd"
        ),
    })
    process = ET.SubElement(defs, f"{{{BPMN}}}process", {
        "id": "Process_1", "name": process_name, "isExecutable": "false",
    })

    # lane set
    if lane_order:
        lane_set = ET.SubElement(process, f"{{{BPMN}}}laneSet", {"id": "LaneSet_1"})
        for i, lane_name in enumerate(lane_order):
            lane_el = ET.SubElement(lane_set, f"{{{BPMN}}}lane",
                                    {"id": f"Lane_{i + 1}", "name": lane_name})
            for n in nodes:
                if n.lane == lane_name:
                    ET.SubElement(lane_el, f"{{{BPMN}}}flowNodeRef").text = n.id

    # incoming / outgoing maps (flow ids)
    flow_ids: List[Tuple[str, str, str]] = []   # (flow_id, src, tgt)
    incoming: Dict[str, List[str]] = {n.id: [] for n in nodes}
    outgoing: Dict[str, List[str]] = {n.id: [] for n in nodes}
    for i, (s, t) in enumerate(edges, start=1):
        fid = f"flow_{i}"
        flow_ids.append((fid, s, t))
        if s in outgoing:
            outgoing[s].append(fid)
        if t in incoming:
            incoming[t].append(fid)

    # flow nodes
    for n in nodes:
        elem = ET.SubElement(process, f"{{{BPMN}}}{n.tag}", {"id": n.id, "name": n.name})
        for fid in incoming.get(n.id, []):
            ET.SubElement(elem, f"{{{BPMN}}}incoming").text = fid
        for fid in outgoing.get(n.id, []):
            ET.SubElement(elem, f"{{{BPMN}}}outgoing").text = fid

    # sequence flows
    for fid, s, t in flow_ids:
        ET.SubElement(process, f"{{{BPMN}}}sequenceFlow",
                      {"id": fid, "sourceRef": s, "targetRef": t})

    # data objects + associations
    assoc_by_task: Dict[str, List[Tuple[str, str]]] = {}
    for kind, task_id, dor_id in data_assocs:
        assoc_by_task.setdefault(task_id, []).append((kind, dor_id))
    for d in data_objs:
        ET.SubElement(process, f"{{{BPMN}}}dataObjectReference",
                      {"id": d.dor_id, "name": d.name, "dataObjectRef": d.do_id})
        ET.SubElement(process, f"{{{BPMN}}}dataObject", {"id": d.do_id})
    for kind, task_id, dor_id in data_assocs:
        if kind == "input":
            a = ET.SubElement(process, f"{{{BPMN}}}dataInputAssociation",
                              {"id": f"dia_{dor_id}"})
            ET.SubElement(a, f"{{{BPMN}}}sourceRef").text = dor_id
            ET.SubElement(a, f"{{{BPMN}}}targetRef").text = task_id
        else:
            a = ET.SubElement(process, f"{{{BPMN}}}dataOutputAssociation",
                              {"id": f"doa_{dor_id}"})
            ET.SubElement(a, f"{{{BPMN}}}sourceRef").text = task_id
            ET.SubElement(a, f"{{{BPMN}}}targetRef").text = dor_id

    # empty diagram plane - normalize_for_signavio fabricates all shapes/edges
    diagram = ET.SubElement(defs, f"{{{BPMNDI}}}BPMNDiagram", {"id": "BPMNDiagram_1"})
    ET.SubElement(diagram, f"{{{BPMNDI}}}BPMNPlane",
                  {"id": "BPMNPlane_1", "bpmnElement": "Process_1"})

    ET.indent(defs, space="  ")
    body = ET.tostring(defs, encoding="unicode", xml_declaration=False)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body


# ---------------------------------------------------------------------------
# .vsd -> .vsdx conversion (legacy binary format)
# ---------------------------------------------------------------------------
def _vsd_to_vsdx(path: str) -> str:
    """Convert a legacy binary .vsd to a temp .vsdx and return its path."""
    try:
        return _vsd_to_vsdx_via_powershell(path)
    except VisioConversionError as com_err:
        lo = _vsd_to_vsdx_via_libreoffice(path)
        if lo:
            return lo
        raise com_err


_VSD_NEEDS_VISIO = (
    "Microsoft Visio is required to read legacy .vsd files. Open the file in "
    "Visio and use File > Save As > Visio Drawing (.vsdx), then upload the "
    ".vsdx.")


def _vsd_to_vsdx_via_powershell(path: str) -> str:
    """Convert .vsd -> temp .vsdx by driving Visio's COM API from PowerShell.

    Uses PowerShell (built into Windows) rather than a Python COM binding, so
    the app needs NO extra dependency; it works whenever Microsoft Visio is
    installed on the user's machine, and raises a clear message otherwise.
    """
    import shutil
    import subprocess
    import tempfile
    pwsh = shutil.which("powershell") or shutil.which("pwsh")
    if not pwsh:
        raise VisioConversionError(_VSD_NEEDS_VISIO)
    tmp = os.path.join(tempfile.gettempdir(),
                       f"diagramiq_visio_{os.getpid()}_{abs(hash(path)) % 100000}.vsdx")
    src = os.path.abspath(path).replace("'", "''")
    dst = tmp.replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop';"
        "try { $v = New-Object -ComObject Visio.Application } "
        "catch { Write-Output 'VISIO_NOT_AVAILABLE'; exit 2 };"
        "try { $v.Visible = $false } catch {};"
        f"try {{ $d = $v.Documents.Open('{src}'); $d.SaveAs('{dst}'); $d.Close() }} "
        "finally { $v.Quit() }; exit 0"
    )
    no_window = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW
    try:
        r = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=180, creationflags=no_window,
        )
    except Exception as exc:
        raise VisioConversionError(
            f"Could not run the .vsd conversion ({exc}). Open the file in Visio "
            "and Save As .vsdx, then upload the .vsdx.")
    if "VISIO_NOT_AVAILABLE" in (r.stdout or "") or r.returncode == 2:
        raise VisioConversionError(_VSD_NEEDS_VISIO)
    if r.returncode != 0 or not os.path.exists(tmp):
        raise VisioConversionError(
            "Microsoft Visio could not convert this .vsd. Open it in Visio and "
            "Save As .vsdx, then upload the .vsdx.")
    return tmp


def _vsd_to_vsdx_via_libreoffice(path: str) -> Optional[str]:
    import shutil
    import subprocess
    import tempfile
    import glob
    soffice = shutil.which("soffice") or shutil.which("soffice.exe")
    if not soffice:
        return None
    outdir = tempfile.mkdtemp(prefix="diagramiq_visio_")
    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "vsdx", "--outdir",
             outdir, os.path.abspath(path)],
            timeout=120, check=True, capture_output=True,
        )
        outs = glob.glob(os.path.join(outdir, "*.vsdx"))
        return outs[0] if outs else None
    except Exception:
        return None
