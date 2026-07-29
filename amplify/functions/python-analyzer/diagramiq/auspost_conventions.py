"""Codified Australia Post BPMN 2.0 modelling conventions and self-review checklist.

Sources:
- "AP BPMN DMN Modeling Conventions.xlsx" (Signavio convention checker config)
- "Auspost BPMN 2.0 check list.xlsx"      (47-item self-review checklist)

Used in three places:

1. `bpmn_validator.validate_bpmn()` - mechanical structural rules (gateway
   meaningful, unique element names, consistent naming style, etc.)
2. AI prompts in `transcription_to_excel.py` and `ai_uplift.py` - the AI
   is told to follow these conventions when generating or uplifting BPMN.
3. The Excel intermediate produced by `bpmn_to_excel.py` - rule violations
   become a "Convention Issues" sheet so the user can see and edit them.

The data structure is stable across the codebase; downstream modules import
RULES, AI_PROMPT_BLOCK, and CHECKLIST_ITEMS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


# ---------------------------------------------------------------------------
# Severity levels (mirrors the Auspost legend tab)
# ---------------------------------------------------------------------------

SEV_ERROR   = "ERROR"      # Must be fixed - validation fails
SEV_WARNING = "WARNING"    # Should be fixed - reviewer attention required
SEV_INFO    = "INFO"       # Recommendation / threshold


# ---------------------------------------------------------------------------
# Convention rules (from "Modeling Conventions" sheet)
# ---------------------------------------------------------------------------

@dataclass
class Convention:
    rule_id:     str
    severity:    str
    name:        str
    description: str
    desired:     str = ""
    category:    str = ""


CONVENTIONS: List[Convention] = [
    Convention("AP-CONV-01", SEV_WARNING, "Usage of a defined BPMN subset",
               "Diagram uses only the AP-approved BPMN subset.",
               "BPMN (Preferred)", "structure"),
    Convention("AP-CONV-02", SEV_INFO, "Restricted diagram size",
               "Diagram fits on A3 or smaller.",
               "A3", "format"),
    Convention("AP-CONV-03", SEV_INFO, "Restricted number of activities",
               "Maximum 20 activities per diagram.",
               "20", "structure"),
    Convention("AP-CONV-04", SEV_INFO, "Restricted consecutive or-splits",
               "Chain of consecutive or-splits compressed to small fragments.",
               "2", "structure"),
    Convention("AP-CONV-05", SEV_INFO, "Restricted expanded pools",
               "Maximum 2 expanded pools per diagram.",
               "2", "structure"),
    Convention("AP-CONV-06", SEV_WARNING, "Activity before or-split",
               "Every or-split must have an activity as predecessor.",
               "", "flow"),
    Convention("AP-CONV-07", SEV_ERROR, "Activities in pools",
               "Every non-blackbox pool contains at least one activity.",
               "", "structure"),
    Convention("AP-CONV-08", SEV_ERROR, "Consistent XOR gateway naming",
               "XOR gateways named in 'Options only' style.",
               "Options only", "naming"),
    Convention("AP-CONV-09", SEV_WARNING, "Consistent activity naming",
               "Activities named in 'Verb + object + Conjunction' style.",
               "Verb object, Conjunction", "naming"),
    Convention("AP-CONV-10", SEV_ERROR, "End event naming",
               "End events: state description, categorisation, or verb-object.",
               "State description, Categorisation, Verb object", "naming"),
    Convention("AP-CONV-11", SEV_ERROR, "Start event naming",
               "Start events: state description, categorisation, or verb-object.",
               "State description, Categorisation, Verb object", "naming"),
    Convention("AP-CONV-12", SEV_ERROR, "Throwing intermediate event naming",
               "State description or categorisation style.", "", "naming"),
    Convention("AP-CONV-13", SEV_ERROR, "Catching intermediate event naming",
               "State description or categorisation style.", "", "naming"),
    Convention("AP-CONV-14", SEV_ERROR, "Correct BPMN syntax",
               "All elements modelled using correct BPMN syntax.", "", "syntax"),
    Convention("AP-CONV-15", SEV_INFO, "Decision logic for decisions",
               "All decisions have decision logic defined.", "", "logic"),
    Convention("AP-CONV-16", SEV_WARNING, "Distinct names for diagram and elements",
               "No element shares the diagram's name.", "", "naming"),
    Convention("AP-CONV-17", SEV_ERROR, "Meaningful gateways",
               "Every gateway has splitting or merging behaviour.", "", "logic"),
    Convention("AP-CONV-18", SEV_INFO, "Message flows on correct nodes",
               "Message flows annotated to sender and receiver.", "", "flow"),
    Convention("AP-CONV-19", SEV_ERROR, "No multiple edges between nodes",
               "No two edges share the same source and target.", "", "flow"),
    Convention("AP-CONV-20", SEV_INFO, "Single start event in subprocess",
               "One start event per subprocess.", "", "structure"),
    Convention("AP-CONV-21", SEV_INFO, "Single start event in process",
               "One start event per process.", "", "structure"),
    Convention("AP-CONV-22", SEV_ERROR, "Specified colours",
               "Default colours not changed.", "", "format"),
    Convention("AP-CONV-23", SEV_ERROR, "Specified element sizes",
               "Default element sizes not changed.", "", "format"),
    Convention("AP-CONV-24", SEV_INFO, "Start message events in subprocesses",
               "Use start message events where appropriate.", "", "structure"),
    Convention("AP-CONV-25", SEV_WARNING, "Sufficient distance between elements",
               "Minimum 75px between elements.", "75", "layout"),
    Convention("AP-CONV-26", SEV_WARNING, "Sequence flow direction",
               "Edge direction matches modelling orientation.", "", "layout"),
    Convention("AP-CONV-27", SEV_WARNING, "Message flow direction",
               "Message flow direction matches orientation.", "", "layout"),
    Convention("AP-CONV-28", SEV_ERROR, "Unique diagram names",
               "All diagram names unique within the workspace.", "", "naming"),
    Convention("AP-CONV-29", SEV_ERROR, "Unique element names",
               "Element names unique within a diagram (start events scope).",
               "Start events", "naming"),
]


# ---------------------------------------------------------------------------
# Self-review checklist (47 items from "Self Review PM Checklist" sheet)
# ---------------------------------------------------------------------------

@dataclass
class ChecklistItem:
    item_id:    str
    check_type: str          # Convention / Logic / Usage
    category:   str
    standard:   str
    description: str
    format:     str = ""
    review_q:   str = ""


CHECKLIST_ITEMS: List[ChecklistItem] = [
    ChecklistItem("AP-CHK-01", "Convention", "Naming Convention",
                  "Model name unique and meaningful",
                  "Model name follows approved naming convention.",
                  "Verb + Object + Context",
                  "Does the model follow the correct naming convention?"),
    ChecklistItem("AP-CHK-02", "Convention", "Naming Convention",
                  "Task names",
                  "Activities use Action verb + object, present tense. Do NOT include system name.",
                  "Action verb + object + Present tense",
                  "Does the model follow the correct naming convention?"),
    ChecklistItem("AP-CHK-03", "Convention", "Naming Convention",
                  "Concise task labels",
                  "Labels are concise (max 5-7 words) and unambiguous.",
                  "Concise, unambiguous, no overlay on Task Type icon",
                  "Are activity labels concise and unambiguous?"),
    ChecklistItem("AP-CHK-04", "Convention", "Process Structure",
                  "No combined tasks",
                  "Each activity is a single unit of work. Avoid 'AND' in task names.",
                  "Granular, role-specific, action-oriented, observable, avoid decision-oriented",
                  "Are activities defined as single units of work?"),
    ChecklistItem("AP-CHK-05", "Convention", "Naming Convention",
                  "Start/End event naming",
                  "Describe the state in past tense.",
                  "Object + Action Verb + Past tense",
                  "Does the model follow the correct naming convention?"),
    ChecklistItem("AP-CHK-06", "Logic", "Process Structure",
                  "Start->End event sync",
                  "Start and End events show how inputs become outputs.",
                  "Outcomes match trigger context",
                  "Are trigger and outcomes clearly reflected?"),
    ChecklistItem("AP-CHK-07", "Convention", "Naming Convention",
                  "Timer Start Event",
                  "Name conveys time (e.g., 'Daily at 10am', 'Quarterly').",
                  "Triggered at a set time / date",
                  "Does the timer event follow the correct convention?"),
    ChecklistItem("AP-CHK-08", "Convention", "Naming Convention",
                  "Intermediate timer event",
                  "Indicates a time elapse (e.g., 'After 2 hours').",
                  "Sense of time elapsing must be present",
                  "Does the element follow the correct convention?"),
    ChecklistItem("AP-CHK-09", "Convention", "Usage",
                  "Catching/Throwing Intermediate Link Events",
                  "Use to avoid flow crossings - NOT to call another process.",
                  "Connect 2 points within the same model",
                  "Are link events used correctly?"),
    ChecklistItem("AP-CHK-10", "Convention", "Naming Convention",
                  "Intermediate Link event names",
                  "Pair-named so next task is clear (e.g., 'Order review required').",
                  "Object + Action Verb + Past tense",
                  "Does the element follow the correct convention?"),
    ChecklistItem("AP-CHK-11", "Convention", "Usage",
                  "Decision Gateways",
                  "Add a decision task before each split. Action verb + object, present tense.",
                  "Action verb + object + Present tense",
                  "Does the decision precede each gateway?"),
    ChecklistItem("AP-CHK-12", "Convention", "Naming Convention",
                  "Roles / Lanes",
                  "Swimlanes named after org roles (e.g., 'Retail Operations Support').",
                  "Per Org Structure - Org Unit",
                  "Do swimlanes reflect responsible roles?"),
    ChecklistItem("AP-CHK-13", "Logic", "Best Practice",
                  "Org-structure hierarchy at right level",
                  "L3: hierarchy CEO-3. L4: hierarchy CEO-6. One swimlane at L3.",
                  "L3: single swimlane Headoff(-3). L4: multiple swimlanes allowed",
                  "Is the right org-structure hierarchy used?"),
    ChecklistItem("AP-CHK-14", "Convention", "Naming Convention",
                  "Decision Task wording",
                  "Decision task wording yields 2+ outcomes.", "",
                  "Does the wording yield two or more outcomes?"),
    ChecklistItem("AP-CHK-15", "Convention", "Naming Convention",
                  "Catch & Throw alignment",
                  "Catch event aligns with the corresponding task.",
                  "Past tense, aligned with corresponding task",
                  "Do Catch/Throw align with the corresponding task?"),
    ChecklistItem("AP-CHK-16", "Logic", "Best Practice",
                  "Checklists on process models",
                  "If used as validation form, link to template; if template, treat as data object.",
                  "1. As validation form: link to template. 2. As template: instances follow template.",
                  "Is the checklist linked or documented appropriately?"),
    ChecklistItem("AP-CHK-17", "Convention", "Process Flow",
                  "Avoid sequence flow crossings",
                  "Flows easy to read with no unnecessary crossings.", "",
                  "Are sequence flows free from unnecessary crossings?"),
    ChecklistItem("AP-CHK-18", "Convention", "Process Flow",
                  "Loops / dead ends",
                  "Loops or dead ends must be intentional and documented.", "",
                  "Are any loops/dead ends intentional and documented?"),
    ChecklistItem("AP-CHK-19", "Logic", "Process Flow",
                  "Logical flow",
                  "Process flow is logical and easy to follow.", "",
                  "Is the flow logical and readable?"),
    ChecklistItem("AP-CHK-20", "Logic", "Mandatory Attributes",
                  "Process-level documentation",
                  "Outcome-focused, business-oriented, concise, unambiguous, stable.",
                  "1-3 sentences, paragraph",
                  "Why does this process exist and what does it deliver?"),
    ChecklistItem("AP-CHK-21", "Convention", "Task Attributes",
                  "Task types / attributes",
                  "Add task types and relevant attributes.", "",
                  "Are task types and attributes added?"),
    ChecklistItem("AP-CHK-22", "Logic", "Attribute - Loop",
                  "Loop / sequential loop with annotation",
                  "Use loop marker AND text annotation when task repeats until done.",
                  "Text Annotation: 'Fix errors until resolved'",
                  "Has loop marker + annotation been applied?"),
    ChecklistItem("AP-CHK-23", "Logic", "Other Attributes",
                  "Message flows, data objects, attributes",
                  "Activity attributes (description, responsible team, systems) completed.",
                  "IT system names align with enterprise architecture standards",
                  "Are activity attributes completed?"),
    ChecklistItem("AP-CHK-24", "Logic", "Attribute - Documentation",
                  "L4 task-level documentation",
                  "Add task-level documentation linked to Signavio Dictionary roles.", "",
                  "Has task-level documentation been included for L4 activities?"),
    ChecklistItem("AP-CHK-25", "Convention", "Roles / Lanes",
                  "Swimlane responsibility",
                  "Swimlanes correctly represent responsible roles or teams.", "",
                  "Are swimlanes aligned with roles?"),
    ChecklistItem("AP-CHK-26", "Convention", "Roles / Lanes",
                  "No empty lanes",
                  "Each activity assigned to the correct lane.",
                  "Activities shown in correct swimlane",
                  "Is each activity assigned to the correct lane?"),
    ChecklistItem("AP-CHK-27", "Convention", "Roles / Lanes",
                  "Generic Roles",
                  "L3: do NOT use Generic Roles. L4: Generic Roles allowed.", "",
                  "Are Generic Roles used at the correct level?"),
    ChecklistItem("AP-CHK-28", "Convention", "Data Objects",
                  "Clear data-object identifiers",
                  "Data objects represent real business artefacts (form, ticket, sheet).",
                  "INPUT/OUTPUT distinction; state in [brackets] only when IT app supports it",
                  "Are data-object attributes used correctly?"),
    ChecklistItem("AP-CHK-29", "Convention", "Data Objects",
                  "Message object for third-party exchange",
                  "Show message object between swimlane and 3rd-party collapsed pool.",
                  "If initiated by 3rd party - uncheck 'Is initiating'",
                  "Is a message object used for third-party exchange?"),
    ChecklistItem("AP-CHK-30", "Convention", "Format",
                  "Consistent formatting",
                  "Equal spacing, no overlays.",
                  "Sentence case (first letter capital, rest lowercase) unless abbreviation",
                  "Is formatting consistent throughout?"),
    ChecklistItem("AP-CHK-31", "Logic", "Events",
                  "Correct event types",
                  "Use message, timer, intermediate events appropriately.", "",
                  "Are correct event types applied?"),
    ChecklistItem("AP-CHK-32", "Convention", "Start Event",
                  "Single start event",
                  "One start event per model. If a 2nd is needed, add text annotation.",
                  "Reference: Resolve Disputed Fees & Charges",
                  "Is only one start event used (or annotated)?"),
    ChecklistItem("AP-CHK-33", "Convention", "End Events",
                  "All end events shown at L3",
                  "L4 end events also reflected on the L3 model.", "",
                  "Are L4 end events represented on the L3 model?"),
    ChecklistItem("AP-CHK-34", "Convention", "Intermediate Events",
                  "Backward flows via catch/throw",
                  "Use catch/throw intermediate events for backward flows.",
                  "Catch and Throw events",
                  "Are backward flows handled via catch/throw?"),
    ChecklistItem("AP-CHK-35", "Convention", "Gateways",
                  "Appropriate gateways",
                  "XOR/AND/OR used correctly to reflect decision logic.", "",
                  "Has the appropriate gateway been applied?"),
    ChecklistItem("AP-CHK-36", "Convention", "Gateways",
                  "Decision outcomes specific",
                  "Avoid Yes/No outcomes - use specific past-tense labels.",
                  "Past tense, relevant to decision",
                  "Are decision outcomes specific (not Yes/No)?"),
    ChecklistItem("AP-CHK-37", "Convention", "Compliance / Glossary",
                  "Dictionary alignment",
                  "All terms align with the process glossary or business dictionary.", "",
                  "Do terms align with the dictionary?"),
    ChecklistItem("AP-CHK-38", "Logic", "Compliance / Glossary",
                  "Controls / Policy / Rules",
                  "Mandatory compliance elements (controls, risks, rules) included.", "",
                  "Are required compliance elements captured?"),
    ChecklistItem("AP-CHK-39", "Convention", "Linking L4 to L3",
                  "L3<->L4 model linkage",
                  "L4 models linked to corresponding L3 so name flows through.", "",
                  "Are L4 models properly linked to L3?"),
    ChecklistItem("AP-CHK-40", "Logic", "Artifacts - Text Annotation",
                  "Text annotations for important info",
                  "Brief on annotation; full detail in documentation section.", "",
                  "Are text annotations used appropriately?"),
    ChecklistItem("AP-CHK-41", "Convention", "Collapsed Pool",
                  "Single collapsed pool per third party",
                  "Avoid multiple collapsed pools for the same third party.",
                  "One collapsed pool per third party",
                  "Is only one collapsed pool used per third party?"),
    ChecklistItem("AP-CHK-42", "Logic", "Convention Check",
                  "AP BPMN Conventions",
                  "Signavio validation returns no errors; warnings reviewed.", "",
                  "Does the model pass Signavio validation?"),
    ChecklistItem("AP-CHK-43", "Convention", "Dictionary entry",
                  "Org-Unit dictionary entries",
                  "Dictionary entry represents an actual AP Org Unit, not 'THE BUSINESS'.", "",
                  "Has the dictionary entry been used correctly?"),
    ChecklistItem("AP-CHK-44", "Logic", "Data Objects",
                  "L3<->L4 data object usage",
                  "Key data objects from dictionary; reference on L3 implies usage on L4.", "",
                  "Are data objects used consistently across L3/L4?"),
    ChecklistItem("AP-CHK-45", "Logic", "Usage",
                  "Send/Receive as separate tasks",
                  "Send and Receive activities modelled as separate tasks.",
                  "Two tasks; message flow between collapsed pool and task",
                  "Are Send/Receive separate tasks with message flows?"),
    ChecklistItem("AP-CHK-46", "Usage", "Task",
                  "Loop / multi-instance for generic role groups",
                  "When lane is generic group ('Evaluation Panel'), use loop or multi-instance.", "",
                  "Is loop/multi-instance used for generic groups?"),
    ChecklistItem("AP-CHK-47", "Logic", "Additional Participants",
                  "Additional Participants on L3",
                  "Use Additional Participants at L3 to depict org-unit interactions.",
                  "Restricted at L4; consult Governance team",
                  "Are Additional Participants used appropriately?"),
]


# ---------------------------------------------------------------------------
# AI prompt block
# ---------------------------------------------------------------------------

AI_PROMPT_BLOCK = """\
AUSPOST BPMN 2.0 CONVENTIONS (mandatory):
Activities/Tasks:
  - Action verb + object, present tense. Example: "Approve invoice", "Send notification".
  - DO NOT include the IT system name in the task name.
  - Concise (max 5-7 words). Single unit of work - never combine with "AND".
