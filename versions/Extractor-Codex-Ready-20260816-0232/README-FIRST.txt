CODEX EXTRACTOR — AUDITED HANDOFF

Start from the audited 20260816-0232 source/package delivered with this version. Do not use stale or earlier implementations.

Workflow:
1. Read the pre-Codex audit report.
2. Read the full master handoff completely.
3. Use every sheet in the QA workbook as the acceptance/test plan.
4. Use the 91-lead regression workbook for smoke/full tests.
5. Use GitHub on every accepted improvement cycle.
6. Never overwrite a previous version; create a new unique version each time.
7. For weak/no-result leads, deliberately test different legitimate public-source query combinations and measure which routes improve correct useful-field yield.
8. Add a regression test for every repeatable defect.
9. Retest the exact failing case, the diverse 10-lead smoke set, and then all 91 leads whenever search/extraction/scoring changes.
10. Continue until the acceptance gates pass twice consecutively.

Pre-handoff baseline: Python compile PASS, 12/12 backend tests PASS, 91/91 planner rows PASS, all valid phones get both mandatory formats, all valid input emails enter planning, SSN search protection PASS, real Uvicorn batch pause/continue/stop/partial-download PASS, frontend JS/ID/structure audit PASS, and desktop/mobile browser-mocked behavior PASS.
