# SDD — Leads Salvos (persisted lead library)

## Goal

Transform the "Leads salvos" tab from "last search in memory" into a durable, named library of lead tables that survives app restarts, can be exported, imported, enriched (placeholder), and merged.

## Non-goals

- Real enrichment via Snov.io / Apollo / Lusha — placeholder only.
- Redesigning the rest of the app.
- Schema for sequenced outreach / campaigns / pipelines.

## Storage

- New Settings field: `saved_leads_path` (env `BEAUTIFUL_LINKEDIN_SAVED_LEADS_PATH`, default `data/saved_leads.sqlite`). Kept distinct from `cache_path` so durable lead data is never wiped with API caches.
- New module: `src/beautiful_linkedin/storage/saved_leads.py` (`SavedLeadsStore`). Mirrors the lock + context manager pattern from `cache/lead_history.py`.
- Two tables only. No dynamic per-user table names.

### Schema

```sql
CREATE TABLE saved_lead_tables (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  source_type TEXT NOT NULL,           -- 'search' | 'imported' | 'merged'
  keywords_json TEXT NOT NULL,         -- JSON list[str] of user-supplied titles/keywords
  search_queries_json TEXT NOT NULL,   -- JSON list[str], public-search queries (may be empty)
  search_request_json TEXT NOT NULL,   -- JSON dict, sanitized SearchRequest echo (NO secrets)
  enrichment_status TEXT NOT NULL      -- 'not_enriched' | 'not_implemented' | 'enriched'
);

CREATE TABLE saved_leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_id TEXT NOT NULL REFERENCES saved_lead_tables(id) ON DELETE CASCADE,
  lead_key TEXT NOT NULL,
  -- OUTPUT_COLUMNS:
  company_name TEXT NOT NULL,
  company_domain TEXT,
  person_name TEXT,
  title TEXT,
  linkedin_url TEXT,
  email TEXT,
  source_url TEXT NOT NULL,
  source_type TEXT NOT NULL,
  snippet TEXT NOT NULL DEFAULT '',
  matched_title TEXT,
  validation_status TEXT NOT NULL DEFAULT 'valid',
  validation_note TEXT,
  confidence_score INTEGER NOT NULL,
  previously_consulted_at TEXT,
  consultation_note TEXT,
  raw_json TEXT,
  enrichment_payload_json TEXT,
  enriched_at TEXT,
  UNIQUE(table_id, lead_key)
);
CREATE INDEX idx_saved_leads_table ON saved_leads(table_id);
```

`lead_key` is computed via `lead_dedupe_key(lead)`, stored as `"<key_type>:<key_value>"`. For leads without a key, a synthetic row-scoped key is used so they still satisfy UNIQUE.

## Secret hygiene

When persisting `search_request_json`, drop these fields if present: `linkedin_cookie`, `api_keys`, `linkedin_li_at_cookie`. Keep `company_name`, `titles`, `lead_providers`, `search_engines`, `scrape_mode`, `max_results`, `search_depth`. `keywords_json` contains the user's titles only.

## Python API (`SavedLeadsStore`)

```python
create_table(name, source_type, keywords, search_queries=None, search_request=None) -> SavedLeadTable
list_tables() -> list[SavedLeadTable]
get_table(table_id) -> SavedLeadTable
delete_table(table_id) -> None

add_leads(table_id, leads: list[Lead]) -> int    # returns inserted count, dedupes via UNIQUE
list_leads(table_id) -> list[Lead]

export_csv(table_id, output_path=None) -> Path
import_table(name, file_path: str | Path) -> SavedLeadTable  # csv or xlsx
merge_tables(name, table_ids: list[str], keywords=None) -> SavedLeadTable
mark_enrichment(table_id, status, payload=None) -> SavedLeadTable
```

`SavedLeadTable` is a Pydantic model containing `id, name, created_at, updated_at, source_type, keywords, search_queries, search_request, enrichment_status, lead_count`.

### Import column aliases

Configured constants (case-insensitive, trimmed):

- person: `person_name, name, full_name, nome, nome_completo, lead_name, contact_name, profile_name`
- company: `company_name, company, organization, organization_name, empresa, current_company, employer, account_name`
- title: `title, job_title, current_title, position, job_position, role, cargo, funcao, função, headline, linkedin_headline, occupation, current_position`
- linkedin_url: `linkedin_url, linkedin, profile_url`
- email: `email, e-mail, mail`
- company_domain: `company_domain, domain, website`

Each row → `Lead(source_type="imported_table", source_url=linkedin_url or f"import:{file.stem}:{row_index}", confidence_score=70, validation_status="valid")`. If person/company/title columns are missing, raise `ImportColumnError`.

### Merge

Uses `deduplicate_leads()` (highest `confidence_score` wins). Combined `keywords` and `search_queries` are unioned preserving first-seen order.

## FastAPI endpoints (`server/app.py`)

```
GET    /lead-tables                            -> list[SavedLeadTableSummary]
GET    /lead-tables/{table_id}                 -> SavedLeadTableDetail (table + leads)
POST   /lead-tables                            -> SavedLeadTableDetail (save current leads)
POST   /lead-tables/import                     -> SavedLeadTableDetail
POST   /lead-tables/{table_id}/export          -> { output_path }
POST   /lead-tables/{table_id}/enrich          -> SavedLeadTableSummary (status -> not_implemented)
POST   /lead-tables/merge                      -> SavedLeadTableDetail
DELETE /lead-tables/{table_id}                 -> { ok: true }
```

Payloads:

- `SaveLeadTableRequest { name, leads: list[Lead], keywords?: list[str], search_request?: dict, generate_queries?: bool }`
- `ImportLeadTableRequest { name, file_path: str }`
- `ExportLeadTableRequest { output_path?: str }`
- `MergeLeadTablesRequest { name, table_ids: list[str], keywords?: list[str] }`

The save endpoint, when `generate_queries=True` and `search_request` contains `company_name` + `titles`, calls `build_balanced_queries_for_company()` and stores the resulting queries.

## Electron client (`shared/api.ts`, `shared/types.ts`)

Add typed methods `listLeadTables`, `getLeadTable`, `createLeadTable`, `importLeadTable`, `exportLeadTable`, `enrichLeadTable`, `mergeLeadTables`, `deleteLeadTable`. Mirror payload types.

## UI changes (App.tsx)

The "Leads salvos" view becomes a library:

- Header row of actions: **Salvar busca atual**, **Importar tabela**, **Juntar tabelas**.
- Left: list of tables (name, lead count, keywords as chips, updated_at, enrichment status badge).
- Right: open table → reuse `ResultsTable` with actions **Exportar CSV** and **Enriquecer** (shows toast: integração ainda não implementada).
- All state hydrated from the API on view open; nothing in localStorage.

No restyle elsewhere.

## Tests

Python:

- `tests/test_saved_leads_store.py` — create/list/get/delete, add_leads dedupe, export_csv schema parity with `OUTPUT_COLUMNS`, import (csv + xlsx + missing-column failure + alias recognition), merge dedupes via `lead_dedupe_key`, secret stripping in `search_request_json`.
- `tests/test_server_saved_leads_api.py` — round-trip via FastAPI TestClient with the store pointed at a tmp_path SQLite, no network.

Electron:

- `electron/test/api-client.test.ts` (or extend existing) covers new methods via `fetch` mock.
