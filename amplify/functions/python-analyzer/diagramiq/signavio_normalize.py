"""Signavio-compatible BPMN 2.0 normaliser.

v1.2.3 - the orphan-strip and namespace-prefix fix were not enough; Signavio
also rejects BPMN files that have ANY of the following structural issues:

  1. AI prose mixed in with the XML  ("Here's the uplifted BPMN: <?xml ...")
  2. Missing `targetNamespace` on <bpmn:definitions>
  3. Missing `id` on <bpmn:definitions>
  4. Missing typeLanguage / expressionLanguage on <bpmn:definitions>
  5. <bpmndi:BPMNDiagram> section absent or empty
  6. <bpmndi:BPMNPlane> bpmnElement doesn't reference an existing process
  7. <bpmndi:BPMNShape> entries pointing at IDs that don't exist
  8. <bpmndi:BPMNEdge> entries with no <di:waypoint> children
  9. Process flow nodes with NO matching BPMNShape (Signavio refuses these)
 10. SequenceFlows with NO matching BPMNEdge
 11. Encoding declaration in lowercase  ("utf-8" instead of "UTF-8")
 12. UTF-8 BOM at the file start
 13. Missing default xmlns or required namespace prefixes
 14. Trailing whitespace / explanatory text after </bpmn:definitions>

This module's `normalize_for_signavio(xml)` runs an aggressive normalisation
pass that fixes all 14 issues. It's idempotent and safe to run on already-
clean files.

Public API:
    normalize_for_signavio(xml: str) -> str
"""
from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from typing import Dict, List, Set, Tuple

BPMN_NS    = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMN_DI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DC_NS      = "http://www.omg.org/spec/DD/20100524/DC"
DI_NS      = "http://www.omg.org/spec/DD/20100524/DI"

DEFAULT_TARGET_NS = "http://bpmn.io/schema/bpmn"

# Tags that must appear on the diagram as a BPMNShape if they appear
# inside a process. Sequence flows get BPMNEdge instead.
_SHAPED_TAGS = {
    "task", "userTask", "serviceTask", "scriptTask", "manualTask",
    "sendTask", "receiveTask", "businessRuleTask",
    "callActivity", "subProcess",
    "exclusiveGateway", "parallelGateway", "inclusiveGateway",
    "eventBasedGateway", "complexGateway",
    "startEvent", "endEvent",
    "intermediateThrowEvent", "intermediateCatchEvent", "boundaryEvent",
    # NOTE: bare <bpmn:dataObject> / <bpmn:dataStore> are DATA DEFINITIONS
    # with NO visual - only their *Reference* counterparts get a BPMNShape.
    # Including "dataObject" here caused phantom empty boxes (one per
    # data object) to be generated. Fixed in v1.2.23.
    "dataObjectReference", "dataStoreReference",
    "textAnnotation",
    "lane", "participant",
}


# ---------------------------------------------------------------------------
# Step 1 - extract the XML region from raw AI output (strip prose / fences)
# ---------------------------------------------------------------------------

