"""Registered-directory import plans (schema 3 add-on).

The plan book stores the previews this service issued, so an apply can only confirm a
preview the same client really received for the same project and directory, and a restarted
process can still apply it. `project_id` deliberately carries no foreign key: a preview is a
read-only statement about a directory and may exist before the project's first committed
write creates its `knowledge_projects` row. Every apply re-checks the registration itself.

Both the knowledge migration and the explicit directory migration install these definitions,
so fresh and upgraded databases cannot drift apart.
"""

VERSION = "1"

SCHEMA = """
CREATE TABLE knowledge_plans (plan_id TEXT PRIMARY KEY, client TEXT NOT NULL,
  project_id TEXT NOT NULL, directory TEXT NOT NULL, body TEXT NOT NULL,
  issued_revision INTEGER NOT NULL, issued_at TEXT NOT NULL);
CREATE INDEX knowledge_plans_scope ON knowledge_plans(client,project_id,directory);
"""

TRACKED = ["knowledge_plans"]
