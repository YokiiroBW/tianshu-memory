"""Project lesson book and promoted global experience tables (schema 3 add-on).

Both the knowledge migration and the explicit lessons migration install these definitions;
the statements live here so fresh and upgraded databases cannot drift apart.
"""

SCHEMA = """
CREATE TABLE lessons (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES knowledge_projects(id),
  version INTEGER NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX lessons_project ON lessons(project_id,state);
CREATE TABLE lesson_history (lesson_id TEXT NOT NULL, version INTEGER NOT NULL,
  payload TEXT NOT NULL, PRIMARY KEY(lesson_id,version));
CREATE VIRTUAL TABLE lesson_index USING fts5(lesson_id UNINDEXED, project_id UNINDEXED, text);
CREATE TABLE experience_entries (id TEXT PRIMARY KEY, version INTEGER NOT NULL, state TEXT NOT NULL,
  project_id TEXT NOT NULL, payload TEXT NOT NULL, record TEXT NOT NULL);
CREATE TABLE experience_history (entry_id TEXT NOT NULL, version INTEGER NOT NULL,
  state TEXT NOT NULL, project_id TEXT NOT NULL, payload TEXT NOT NULL, record TEXT NOT NULL,
  PRIMARY KEY(entry_id,version));
CREATE TABLE experience_lesson_refs (entry_id TEXT NOT NULL REFERENCES experience_entries(id),
  lesson_id TEXT NOT NULL, project_id TEXT NOT NULL, version INTEGER NOT NULL, hash TEXT NOT NULL,
  state TEXT NOT NULL, PRIMARY KEY(entry_id,lesson_id));
CREATE VIRTUAL TABLE experience_index USING fts5(entry_id UNINDEXED, project_id UNINDEXED, text);
"""

TRACKED = (
    "lessons lesson_history experience_entries experience_history experience_lesson_refs"
).split()

LESSONS_SCHEMA_VERSION = "1"