def _extract_xml_block(text: str) -> str:
    """Find the first <?xml or <definitions and the last </definitions>."""
    if not text:
        return text
    text = text.strip()
    # Strip any leading UTF-8 BOM
    if text.startswith("﻿"):
        text = text[1:]

    # Strip Markdown fences in any common form (```xml, ```bpmn, ```)
    text = re.sub(r"^```[a-zA-Z]*\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)

    # Find the start: <?xml > <bpmn:definitions > <definitions
    starts = []
    for needle in ("<?xml", "<bpmn:definitions", "<definitions"):
        idx = text.find(needle)
        if idx >= 0:
            starts.append(idx)
    if starts:
        text = text[min(starts):]

    # Find the end: last </bpmn:definitions> or </definitions>
    ends = []
    for needle in ("</bpmn:definitions>", "</definitions>"):
        idx = text.rfind(needle)
        if idx >= 0:
            ends.append(idx + len(needle))
    if ends:
        text = text[:max(ends)]

    return text.strip()


# ---------------------------------------------------------------------------
# Step 2 - register the canonical namespace prefixes
# ---------------------------------------------------------------------------

def _register_namespaces(xml: str) -> None:
    """Pre-register prefixes so ET.tostring keeps bpmn:/bpmndi:/dc:/di: not
    auto-generated ns0/ns1/etc."""
    if 'xmlns="' + BPMN_NS + '"' in xml:
        ET.register_namespace("", BPMN_NS)
    else:
        ET.register_namespace("bpmn", BPMN_NS)
    ET.register_namespace("bpmndi", BPMN_DI_NS)
    if 'xmlns:omgdc="' + DC_NS + '"' in xml:
        ET.register_namespace("omgdc", DC_NS)
        ET.register_namespace("omgdi", DI_NS)
    else:
        ET.register_namespace("dc", DC_NS)
        ET.register_namespace("di", DI_NS)
    ET.register_namespace("xsi",      "http://www.w3.org/2001/XMLSchema-instance")
    ET.register_namespace("signavio", "http://www.signavio.com")
    # v1.2.4 - Signavio's exporter declares i18n on the root and uses it
    # for translated labels in the body. ElementTree drops unregistered
    # namespace decls on round-trip, so any i18n:translation in the body
    # would reference an undefined prefix and Signavio's importer rejects
    # the whole file with "could not be processed as a BPMN 2.0 XML file".
    ET.register_namespace(
        "i18n",
        "http://www.omg.org/spec/BPMN/non-normative/extensions/i18n/1.0",
    )


# ---------------------------------------------------------------------------
# Step 3 - collect IDs from the process side and the diagram side
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _collect_process_ids(root: ET.Element) -> Tuple[Set[str], Set[str]]:
    """Returns (shape_ids, edge_ids) - ids of elements that need a
    BPMNShape vs a BPMNEdge respectively."""
    shape_ids: Set[str] = set()
    edge_ids:  Set[str] = set()
    # Connectors that need a BPMNEdge. Data associations (the dotted links
    # between a data object and its task) MUST be here - otherwise step 3a
    # drops their edge as an "orphan" and step 3c never recreates one, so the
    # data objects render floating/unconnected.
    _EDGE_TAGS = {"sequenceFlow", "messageFlow",
                  "dataInputAssociation", "dataOutputAssociation"}
    for elem in root.iter():
        local = _local(elem.tag)
        if local in _EDGE_TAGS:
            eid = elem.get("id")
            if eid:
                edge_ids.add(eid)
        elif local in _SHAPED_TAGS:
            eid = elem.get("id")
            if eid:
                shape_ids.add(eid)
    return shape_ids, edge_ids


def _collect_diagram_refs(root: ET.Element) -> Tuple[Set[str], Set[str]]:
    """Returns (shape_refs, edge_refs) - ids referenced by BPMNShape and
    BPMNEdge entries inside any BPMNDiagram."""
    shape_refs: Set[str] = set()
    edge_refs:  Set[str] = set()
    for shape in root.iter(f"{{{BPMN_DI_NS}}}BPMNShape"):
        ref = shape.get("bpmnElement")
        if ref:
            shape_refs.add(ref)
    for edge in root.iter(f"{{{BPMN_DI_NS}}}BPMNEdge"):
        ref = edge.get("bpmnElement")
        if ref:
            edge_refs.add(ref)
    return shape_refs, edge_refs


# ---------------------------------------------------------------------------
# Step 4 - the main public entry point
# ---------------------------------------------------------------------------

def normalize_for_signavio(xml: str) -> str:
    """Run the full normalisation chain. Always returns a string; on parse
    failure falls back to the input wrapped in a default <bpmn:definitions>.
    """
    xml = _extract_xml_block(xml)
    if not xml:
        return _empty_template()

    _register_namespaces(xml)

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        # Last-ditch: try to wrap in a fresh definitions and re-parse.
        wrapped = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<bpmn:definitions xmlns:bpmn="' + BPMN_NS + '" '
            'xmlns:bpmndi="' + BPMN_DI_NS + '" '
            'xmlns:dc="' + DC_NS + '" '
            'xmlns:di="' + DI_NS + '" '
            'targetNamespace="' + DEFAULT_TARGET_NS + '">\n'
            + xml
            + '\n</bpmn:definitions>'
        )
        try:
            root = ET.fromstring(wrapped)
        except ET.ParseError:
            return _empty_template()

    if _local(root.tag) != "definitions":
        # AI returned something other than <definitions> at root.
        return _empty_template()

    # ------- 1. Required attributes on <bpmn:definitions> ------------
    if not root.get("id"):
        root.set("id", "Definitions_" + uuid.uuid4().hex[:8])
    if not root.get("targetNamespace"):
        root.set("targetNamespace", DEFAULT_TARGET_NS)
    if not root.get("typeLanguage"):
        root.set("typeLanguage", "http://www.w3.org/2001/XMLSchema")
    if not root.get("expressionLanguage"):
        root.set("expressionLanguage", "http://www.w3.org/1999/XPath")

    # Make sure the namespace declarations we need are on the root, even if
    # ElementTree re-derives them - we'll re-emit them from string later.

    # ------- 2. Find / create the BPMNDiagram + BPMNPlane ---------
    diagram = root.find(f"{{{BPMN_DI_NS}}}BPMNDiagram")
    if diagram is None:
        diagram = ET.SubElement(root, f"{{{BPMN_DI_NS}}}BPMNDiagram", {
            "id": "BPMNDiagram_" + uuid.uuid4().hex[:8],
        })
    plane = diagram.find(f"{{{BPMN_DI_NS}}}BPMNPlane")
    process = root.find(f"{{{BPMN_NS}}}process")
    if process is None:
        # Try collaboration as the plane reference
        coll = root.find(f"{{{BPMN_NS}}}collaboration")
        plane_ref = coll.get("id") if coll is not None and coll.get("id") else "Process_default"
    else:
        if not process.get("id"):
            process.set("id", "Process_" + uuid.uuid4().hex[:8])
        if process.get("isExecutable") is None:
            process.set("isExecutable", "false")
        plane_ref = process.get("id")

    if plane is None:
        plane = ET.SubElement(diagram, f"{{{BPMN_DI_NS}}}BPMNPlane", {
            "id": "BPMNPlane_" + uuid.uuid4().hex[:8],
            "bpmnElement": plane_ref,
        })
    elif not plane.get("bpmnElement"):
        plane.set("bpmnElement", plane_ref)

    # ------- 3. Reconcile process IDs and diagram refs ---------
    shape_ids, edge_ids = _collect_process_ids(root)
    shape_refs, edge_refs = _collect_diagram_refs(root)

    # 3a. Drop BPMNShape / BPMNEdge that point at non-existent IDs (orphan DI)
    for parent in list(plane):
        kind = _local(parent.tag)
        ref = parent.get("bpmnElement")
        if not ref:
            plane.remove(parent)
            continue
        if kind == "BPMNShape" and ref not in shape_ids:
            plane.remove(parent)
        elif kind == "BPMNEdge" and ref not in edge_ids:
            plane.remove(parent)

    # 3b. Add a default BPMNShape for any process node lacking one.
    # Use a simple grid layout (x = column*180, y = row*120).
    existing_shape_refs = {s.get("bpmnElement") for s in plane.findall(f"{{{BPMN_DI_NS}}}BPMNShape")}
    missing_shapes = sorted(shape_ids - existing_shape_refs)
    for i, sid in enumerate(missing_shapes):
        x, y = 100 + (i % 6) * 180, 100 + (i // 6) * 120
        # Pick a default size based on what kind of element this is
        elem = _find_by_id(root, sid)
        local = _local(elem.tag) if elem is not None else "task"
        w, h = _default_size(local)
        shape = ET.SubElement(plane, f"{{{BPMN_DI_NS}}}BPMNShape", {
            "id": "Shape_" + sid,
            "bpmnElement": sid,
        })
        ET.SubElement(shape, f"{{{DC_NS}}}Bounds", {
            "x": str(x), "y": str(y), "width": str(w), "height": str(h),
        })

    # 3c. Add a default BPMNEdge for any sequenceFlow lacking one.
    existing_edge_refs = {e.get("bpmnElement") for e in plane.findall(f"{{{BPMN_DI_NS}}}BPMNEdge")}
    missing_edges = sorted(edge_ids - existing_edge_refs)
    for fid in missing_edges:
        edge = ET.SubElement(plane, f"{{{BPMN_DI_NS}}}BPMNEdge", {
            "id": "Edge_" + fid,
            "bpmnElement": fid,
        })
        # Two waypoints are the BPMN minimum
        ET.SubElement(edge, f"{{{DI_NS}}}waypoint", {"x": "0", "y": "0"})
        ET.SubElement(edge, f"{{{DI_NS}}}waypoint", {"x": "100", "y": "0"})

    # 3d. Ensure every existing BPMNEdge has at least 2 waypoints
    for edge in plane.findall(f"{{{BPMN_DI_NS}}}BPMNEdge"):
        wps = edge.findall(f"{{{DI_NS}}}waypoint")
        if len(wps) < 2:
            for w in wps:
                edge.remove(w)
            ET.SubElement(edge, f"{{{DI_NS}}}waypoint", {"x": "0", "y": "0"})
            ET.SubElement(edge, f"{{{DI_NS}}}waypoint", {"x": "100", "y": "0"})

    # ------- 4. Re-serialise ---------
    body = ET.tostring(root, encoding="unicode")

    # Normalise the XML declaration (Signavio prefers UTF-8 capitalised).
    declaration = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    if body.startswith("<?xml"):
        body = body.split("?>", 1)[1].lstrip()

    # v1.2.4 - ElementTree only emits namespace declarations for prefixes
    # that are actively USED by some element. Signavio-native files declare
    # `xmlns:i18n` (and a few others) on the root even when no element
    # currently uses them - the importer expects the decl to be there.
    # Force-inject any missing standard Signavio namespace declarations
    # onto the <definitions> opening tag.
    body = _ensure_root_namespaces(body)

    return declaration + "\n" + body + "\n"


_REQUIRED_ROOT_NAMESPACES = [
    ("bpmndi",   BPMN_DI_NS),
    ("omgdc",    DC_NS),       # Signavio uses omgdc/omgdi prefixes
    ("omgdi",    DI_NS),
    ("dc",       DC_NS),       # also accept the unprefixed pair
    ("di",       DI_NS),
    ("xsi",      "http://www.w3.org/2001/XMLSchema-instance"),
    ("signavio", "http://www.signavio.com"),
    ("i18n",     "http://www.omg.org/spec/BPMN/non-normative/extensions/i18n/1.0"),
]


def _ensure_root_namespaces(body: str) -> str:
    """Force any missing Signavio-expected xmlns declarations onto the
    <definitions> opening tag. ET drops unused namespace decls on
    serialisation; Signavio's importer expects them anyway."""
    m = re.search(r"<(?:bpmn:)?definitions\b[^>]*>", body, flags=re.DOTALL)
    if not m:
        return body
    open_tag = m.group(0)

    # Detect which prefix style the file uses for dc/di so we don't
    # double-inject both pairs. If the file already has dc:/di: present we
    # skip omgdc/omgdi, and vice versa.
    has_dc_pair    = ('xmlns:dc='    in open_tag)
    has_di_pair    = ('xmlns:di='    in open_tag)
    has_omgdc_pair = ('xmlns:omgdc=' in open_tag)
    has_omgdi_pair = ('xmlns:omgdi=' in open_tag)
    skip_omg     = has_dc_pair or has_di_pair
    skip_short   = has_omgdc_pair or has_omgdi_pair

    additions = []
    for prefix, uri in _REQUIRED_ROOT_NAMESPACES:
        if prefix in ("dc", "di") and skip_short:
            continue
        if prefix in ("omgdc", "omgdi") and skip_omg:
            continue
        if f'xmlns:{prefix}=' in open_tag:
            continue
        additions.append(f'xmlns:{prefix}="{uri}"')

    if not additions:
        return body

    # Inject right after the tag name. Find the first whitespace after
    # `<definitions` (or `<bpmn:definitions`) and splice in the additions.
    new_open = re.sub(
        r"^(<(?:bpmn:)?definitions)(\s|>)",
        lambda mm: mm.group(1) + " " + " ".join(additions) + mm.group(2),
        open_tag,
        count=1,
    )
    return body.replace(open_tag, new_open, 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_by_id(root: ET.Element, eid: str):
    for elem in root.iter():
        if elem.get("id") == eid:
            return elem
    return None


def _default_size(local: str) -> Tuple[int, int]:
    if local in ("startEvent", "endEvent",
                 "intermediateThrowEvent", "intermediateCatchEvent",
                 "boundaryEvent"):
        return 36, 36
    if local in ("exclusiveGateway", "parallelGateway",
                 "inclusiveGateway", "eventBasedGateway", "complexGateway"):
        return 50, 50
    if local in ("subProcess",):
        return 200, 120
    if local in ("textAnnotation",):
        return 100, 30
    if local in ("dataObject", "dataObjectReference", "dataStoreReference"):
        return 36, 50
    if local == "lane":
        return 600, 100
    if local == "participant":
        return 600, 200
    return 120, 80   # default = task


def _empty_template() -> str:
    proc_id = "Process_" + uuid.uuid4().hex[:8]
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<bpmn:definitions xmlns:bpmn="' + BPMN_NS + '" '
        'xmlns:bpmndi="' + BPMN_DI_NS + '" '
        'xmlns:dc="' + DC_NS + '" '
        'xmlns:di="' + DI_NS + '" '
        'id="Definitions_default" '
        'targetNamespace="' + DEFAULT_TARGET_NS + '" '
        'typeLanguage="http://www.w3.org/2001/XMLSchema" '
        'expressionLanguage="http://www.w3.org/1999/XPath">\n'
        '  <bpmn:process id="' + proc_id + '" isExecutable="false"/>\n'
        '  <bpmndi:BPMNDiagram id="BPMNDiagram_default">\n'
        '    <bpmndi:BPMNPlane id="BPMNPlane_default" '
        'bpmnElement="' + proc_id + '"/>\n'
        '  </bpmndi:BPMNDiagram>\n'
        '</bpmn:definitions>\n'
    )


# ---------------------------------------------------------------------------
# Celonis Process Repository ("CPM") compatibility
# ---------------------------------------------------------------------------
#
# The Celonis Process Repository accepts a `.bpmn` upload (its file picker even
# lists `.bpmn` in `accept`), but it PARSES the content and rejects with
# "This File type is not allowed" any BPMN that carries the **Signavio**
# extension namespace (http://www.signavio.com) or its i18n companion.
#
# DiagramIQ's Signavio normaliser deliberately injects those namespaces (plus
# `standalone="yes"`, `typeLanguage` and `expressionLanguage`) because Signavio
# itself wants them - so EVERY DiagramIQ file is refused by Celonis, while a
# clean BPMN 2.0 file (e.g. one Celonis exported itself) is accepted.
#
# `make_celonis_compatible` strips the Signavio-specific bits back down to the
# bpmn / bpmndi / dc / di / xsi namespaces - the exact shape Celonis exports
# and re-accepts. Signavio, Camunda Modeler and bpmn.io all still open the
# result, so it is a safe "lowest common denominator" export.

SIGNAVIO_NS = "http://www.signavio.com"
I18N_NS     = "http://www.omg.org/spec/BPMN/non-normative/extensions/i18n/1.0"
XSI_NS      = "http://www.w3.org/2001/XMLSchema-instance"


def make_celonis_compatible(xml: str) -> str:
    """Return BPMN 2.0 XML accepted by the Celonis Process Repository.

    Removes everything Celonis's BPMN parser refuses:
      * every ``<signavio:*>`` / ``<i18n:*>`` element (and any
        ``<bpmn:extensionElements>`` wrapper left empty afterwards),
      * every ``signavio:`` / ``i18n:`` attribute,
      * the ``xmlns:signavio`` / ``xmlns:i18n`` declarations,
      * ``typeLanguage`` / ``expressionLanguage`` on ``<bpmn:definitions>``,
      * ``standalone="yes"`` from the XML declaration.

    The structural BPMN (lanes, tasks, gateways, events, flows, DI) is left
    untouched. Idempotent and safe on already-clean files.
    """
    # Only the clean prefixes may be emitted on the way out.
    for prefix, uri in (("bpmn", BPMN_NS), ("bpmndi", BPMN_DI_NS),
                        ("dc", DC_NS), ("di", DI_NS), ("xsi", XSI_NS)):
        ET.register_namespace(prefix, uri)

    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except ET.ParseError:
        # Could not parse - best-effort string strip so we still help.
        return _celonis_regex_strip(xml)

    sig     = "{" + SIGNAVIO_NS + "}"
    i18     = "{" + I18N_NS + "}"
    ext_tag = "{" + BPMN_NS + "}extensionElements"

    def prune(el: ET.Element) -> None:
        for child in list(el):
            tag = child.tag
            if isinstance(tag, str) and (tag.startswith(sig) or tag.startswith(i18)):
                el.remove(child)
                continue
            prune(child)
            # Drop a now-empty <bpmn:extensionElements> shell.
            if child.tag == ext_tag and len(child) == 0 and not (child.text or "").strip():
                el.remove(child)

    prune(root)

    # Strip any leftover signavio:/i18n: attributes anywhere in the tree.
    for el in root.iter():
        for key in list(el.attrib):
            if key.startswith(sig) or key.startswith(i18):
                del el.attrib[key]

    # These two are Signavio-isms DiagramIQ adds; Celonis doesn't want them.
    for attr in ("typeLanguage", "expressionLanguage"):
        root.attrib.pop(attr, None)

    # A file that originated in Signavio also carries a Signavio fingerprint on
    # <definitions>: exporter / exporterVersion and a signavio.com
    # targetNamespace. Celonis treats these as "this is a Signavio file" too,
    # so scrub them back to a neutral DiagramIQ namespace.
    for attr in ("exporter", "exporterVersion"):
        root.attrib.pop(attr, None)
    tns = root.get("targetNamespace", "")
    if (not tns) or ("signavio.com" in tns):
        root.set("targetNamespace", "http://diagramiq")

    try:                       # tidy indentation (Python 3.9+)
        ET.indent(root, space="  ")
    except Exception:
        pass

    body = ET.tostring(root, encoding="unicode").strip()
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"


def _celonis_regex_strip(xml: str) -> str:
    """Fallback for un-parseable input: best-effort textual removal of the
    Signavio bits. Used only when ElementTree can't parse the XML."""
    out = re.sub(r'<\?xml[^>]*\?>',
                 '<?xml version="1.0" encoding="UTF-8"?>', xml, count=1)
    # Paired signavio/i18n elements (e.g. dictionaryLink/signavioDictionaryLink).
    out = re.sub(r'<(signavio|i18n):([A-Za-z]+)\b[^>]*>.*?</\1:\2>', '', out, flags=re.S)
    # Self-closing signavio/i18n elements.
    out = re.sub(r'<(signavio|i18n):[A-Za-z]+\b[^>]*/>', '', out)
    # signavio/i18n attributes, namespace decls, and the Signavio-only attrs.
    out = re.sub(r'\s+(?:signavio|i18n):[A-Za-z]+="[^"]*"', '', out)
    out = re.sub(r'\s+xmlns:(?:signavio|i18n)="[^"]*"', '', out)
    out = re.sub(r'\s+(?:typeLanguage|expressionLanguage)="[^"]*"', '', out)
    # Scrub Signavio exporter fingerprint and a signavio.com targetNamespace.
    out = re.sub(r'\s+exporter(?:Version)?="[^"]*"', '', out)
    out = re.sub(r'targetNamespace="https?://[^"]*signavio\.com[^"]*"',
                 'targetNamespace="http://diagramiq"', out)
    # Tidy empty extensionElements wrappers and blank lines.
    out = re.sub(r'<bpmn:extensionElements>\s*</bpmn:extensionElements>', '', out)
    out = re.sub(r'<bpmn:extensionElements\s*/>', '', out)
    out = re.sub(r'\n[ \t]*\n', '\n', out)
    return out


def strip_data_objects(xml: str) -> str:
    """Remove input/output **data objects** (the document boxes) and their
    associations + DI, leaving just lanes / tasks / gateways / events / flows.

    The Celonis Process Repository doesn't need DiagramIQ's input/output
    document objects, and they clutter the diagram. After this runs, re-grid
    with ``bpmn_layout_fixer.fix_layout(xml, compact=True)`` for a tight,
    data-object-free layout. Idempotent; safe on diagrams that have none.
    """
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except ET.ParseError:
        return xml

    data_tags = {"dataObjectReference", "dataObject",
                 "dataInputAssociation", "dataOutputAssociation"}

    def _loc(tag) -> str:
        return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""

    bpmn_prefix = "{" + BPMN_NS + "}"

    def _ids():
        return {el.get("id") for el in root.iter()
                if isinstance(el.tag, str) and el.tag.startswith(bpmn_prefix) and el.get("id")}

    # 1) Remove the data-object MODEL elements wherever they sit (process-level
    #    data objects + the in/out associations nested inside tasks).
    def prune(parent):
        for child in list(parent):
            if _loc(child.tag) in data_tags:
                parent.remove(child)
                continue
            prune(child)
    prune(root)

    # 2) DiagramIQ also wires a data object to its task with a plain
    #    <bpmn:association> (id like dia_*/doa_*). After the data objects are
    #    gone these dangle, so drop any association whose source/target no
    #    longer exists - otherwise its BPMNEdge renders as a stray arrow.
    valid = _ids()

    def prune_assoc(parent):
        for child in list(parent):
            if _loc(child.tag) == "association":
                s = child.get("sourceRef")
                t = child.get("targetRef")
                if (s and s not in valid) or (t and t not in valid):
                    parent.remove(child)
                    continue
            prune_assoc(child)
    prune_assoc(root)

    # 3) Drop now-orphaned DI: any BPMNShape/BPMNEdge whose bpmnElement no
    #    longer exists (the data-object shapes and the association edges).
    valid = _ids()
    for plane in root.iter("{" + BPMN_DI_NS + "}BPMNPlane"):
        for di in list(plane):
            be = di.get("bpmnElement")
            if be is not None and be not in valid:
                plane.remove(di)

    for prefix, uri in (("bpmn", BPMN_NS), ("bpmndi", BPMN_DI_NS), ("dc", DC_NS),
                        ("di", DI_NS), ("xsi", XSI_NS),
                        ("signavio", SIGNAVIO_NS), ("i18n", I18N_NS)):
        ET.register_namespace(prefix, uri)
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body
