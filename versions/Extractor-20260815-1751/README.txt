EXTRACTOR

Clean name-neutral browser + local backend build.

START ON WINDOWS
1. Double-click start-local.bat.
2. Leave both console windows open while using the extractor.
3. The browser UI connects to the local backend automatically.

SEARCH
- Press Enter or Numpad Enter from a search input to begin.
- Advanced Search is closed by default.
- Live Research is collapsible and remembers its last open/closed state.
- Recent manual searches and structured results are retained in browser local storage.

WORKBOOK ENRICHMENT
- Click the workbook drop area to open the native XLSX file chooser or drag an XLSX onto it.
- The page confirms receipt immediately with filename and size.
- Column names do not need to match exact names. Aliases and fuzzy header detection map company, owner first/last/full, Phone1..N, Email1..N, address, city, state, ZIP, DOB, EIN, SSN, BSD and revenue fields.
- Progress, elapsed time, completed/remaining/failed counts, pause, continue and stop controls are available while a job is active.
- A partial or completed enriched XLSX can be downloaded when output is available.
- Output is sorted by Revenue descending.

IDENTITY AND CONTACT RULES
- Existing DOB and SSN remain preserved and unmasked.
- DOB is a high-weight identity signal for same-name owners.
- SSN is retained from the source workbook and is not used as a public-web search target.
- Phone display is normalized to (AAA) BBB-CCCC while search planning also tries hyphenated, spaced, digits-only and +1 forms.
- Mobile/landline classification uses numbering metadata; ambiguous US ranges remain Mobile/Landline rather than being falsely classified.
- Newly discovered phones/emails are allowed when identity evidence is strong; known values are corroboration anchors, not whitelist filters.
- Structured fields, confidence reasons and compact source links are primary. Long snippets stay in evidence/debug paths.

BACKEND ROUTES
GET  /health
POST /search
POST /search/stream
POST /batch/start
GET  /batch/status/{job_id}
POST /batch/control/{job_id}/{pause|continue|stop}
GET  /batch/download/{job_id}
