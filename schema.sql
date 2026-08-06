CREATE TABLE IF NOT EXISTS master_leads (
  lead_hash TEXT PRIMARY KEY,
  company_name TEXT NOT NULL DEFAULT '',
  owner_name TEXT NOT NULL DEFAULT '',
  revenue TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  dob TEXT NOT NULL DEFAULT '',
  ssn TEXT NOT NULL DEFAULT '',
  ein TEXT NOT NULL DEFAULT '',
  start_date TEXT NOT NULL DEFAULT '',
  all_phones TEXT NOT NULL DEFAULT '',
  all_emails TEXT NOT NULL DEFAULT '',
  sources TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_master_leads_company ON master_leads(company_name);
CREATE INDEX IF NOT EXISTS idx_master_leads_owner ON master_leads(owner_name);
