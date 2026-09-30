# N07 stable quota pagination

Baseline live UI on the dedicated 11-account acceptance workspace exposed row movement from page 1 to page 2 after saving total quota while every used_quota was zero. Existing get_quota_list sorted only used_quota DESC, so equal values had no deterministic order. This is a baseline UX defect discovered during acceptance; its merge origin is not established.

The sole production change adds account_id ASC after used_quota DESC. The account_id unique constraint makes the tie order deterministic. Page size, filtering, primary usage ranking, amount precision, writes and permissions are preserved. No schema/migration or application data changes.

Two behavior tests use real SQLite ORM with 12 accounts inserted in reverse ID order, 10/2 page results, one nonzero usage account, an edited total with 7 decimals, stable memberships and ranks, usage preservation and filtered search. Reverse insertion order makes this test fail with the old unspecified tie order.

Focused quota suite: 29 passed in 2.15s (2 upstream deprecation warnings). Ruff 0.16.6 check and format --check pass for both changed paths. Initial fixture errors came from Account.id init=False and were corrected; no implementation failure. Git diff --check is recorded separately. This evidence is source/tests only. Final canonical image rebuild and actual UI same-value pagination acceptance remain owned by runtime; old candidate records are unchanged.

Source frozen at HEAD 953cf1c1ef88258197c72b98e89fcd1dab85ac39 plus the two paths in source.sha256. Neither staged nor committed.
