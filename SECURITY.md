# Security

schemagate decides which schema objects a caller may see. If you find a way for
a caller to see an object they are not entitled to — in the selected list, in
the rendered DDL, through foreign-key expansion, through the MCP server, or
through the Studio — that is a security bug, not a ranking bug.

Report it privately through GitHub's "Report a vulnerability" button on this
repository (Security tab). Please include a minimal catalog (object names,
roles, the principal) and the question that reproduces it. Expect an
acknowledgement within three days.

Do not open a public issue for entitlement bypasses.

## What schemagate does and does not protect

- It filters *schema metadata* by identity before retrieval. It never sees or
  filters row data; row-level security in the database is still yours to
  configure.
- The AI describer sends only object and column names, types and comments to
  the provider you configure. It has no field a row could travel in.
- The MCP server redacts database passwords from every output it produces.
- Anonymous callers (no principal) see only objects with no role requirement.
