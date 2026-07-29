"""Advanced structural rules applied after the patcher (v1.2.7).

Three deterministic passes that fix common Signavio modelling-convention
violations the patcher itself can't detect:

  1. Gateway pairing            - a splitting gateway and its corresponding
                                   merging gateway must share the SAME type
                                   (XOR pair with XOR, AND pair with AND,
                                   OR pair with OR). v1.2.6 left mismatched
                                   pairs untouched.

  2. Verb-at-start enforcement  - every activity / call activity / sub-process
                                   name must start with a verb. Names where
                                   a verb appears later get reordered to
                                   verb-object form ("Order place" -> "Place
                                   order"). Names with no recognised verb
                                   are left alone (we don't invent words).

  3. Straighten sequence flows  - any <bpmndi:BPMNEdge> whose endpoints are
                                   roughly horizontally or vertically aligned
                                   gets its waypoints reduced to two: source
                                   centre, target centre. Removes zig-zags.

Each pass returns a change log so `uplift_report.py` can render an Excel
diff for the user.

Public API
----------
    apply_advanced_rules(xml: str) -> tuple[str, list[dict]]
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Set, Tuple

BPMN_NS    = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMN_DI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DC_NS      = "http://www.omg.org/spec/DD/20100524/DC"
DI_NS      = "http://www.omg.org/spec/DD/20100524/DI"

# Tags considered "activities" for the verb-at-start rule.
_ACTIVITY_TAGS = {
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

# All gateway tags share this suffix.
_GATEWAY_TAGS = {
    f"{{{BPMN_NS}}}exclusiveGateway",
    f"{{{BPMN_NS}}}parallelGateway",
    f"{{{BPMN_NS}}}inclusiveGateway",
    f"{{{BPMN_NS}}}eventBasedGateway",
    f"{{{BPMN_NS}}}complexGateway",
}


def apply_advanced_rules(xml: str) -> Tuple[str, List[Dict]]:
    """Run the three structural passes.

    Returns:
        (modified_xml, changes_list)
    """
    # Re-register namespaces so the round-trip preserves canonical prefixes.
    if 'xmlns="' + BPMN_NS + '"' in xml:
        ET.register_namespace("", BPMN_NS)
    else:
        ET.register_namespace("bpmn", BPMN_NS)
    ET.register_namespace("bpmndi", BPMN_DI_NS)
    if 'xmlns:omgdc="' in xml:
        ET.register_namespace("omgdc", DC_NS)
        ET.register_namespace("omgdi", DI_NS)
    else:
        ET.register_namespace("dc", DC_NS)
        ET.register_namespace("di", DI_NS)
    ET.register_namespace("xsi",      "http://www.w3.org/2001/XMLSchema-instance")
    ET.register_namespace("signavio", "http://www.signavio.com")
    ET.register_namespace(
        "i18n",
        "http://www.omg.org/spec/BPMN/non-normative/extensions/i18n/1.0",
    )

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return xml, []

    changes: List[Dict] = []

    # Pass 1 - gateway pairing
    changes.extend(_fix_gateway_pairs(root))

    # Pass 2 - verb at start
    changes.extend(_enforce_verb_at_start(root))

    # Pass 3 - straighten edges
    changes.extend(_straighten_edges(root))

    return ET.tostring(root, encoding="unicode"), changes


# ---------------------------------------------------------------------------
# Pass 1 - gateway pairing
# ---------------------------------------------------------------------------

def _fix_gateway_pairs(root: ET.Element) -> List[Dict]:
    """For every splitting gateway G with N>1 outgoing flows, BFS forward
    until a merging gateway M (N>1 incoming) is reached on every branch.
    If M's type differs from G's, change M's type to match G.

    Returns one change-log entry per gateway whose type was modified.
    """
    changes: List[Dict] = []

    elements = {e.get("id"): e for e in root.iter() if e.get("id")}
    flows    = list(root.iter(f"{{{BPMN_NS}}}sequenceFlow"))

    out_of: Dict[str, List[str]] = {}
    in_of:  Dict[str, List[str]] = {}
    for f in flows:
        s = f.get("sourceRef")
        t = f.get("targetRef")
        if s and t:
            out_of.setdefault(s, []).append(t)
            in_of.setdefault(t, []).append(s)

    splits = [
        e for e in root.iter()
        if e.tag in _GATEWAY_TAGS and len(out_of.get(e.get("id") or "", [])) >= 2
    ]

    for split in splits:
        sid = split.get("id") or ""
        # BFS forward looking for the convergence point.
        visited: Set[str] = set()
        frontier: List[str] = list(out_of.get(sid, []))
        merge_candidate: Optional[str] = None
        while frontier:
            nid = frontier.pop(0)
            if nid in visited:
                continue
            visited.add(nid)
            elem = elements.get(nid)
            if elem is None:
                continue
            if elem.tag in _GATEWAY_TAGS and len(in_of.get(nid, [])) >= 2:
                merge_candidate = nid
                break  # take the first convergence we hit
            frontier.extend(out_of.get(nid, []))

        if merge_candidate is None:
            continue

        merge = elements[merge_candidate]
        if merge.tag == split.tag:
            continue  # already paired correctly

        # Local tag names for a readable change log.
        split_local = _local(split.tag)
        merge_local = _local(merge.tag)
        old_tag = merge.tag
        merge.tag = split.tag  # re-tag the merge to match the split

        changes.append({
            "category": "Gateway pairing",
            "rule":     "G1 - matching split/merge gateway types",
            "element_id": merge_candidate,
            "before":   merge_local,
            "after":    split_local,
            "detail":   f"merge gateway changed from {merge_local} to "
                        f"{split_local} to match the upstream split "
                        f"gateway '{sid}'",
        })

    return changes


# ---------------------------------------------------------------------------
# Pass 2 - verb at start
# ---------------------------------------------------------------------------

def _enforce_verb_at_start(root: ET.Element) -> List[Dict]:
    """For each activity, if its name doesn't start with a recognised verb,
    try to reorder it to verb-object form via local_uplift._verb_object_fix.
    If no verb is recognised anywhere in the name, leave it alone (we don't
    invent words).

    Names already in good shape are no-ops; only actual changes are logged.
    """
    try:
        from local_uplift import _verb_object_fix, _BPMN_VERBS, _clean_name
    except ImportError:
        return []

    changes: List[Dict] = []
    for elem in root.iter():
        if elem.tag not in _ACTIVITY_TAGS:
            continue
        name = (elem.get("name") or "").strip()
        if not name:
            continue
        # Clean newlines and CamelCase joins first.
        cleaned = _clean_name(name)
        # Try to enforce verb-first form.
        new_name = _verb_object_fix(cleaned)
        if new_name == name:
            continue
        elem.set("name", new_name)
        changes.append({
            "category":   "Naming",
            "rule":       "N1 - activity must start with a verb",
            "element_id": elem.get("id") or "",
            "before":     name,
            "after":      new_name,
            "detail":     "reordered to verb-object form",
        })
    return changes


# ---------------------------------------------------------------------------
# Pass 3 - straighten edges
# ---------------------------------------------------------------------------

# v1.2.12 - orthogonal-only collision-aware edge routing.
#
# Rules the user spelled out:
#   - Straight (2-waypoint) lines: ALLOWED
#   - L-shape (3-waypoint, one 90 deg bend): ALLOWED
#   - Zig-zag (4+ waypoints): NOT ALLOWED
#   - Lines crossing through other shapes: NOT ALLOWED
#   - Diagonal lines (NOT axis-aligned): NOT ALLOWED  <-- key change in v1.2.12
#
# Algorithm (per edge):
#   0. If the edge already has 2 or 3 waypoints AND its existing route
#      does not pass through any obstacle, LEAVE IT ALONE. The original
#      BPMN tool placed those waypoints; second-guessing them creates
#      cosmetic regressions that are visible in Signavio.
#   1. Try a 2-waypoint orthogonal straight line - only if the source
#      and target centres are aligned within _ALIGN_TOLERANCE on one
#      axis. The two endpoints get snapped to the shared coordinate
#      so the line is perfectly horizontal or vertical (never diagonal).
#   2. Otherwise try L-shape #1: corner at (source.x, target.y) -
#      vertical leg first, then horizontal leg. Both legs collision-
#      checked.
#   3. Otherwise try L-shape #2: corner at (target.x, source.y) -
#      horizontal leg first, then vertical. Both legs collision-checked.
#   4. If every candidate route still collides with another shape,
#      leave the original waypoints alone.
_SHAPE_PADDING = 4.0     # px of slack around each obstacle bounding box
_ALIGN_TOLERANCE = 30.0  # px - "same row" / "same column" threshold


def _straighten_edges(root: ET.Element) -> List[Dict]:
    """Collision-aware edge routing. See module-level comment for the rules."""
    changes: List[Dict] = []

    # Collect every BPMNShape's bounds so we can do collision checks.
    shape_bounds: Dict[str, Tuple[float, float, float, float]] = {}
    shape_centres: Dict[str, Tuple[float, float]] = {}
    for shape in root.iter(f"{{{BPMN_DI_NS}}}BPMNShape"):
        ref = shape.get("bpmnElement") or ""
        bounds = shape.find(f"{{{DC_NS}}}Bounds")
        if not ref or bounds is None:
            continue
        try:
            x = float(bounds.get("x", 0)); y = float(bounds.get("y", 0))
            w = float(bounds.get("width", 0)); h = float(bounds.get("height", 0))
        except ValueError:
            continue
        # Skip lane / pool shapes - they're large containers that any
        # cross-lane edge "crosses" by design.
        elem = root.find(f".//*[@id='{ref}']")
        if elem is not None and _local(elem.tag) in ("lane", "participant"):
            continue
        shape_bounds[ref] = (x, y, w, h)
        shape_centres[ref] = (x + w / 2, y + h / 2)

    # Map each sequenceFlow id -> (source_id, target_id).
    flow_endpoints: Dict[str, Tuple[str, str]] = {}
    for f in root.iter(f"{{{BPMN_NS}}}sequenceFlow"):
        fid = f.get("id") or ""
        s   = f.get("sourceRef") or ""
        t   = f.get("targetRef") or ""
        if fid and s and t:
            flow_endpoints[fid] = (s, t)

    # v1.2.13 - for each node that has multiple outgoing edges (a "split"
    # gateway is the canonical example), pre-compute distinct EXIT POINTS
    # along the source shape's perimeter so the edges don't all emerge
    # from the same centre point and overlap near the source.
    # Same logic for multiple incoming edges into a "merge" node.
    exit_overrides  = _distribute_endpoints(
        flow_endpoints, shape_bounds, "out",
    )
    entry_overrides = _distribute_endpoints(
        flow_endpoints, shape_bounds, "in",
    )

    for edge in root.iter(f"{{{BPMN_DI_NS}}}BPMNEdge"):
        ref = edge.get("bpmnElement") or ""
        ends = flow_endpoints.get(ref)
        if not ends:
            continue
        src_id, tgt_id = ends
        src_centre = shape_centres.get(src_id)
        tgt_centre = shape_centres.get(tgt_id)
        if not src_centre or not tgt_centre:
            continue

        # v1.2.13 - if this node is one of multiple outputs from src_id,
        # use the pre-computed perimeter point instead of the centre so
        # the parallel outputs don't visually overlap at the gateway.
        src_for_route = exit_overrides.get(ref, src_centre)
        tgt_for_route = entry_overrides.get(ref, tgt_centre)

        old_wps = edge.findall(f"{{{DI_NS}}}waypoint")
        old_pts = [(float(w.get("x", 0)), float(w.get("y", 0))) for w in old_wps]

        # Obstacles = every shape except the edge's own source and target.
        obstacles = [
            (b[0] - _SHAPE_PADDING, b[1] - _SHAPE_PADDING,
             b[2] + 2 * _SHAPE_PADDING, b[3] + 2 * _SHAPE_PADDING)
            for k, b in shape_bounds.items()
            if k != src_id and k != tgt_id
        ]

        # Step 0: skip if the existing route is already clean enough AND
        # the source/target don't have multiple parallel edges that need
        # exit-point distribution. An edge with <=3 waypoints whose
        # segments don't cross any other shape was hand-placed by the
        # source BPMN tool and we leave it alone - second-guessing it
        # produces cosmetic regressions.
        needs_distribution = (ref in exit_overrides) or (ref in entry_overrides)
        if (not needs_distribution
                and len(old_pts) <= 3
                and not _path_crosses_any(old_pts, obstacles)):
            continue

        new_pts, kind = _best_clean_route(src_for_route, tgt_for_route, obstacles)
        if new_pts is None:
            # No clean orthogonal route - leave the original alone.
            continue

        # Skip if the existing waypoints are already this same clean route.
        existing = [(int(p[0]), int(p[1])) for p in old_pts]
        normalised_new = [(int(p[0]), int(p[1])) for p in new_pts]
        if existing == normalised_new:
            continue

        for w in old_wps:
            edge.remove(w)
        for x, y in new_pts:
            ET.SubElement(edge, f"{{{DI_NS}}}waypoint", {
                "x": str(int(x)), "y": str(int(y)),
            })

        changes.append({
            "category":   "Layout",
            "rule":       "F8 - lines straight or L-shape, never zigzag, never overlap, never diagonal",
            "element_id": ref,
            "before":     f"{len(old_wps)} waypoints",
            "after":      f"{len(new_pts)} waypoints ({kind})",
            "detail":     f"re-routed as {kind}; collision-checked against "
                          f"{len(obstacles)} other shapes",
        })

    return changes


def _path_crosses_any(
    pts: List[Tuple[float, float]],
    obstacles: List[Tuple[float, float, float, float]],
) -> bool:
    """Does any segment of the polyline `pts` cross any obstacle rect?"""
    for i in range(len(pts) - 1):
        if _segment_crosses_any(pts[i], pts[i + 1], obstacles):
            return True
    return False


# ---------------------------------------------------------------------------
# Routing helpers (collision-aware)
# ---------------------------------------------------------------------------

def _best_clean_route(
    src: Tuple[float, float],
    tgt: Tuple[float, float],
    obstacles: List[Tuple[float, float, float, float]],
) -> Tuple[Optional[List[Tuple[float, float]]], str]:
    """Try (in order): orthogonal 2-pt line (only if axis-aligned), then
    vertical-first L, then horizontal-first L. Diagonal lines are NEVER
    produced - if shapes are not axis-aligned, we always use an L-shape
    or leave the original alone.

    Returns (waypoints, kind) for the first clean option, or (None, '')
    if all candidates collide with obstacles.
    """
    sx, sy = src
    tx, ty = tgt

    horizontally_aligned = abs(sy - ty) <= _ALIGN_TOLERANCE
    vertically_aligned   = abs(sx - tx) <= _ALIGN_TOLERANCE

    # Option 1: 2-point ORTHOGONAL straight line.
    # Only attempted when source and target sit in the same row (or column)
    # within the alignment tolerance. Snap both endpoints to the shared
    # coordinate so the result is perfectly axis-aligned (no diagonal).
    if horizontally_aligned:
        avg_y = (sy + ty) / 2
        new_pts = [(sx, avg_y), (tx, avg_y)]
        if not _segment_crosses_any(new_pts[0], new_pts[1], obstacles):
            return new_pts, "straight (horizontal)"
    elif vertically_aligned:
        avg_x = (sx + tx) / 2
        new_pts = [(avg_x, sy), (avg_x, ty)]
        if not _segment_crosses_any(new_pts[0], new_pts[1], obstacles):
            return new_pts, "straight (vertical)"

    # Option 2: L-shape, vertical leg first (corner at source's X column,
    # target's Y row). Both legs are perfectly orthogonal.
    corner_v = (sx, ty)
    if (not _segment_crosses_any(src, corner_v, obstacles)
            and not _segment_crosses_any(corner_v, tgt, obstacles)):
        return [src, corner_v, tgt], "L-shape (vertical-first)"

    # Option 3: L-shape, horizontal leg first (corner at target's X column,
    # source's Y row).
    corner_h = (tx, sy)
    if (not _segment_crosses_any(src, corner_h, obstacles)
            and not _segment_crosses_any(corner_h, tgt, obstacles)):
        return [src, corner_h, tgt], "L-shape (horizontal-first)"

    return None, ""


def _segment_crosses_any(
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    rects: List[Tuple[float, float, float, float]],
) -> bool:
    for r in rects:
        if _segment_crosses_rect(p1, p2, r):
            return True
    return False


def _segment_crosses_rect(
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    rect: Tuple[float, float, float, float],
) -> bool:
    """Does the line segment p1-p2 intersect rect (x, y, w, h)?

    Uses a fast bounding-box rejection followed by Cohen-Sutherland-style
    clipping codes. Treats touching the rect's interior as crossing.
    """
    rx, ry, rw, rh = rect
    rmaxx = rx + rw
    rmaxy = ry + rh

    # Bounding-box reject
    if max(p1[0], p2[0]) <= rx:   return False
    if min(p1[0], p2[0]) >= rmaxx: return False
    if max(p1[1], p2[1]) <= ry:    return False
    if min(p1[1], p2[1]) >= rmaxy: return False

    # If either endpoint is strictly inside the rect, definitely crosses.
    for px, py in (p1, p2):
        if rx < px < rmaxx and ry < py < rmaxy:
            return True

    # Otherwise check intersection against each of the four rect edges.
    edges = [
        ((rx, ry),    (rmaxx, ry)),     # top
        ((rmaxx, ry), (rmaxx, rmaxy)),  # right
        ((rmaxx, rmaxy), (rx, rmaxy)),  # bottom
        ((rx, rmaxy), (rx, ry)),        # left
    ]
    for q1, q2 in edges:
        if _segments_intersect(p1, p2, q1, q2):
            return True
    return False


def _segments_intersect(
    p1: Tuple[float, float], p2: Tuple[float, float],
    p3: Tuple[float, float], p4: Tuple[float, float],
) -> bool:
    """Standard 2D line-segment intersection test (excludes collinear touch)."""
    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) > (b[1] - a[1]) * (c[0] - a[0])
    return (ccw(p1, p3, p4) != ccw(p2, p3, p4)
            and ccw(p1, p2, p3) != ccw(p1, p2, p4))


# ---------------------------------------------------------------------------
# v1.2.13 - distribute endpoints across the perimeter of multi-edge nodes
# ---------------------------------------------------------------------------

def _distribute_endpoints(
    flow_endpoints: Dict[str, Tuple[str, str]],
    shape_bounds: Dict[str, Tuple[float, float, float, float]],
    direction: str,           # "out" -> distribute exits; "in" -> distribute entries
) -> Dict[str, Tuple[float, float]]:
    """For every node with multiple outgoing (or incoming) edges, return
    a map of edge_id -> (x, y) override point on the node's perimeter.

    The override point becomes the source (or target) for routing, replacing
    the shape centre. This avoids the "all edges emerge from the same point"
    problem that makes split / merge gateways look cluttered.

    Algorithm:
      - Group edges by their `direction` neighbour (outgoing => group by source;
        incoming => group by target).
      - For each group of size >= 2:
          * Sort the group by the OPPOSITE node's centre Y (top to bottom).
          * Pick perimeter anchor points based on group size:
              2 edges  -> top-corner, bottom-corner
              3 edges  -> top-corner, right-corner, bottom-corner
              4+ edges -> top-corner, right-corner, bottom-corner, left-corner
                         (the rest fall back to right-corner)
          * Assign the sorted edges to the perimeter anchors in order.
    """
    overrides: Dict[str, Tuple[float, float]] = {}

    # Group edges by the node we're distributing around.
    groups: Dict[str, List[Tuple[str, str]]] = {}
    for fid, (sid, tid) in flow_endpoints.items():
        node = sid if direction == "out" else tid
        opposite = tid if direction == "out" else sid
        groups.setdefault(node, []).append((fid, opposite))

    for node_id, members in groups.items():
        if len(members) < 2:
            continue
        node_b = shape_bounds.get(node_id)
        if not node_b:
            continue
        nx, ny, nw, nh = node_b
        cx, cy = nx + nw / 2, ny + nh / 2

        # Annotate with opposite centre coords for sorting.
        annotated = []
        for fid, opp_id in members:
            opp_b = shape_bounds.get(opp_id)
            if opp_b:
                opp_cx = opp_b[0] + opp_b[2] / 2
                opp_cy = opp_b[1] + opp_b[3] / 2
            else:
                opp_cx, opp_cy = cx, cy
            annotated.append((fid, opp_cx, opp_cy))

        # Sort by opposite Y so the top-most opposite gets the top anchor.
        annotated.sort(key=lambda x: x[2])

        n = len(annotated)
        if n == 2:
            anchors = [(cx, ny), (cx, ny + nh)]                       # top, bottom
        elif n == 3:
            anchors = [(cx, ny), (nx + nw, cy), (cx, ny + nh)]        # top, right, bottom
        else:
            anchors = [
                (cx, ny),           # top
                (nx + nw, cy),      # right
                (cx, ny + nh),      # bottom
                (nx, cy),           # left
            ]
            while len(anchors) < n:
                anchors.append((nx + nw, cy))                          # extras default to right

        for (fid, _, _), anchor in zip(annotated, anchors):
            overrides[fid] = anchor

    return overrides


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag
