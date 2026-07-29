"""DiagramIQ BPMN engine — ported verbatim from the desktop app's src/.

Every module here is stdlib-only (re, xml.etree, zipfile, dataclasses), which
is why it drops into a zip Lambda with no build step and no layer. Modules
needing openpyxl (excel_to_bpmn, bpmn_patcher, uplift_report) and the AI passes
are deliberately not included yet — see the port plan in the PR.

Upstream: github.com/venkaat92-Rock/Diagram.IQ  (src/, v1.2.27)
Keep these files close to upstream so fixes can be diffed across both apps.
"""
