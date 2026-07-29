"""Parse BPMN 2.0 XML files into an internal model for validation and analysis."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMN_DI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DC_NS = "http://www.omg.org/spec/DD/20100524/DC"

ACTIVITY_TAGS = {
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
}

GATEWAY_TAGS = {
    f"{{{BPMN_NS}}}exclusiveGateway",
    f"{{{BPMN_NS}}}parallelGateway",
    f"{{{BPMN_NS}}}inclusiveGateway",
    f"{{{BPMN_NS}}}eventBasedGateway",
    f"{{{BPMN_NS}}}complexGateway",
}

EVENT_TAGS = {
    f"{{{BPMN_NS}}}startEvent",
    f"{{{BPMN_NS}}}endEvent",
    f"{{{BPMN_NS}}}intermediateThrowEvent",
    f"{{{BPMN_NS}}}intermediateCatchEvent",
    f"{{{BPMN_NS}}}boundaryEvent",
}


@dataclass
class BPMNElement:
    id: str
    tag: str
    name: str
    kind: str  # "event", "task", "gateway", "flow", "message_flow", "lane", "pool", "other"
    process_id: Optional[str] = None
    incoming: List[str] = field(default_factory=list)
    outgoing: List[str] = field(default_factory=list)
    source_ref: Optional[str] = None
    target_ref: Optional[str] = None
    attached_to: Optional[str] = None
    # v1.2.1 - for kind=="lane" elements only: IDs of flow nodes contained in
    # this lane (parsed from <bpmn:flowNodeRef> children). Used by
    # bpmn_to_excel.py to populate the "Participant" column from swimlanes.
    flow_node_refs: List[str] = field(default_factory=list)
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0

    def tag_local(self) -> str:
        return self.tag.split("}")[-1] if "}" in self.tag else self.tag


@dataclass
class BPMNModel:
    elements: Dict[str, BPMNElement] = field(default_factory=dict)
    processes: List[str] = field(default_factory=list)
    collaborations: List[str] = field(default_factory=list)
    raw_xml: str = ""

    def get_by_kind(self, kind: str) -> List[BPMNElement]:
        return [e for e in self.elements.values() if e.kind == kind]

    def get_flows(self) -> List[BPMNElement]:
        return self.get_by_kind("flow")

    def get_events(self) -> List[BPMNElement]:
        return self.get_by_kind("event")

    def get_tasks(self) -> List[BPMNElement]:
        return self.get_by_kind("task")

    def get_gateways(self) -> List[BPMNElement]:
        return self.get_by_kind("gateway")

    def get_lanes(self) -> List[BPMNElement]:
        return self.get_by_kind("lane")

    def get_message_flows(self) -> List[BPMNElement]:
        return self.get_by_kind("message_flow")


def _kind_for_tag(tag: str) -> str:
    if tag in ACTIVITY_TAGS:
        return "task"
    if tag in GATEWAY_TAGS:
        return "gateway"
    if tag in EVENT_TAGS:
        return "event"
    if tag == f"{{{BPMN_NS}}}sequenceFlow":
        return "flow"
    if tag == f"{{{BPMN_NS}}}messageFlow":
        return "message_flow"
    if tag in (f"{{{BPMN_NS}}}lane",):
        return "lane"
    if tag in (f"{{{BPMN_NS}}}participant", f"{{{BPMN_NS}}}collaboration"):
        return "pool"
    return "other"


def parse_bpmn_xml(xml_content: str) -> BPMNModel:
    """Parse a BPMN 2.0 XML string into a BPMNModel."""
    model = BPMNModel(raw_xml=xml_content)

    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as e:
        raise ValueError(f"Invalid XML: {e}")

    # Extract DI shape positions
    shape_bounds: Dict[str, Tuple[float, float, float, float]] = {}
    for shape in root.iter(f"{{{BPMN_DI_NS}}}BPMNShape"):
        elem_id = shape.get("bpmnElement", "")
        bounds = shape.find(f"{{{DC_NS}}}Bounds")
        if bounds is not None and elem_id:
            shape_bounds[elem_id] = (
                float(bounds.get("x", 0)),
                float(bounds.get("y", 0)),
                float(bounds.get("width", 0)),
                float(bounds.get("height", 0)),
            )

    # Process elements inside each process
    for proc in root.iter(f"{{{BPMN_NS}}}process"):
        proc_id = proc.get("id", "")
        if proc_id:
            model.processes.append(proc_id)

        for child in proc.iter():
            elem_id = child.get("id")
            if not elem_id:
                continue

            tag = child.tag
            kind = _kind_for_tag(tag)
            elem = BPMNElement(
                id=elem_id,
                tag=tag,
                name=child.get("name", "") or "",
                kind=kind,
                process_id=proc_id,
            )

            elem.source_ref = child.get("sourceRef")
            elem.target_ref = child.get("targetRef")
            elem.attached_to = child.get("attachedToRef")

            for inc in child.findall(f"{{{BPMN_NS}}}incoming"):
                if inc.text:
                    elem.incoming.append(inc.text.strip())
            for out in child.findall(f"{{{BPMN_NS}}}outgoing"):
                if out.text:
                    elem.outgoing.append(out.text.strip())

            # v1.2.1 - capture lane membership for swimlane->participant mapping
            if kind == "lane":
                for ref in child.findall(f"{{{BPMN_NS}}}flowNodeRef"):
                    if ref.text:
                        elem.flow_node_refs.append(ref.text.strip())

            if elem_id in shape_bounds:
                elem.x, elem.y, elem.width, elem.height = shape_bounds[elem_id]

            model.elements[elem_id] = elem

    # v1.2.1 - many real-world BPMN exports (Signavio, Camunda, draw.io) omit
    # <bpmn:incoming>/<bpmn:outgoing> children on flow nodes and rely entirely
    # on sourceRef/targetRef attributes of <bpmn:sequenceFlow>. Backfill the
    # incoming/outgoing lists here so downstream connectedness checks work
    # regardless of which style the file uses.
    for flow in (e for e in model.elements.values() if e.kind == "flow"):
        if flow.source_ref and flow.source_ref in model.elements:
            src = model.elements[flow.source_ref]
            if flow.id not in src.outgoing:
                src.outgoing.append(flow.id)
        if flow.target_ref and flow.target_ref in model.elements:
            tgt = model.elements[flow.target_ref]
            if flow.id not in tgt.incoming:
                tgt.incoming.append(flow.id)

    # Process collaboration elements (message flows, pools)
    for collab in root.iter(f"{{{BPMN_NS}}}collaboration"):
        collab_id = collab.get("id", "")
        if collab_id:
            model.collaborations.append(collab_id)

        for child in collab:
            elem_id = child.get("id")
            if not elem_id:
                continue
            tag = child.tag
            kind = _kind_for_tag(tag)
            elem = BPMNElement(
                id=elem_id,
                tag=tag,
                name=child.get("name", "") or "",
                kind=kind,
            )
            elem.source_ref = child.get("sourceRef")
            elem.target_ref = child.get("targetRef")
            model.elements[elem_id] = elem

    return model
