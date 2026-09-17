"""Versioned research notes and explicit project decisions (schema 3 add-on).

The knowledge migration and the explicit research-note migration both install these
definitions; the statements live here so fresh and upgraded databases cannot drift apart.
A note version keeps its own immutable payload, and the citations of that version live in
their own table, so "which source version was actually read" is answered by a row instead of
being re-derived from a title or a later revision of the note.
"""

SCHEMA = """
CREATE TABLE research_notes (id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES knowledge_projects(id), key TEXT NOT NULL,
  owner TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX research_notes_project ON research_notes(project_id,state,id);
CREATE TABLE research_note_history (note_id TEXT NOT NULL REFERENCES research_notes(id),
  version INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(note_id,version));
CREATE TABLE research_note_citations (note_id TEXT NOT NULL, version INTEGER NOT NULL,
  ordinal INTEGER NOT NULL, kind TEXT NOT NULL, block_id TEXT, document_id TEXT,
  document_version INTEGER, hash TEXT NOT NULL, cited_note_id TEXT, cited_note_version INTEGER,
  PRIMARY KEY(note_id,version,ordinal));
CREATE INDEX research_note_citations_source ON research_note_citations(document_id,document_version);
CREATE INDEX research_note_citations_note ON research_note_citations(cited_note_id);
CREATE VIRTUAL TABLE research_note_index USING fts5(note_id UNINDEXED, project_id UNINDEXED, text);
"""

TRACKED = "research_notes research_note_history research_note_citations".split()

RESEARCH_NOTES_SCHEMA_VERSION = "1"
