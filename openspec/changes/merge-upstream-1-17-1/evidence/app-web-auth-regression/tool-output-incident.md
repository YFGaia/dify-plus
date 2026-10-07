# Scope-neutral tool-output incident

During login-fixture diagnosis, an overly broad text search of a private local directory printed a session JSON line into the tool output. This was an avoidable credential-handling error. No credential values, token hashes, or raw output are stored in this evidence.

The visible output included synthetic Dify cookie names `access_token` and `csrf_token`; `refresh_token` was present in the original private export, without inspecting whether its value survived tool truncation. Unrelated localhost cookie names visible in the output were `multica_auth`, `__next_hmr_refresh_hash__`, `multica_logged_in`, and `agentdock_logged_in`. All domains visible were localhost; the export request targeted only localhost:23010 and localhost:25442. No additional unrelated-account inspection, logout, rotation or session modification was performed.

The private canonical Dify session was rewritten to the exact allowlist `access_token`, `refresh_token`, `csrf_token`, and verified to contain only `domain=localhost`, `path=/`, with file permission 0600. Ordinary API access was verified 200 without an explicit owner login/logout/refresh. Other agents were instructed to reread the canonical file rather than reuse an expired in-memory cookie set.

All subsequent private credential operations use exact schema reads in memory and preselected safe output fields. No private-directory search, raw cookie serialization to tool output, or unsolicited changes to other application sessions are permitted. The coordinator received the incident summary and informed the user.