Start / End Events:
  - Object + Action Verb + Past tense. Example: "Order received", "Invoice approved".
Intermediate Events:
  - Past tense, name conveys what just happened.
  - Use catch/throw to represent backward flows or to avoid line crossings.
Gateways:
  - XOR: name in "Options only" style. Decision outcomes must be specific past-tense
    statements - never "Yes" / "No".
  - Always precede a gateway with a decision task.
Swimlanes / Roles:
  - Name after organisational roles (e.g., "Retail Operations Support").
  - L3 model: ONE swimlane only. L4: multiple swimlanes allowed.
  - Generic roles permitted only at L4.
Pools:
  - Maximum 2 expanded pools per diagram.
  - One collapsed pool per third party - never duplicate.
Data Objects:
  - Represent real business artefacts (form, ticket, spreadsheet).
  - State in [brackets] only when the IT system supports that state.
Process Structure:
  - One start event per model (annotate if a second is unavoidable).
  - All possible end events shown at the L3 level.
  - Maximum 20 activities per diagram.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def conventions_by_severity(severity: str) -> List[Convention]:
    return [c for c in CONVENTIONS if c.severity == severity]


def checklist_by_category(category: str) -> List[ChecklistItem]:
    return [c for c in CHECKLIST_ITEMS if c.category == category]
