"""Deterministic BPMN cleanup pass - runs before AI uplift.

v1.2.1 - removes orphan tasks (tasks/events/gateways with NO incoming AND
NO outgoing sequence flows). The AI uplift prompt also tells the model to
do this, but trusting AI for a deterministic structural fix is unreliable;
this module guarantees orphans are removed regardless of provider.

v1.2.2 - registers BPMN namespace prefixes BEFORE re-serialising so the
cleaned output keeps the canonical `bpmn:`, `bpmndi:`, `dc:`, `di:`
prefixes that Signavio requires. Without this, ElementTree assigns
auto-prefixes (ns0:, ns1:, ns2:, ns3:) and Signavio rejects the file
with "could not be processed as a BPMN 2.0 XML file".

Usage:
    from bpmn_cleanup import strip_orphans
    cleaned_xml, removed_count = strip_orphans(input_xml)
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Set, Tuple

BPMN_NS    = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMN_DI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DC_NS      = "http://www.omg.org/spec/DD/20100524/DC"
DI_NS      = "http://www.omg.org/spec/DD/20100524/DI"

# Tags that count as flow nodes (i.e. things that participate in sequence flow)
_FLOW_NODE_TAGS = {
    f"{{{BPMN_NS}}}task",
    f"{{{BPMN_NS}}}userTask",
    f"{{{BPMN_NS}}}serviceTask",
    f"{{{BPMN_NS}}}scriptTask",
    f"{{{BPMN_NS}}}manualTask",
    f"{{{BPMN_NS}}}sendTask",
    f"{{{BPMN_NS}}}receiveTask",
    f"{{{BPMN_NS}}}businessRuleTask",
    f"{{{BPMN_NS}}}callActivity",
    f"{{{BPMN_NS}}}subProcess",
    f"{{{BPMN_NS}}}exclusiveGateway",
    f"{{{BPMN_NS}}}parallelGateway",
    f"{{{BPMN_NS}}}inclusiveGateway",
    f"{{{BPMN_NS}}}eventBasedGateway",
    f"{{{BPMN_NS}}}complexGateway",
    f"{{{BPMN_NS}}}intermediateThrowEvent",
    f"{{{BPMN_NS}}}intermediateCatchEvent",
    # Note: startEvent and endEvent are intentionally excluded - they are
    # the natural "ends" of the flow and should not be classified as orphans
    # if they have a single connection (which a proper start/end always has).
}


def _register_bpmn_namespaces(xml_content: str) -> None:
    """v1.2.2 - tell ElementTree which prefix to use for each BPMN namespace,
    matching the style of the input file so the round-trip preserves
    Signavio-compatible prefixes (bpmn:, bpmndi:, dc:, di: instead of
    auto-generated ns0:, ns1:, ns2:, ns3:).
    """
    # The BPMN model namespace can be declared as the default xmlns="..."
    # (Signavio does this) or with a 'bpmn' prefix. Detect and preserve.
    if 'xmlns="' + BPMN_NS + '"' in xml_content:
        ET.register_namespace("", BPMN_NS)
    else:
        ET.register_namespace("bpmn", BPMN_NS)

    ET.register_namespace("bpmndi", BPMN_DI_NS)

    # Signavio uses 'omgdc' / 'omgdi' as alias prefixes. Other tools use
    # plain 'dc' / 'di'. Preserve whichever style the input used.
    if 'xmlns:omgdc="' + DC_NS + '"' in xml_content:
        ET.register_namespace("omgdc", DC_NS)
        ET.register_namespace("omgdi", DI_NS)
    else:
        ET.register_namespace("dc", DC_NS)
        ET.register_namespace("di", DI_NS)

    ET.register_namespace("xsi",      "http://www.w3.org/2001/XMLSchema-instance")
    ET.register_namespace("signavio", "http://www.signavio.com")
    # Signavio i18n extension - older Signavio exports include this
    ET.register_namespace(
        "i18n",
        "http://www.omg.org/spec/BPMN/non-normative/extensions/i18n/1.0",
    )


def strip_orphans(xml_content: str) -> Tuple[str, int]:
    """Remove orphan flow nodes from BPMN XML.

    A node is an orphan when it has NO referenced incoming sequence flow
    AND NO referenced outgoing sequence flow. Both <bpmn:incoming> children
    and <bpmn:sequenceFlow targetRef="..."> are considered.

    Returns:
        (cleaned_xml, removed_count)

    Side effects: also strips any orphan node's bpmndi:BPMNShape so the
    diagram doesn't reference deleted IDs.
    """
    # v1.2.2 - register namespace prefixes BEFORE parsing/serialising so
    # the round-trip keeps Signavio-compatible 'bpmn:' / 'bpmndi:' / 'dc:'
    # / 'di:' prefixes instead of auto-generated 'ns0:' / 'ns1:'.
    _register_bpmn_namespaces(xml_content)

    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        # Caller will see the original parse error from the regular pipeline.
        return xml_content, 0

    # 1) Build sets of every node ID that appears as a sourceRef OR targetRef
    referenced: Set[str] = set()
    for flow in root.iter(f"{{{BPMN_NS}}}sequenceFlow"):
        src = flow.get("sourceRef")
        tgt = flow.get("targetRef")
        if src:
            referenced.add(src)
        if tgt:
            referenced.add(tgt)
    # Also count <bpmn:incoming> / <bpmn:outgoing> children of nodes - some
    # exporters embed them as text instead of using sourceRef/targetRef.
    for elem in root.iter():
        for child_tag in (f"{{{BPMN_NS}}}incoming", f"{{{BPMN_NS}}}outgoing"):
            for ref in elem.findall(child_tag):
                if ref.text and ref.text.strip():
                    # The element here is connected, so add ITS id to referenced
                    if elem.get("id"):
                        referenced.add(elem.get("id") or "")

    # 2) Find every orphan flow node ID
    orphan_ids: Set[str] = set()
    for elem in root.iter():
        if elem.tag not in _FLOW_NODE_TAGS:
            continue
        elem_id = elem.get("id")
        if not elem_id:
            continue
        if elem_id not in referenced:
            orphan_ids.add(elem_id)

    if not orphan_ids:
        return xml_content, 0

    # 3) Walk parents in reverse so we can safely remove children. ET doesn't
    # give parent links, so we iterate parents explicitly.
    removed = 0
    for parent in root.iter():
        # Snapshot children to avoid mutation during iteration
        for child in list(parent):
            cid = child.get("id")
            if cid in orphan_ids:
                parent.remove(child)
                removed += 1

    # 4) Strip any leftover BPMNShape entries that referenced the orphan IDs
    for diagram in root.iter(f"{{{BPMN_DI_NS}}}BPMNDiagram"):
        for parent in diagram.iter():
            for child in list(parent):
                ref_id = child.get("bpmnElement")
                if ref_id and ref_id in orphan_ids:
                    parent.remove(child)
                    removed += 1

    # 5) Strip orphan IDs from <bpmn:flowNodeRef> entries inside lanes,
    # otherwise downstream tools see dangling references.
    for parent in root.iter():
        for child in list(parent):
            if child.tag == f"{{{BPMN_NS}}}flowNodeRef" and child.text:
                if child.text.strip() in orphan_ids:
                    parent.remove(child)

    cleaned = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
    return cleaned, len(orphan_ids)


# Convenience for callers that want a single number, not the tuple.
def count_orphans(xml_content: str) -> int:
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return 0
    referenced: Set[str] = set()
    for flow in root.iter(f"{{{BPMN_NS}}}sequenceFlow"):
        if flow.get("sourceRef"):
            referenced.add(flow.get("sourceRef") or "")
        if flow.get("targetRef"):
            referenced.add(flow.get("targetRef") or "")
    n = 0
    for elem in root.iter():
        if elem.tag in _FLOW_NODE_TAGS and elem.get("id") and elem.get("id") not in referenced:
            n += 1
    return n
