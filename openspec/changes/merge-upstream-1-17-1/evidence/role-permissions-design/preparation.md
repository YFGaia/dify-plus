# F03/F04 dedicated-role runtime preparation

This is a preparation record, **not** runtime acceptance. Full-image isolated stack readiness is pending with the runtime verifier.

Final planned endpoints: Web `http://localhost:23010`, API `http://localhost:25442`, websocket `http://localhost:25443`, with Docker ports bound to `[::1]`. The initially proposed `127.0.0.2` host had no loopback alias on this machine and could not bind; coordination changed external URLs to `localhost`, which resolves to `::1` here, and confirmed Docker's IPv6 loopback bind with a temporary probe. The user's existing stack was not changed. All test URLs use the same host for session/CSRF behavior, separate from the existing user's `127.0.0.1` cookie host. Reuse Ego TaskSpace 9 in a new delegated tab; never operate runtime's p1 concurrently. Browser tabs share cookies, so owner/admin/normal role tests run serially with product logout/login between accounts.

## Legitimate test identities

- Fresh owner is created through first `/install`, by the runtime verifier.
- Owner invites one admin and nine normal accounts using the product workspace member invitation API/UI, all under `example.invalid`. This yields 11 disposable accounts including owner. Account creation already calls `ensure_account_quota_extend` in its transaction; quota pagination therefore has 11 balance rows without artificial balance inserts.
- `MAIL_TYPE` must be disabled. `mail_invite_member_task` returns when `mail.is_inited()` is false, and the invalid email domain ensures no real recipient. No mail server or provider is configured for acceptance.
- COMMUNITY invitation results include legitimate activation links in both API response and the invitation modal. Tokens stay inside the test script, with no console, screenshot or durable evidence output. Snapshot output must redact token/invite_token query values before printing.
- For an invited pending account without a password, `/login` with its valid invitation token and a newly generated test password is the actual supported password-initialization path (`account_login_service.py`, `_password_login_completion`). Then `/signin/invite-settings` and `/activate` complete account setup. No SQL role grants, fabricated session cookies or direct password changes are permitted.
- Only admin and one normal need login/activation for the three-role boundary; remaining valid pending invitations are sufficient for pagination. Activate all samples through product flows if the test requires active-only data.

## Expected existing permission contract

| Role | System-manage navigation | Direct system-manage URL | System-management API |
| --- | --- | --- | --- |
| owner | present | renders business view | allowed |
| admin | absent | permission message | allowed |
| normal/member | absent | permission message | 403 |

The owner-only UI versus owner/admin API difference is already part of the current contract and verification matrix F03. Record it explicitly; do not expand permissions during merge acceptance.

For each role collect the actual current workspace role, UI navigation and direct URL result, and authenticated API status for quota-management and representative system-management list/config endpoints. Summarize fields only; never output key or token values. Member write denial uses a dedicated synthetic target and must establish that its existing quota did not change. Admin can update a dedicated synthetic account through the existing authorized API even though UI entry is absent. Owner executes the actual UI quota-edit flow.

## Owner quota UI batch

1. Confirm 11 distinct disposable accounts from live API summary and default 10-row UI page, plus page count.
2. Edit a visible target via UI; verify successful save, refreshed current page and exact target balance through API.
3. Navigate to the second UI page, edit a different target, verify refreshed second-page row, and return to the first page to ensure prior target persists.
4. Search one exact disposable email, verify one matching target, edit and refetch. Clear search and confirm page reset and correct default pagination.
5. Switch page size using the exposed 10/30/50/100 selector and verify the 11-account view and return to default.

F04 integration configuration/test endpoint coverage requires real authorized test endpoints or credentials. Readonly config schema/permission behavior can run with no integrations configured; unavailable external OAuth/SSO/Email credentials remain explicit acceptance gaps. HTTP status alone does not prove a real integration.

No current-user account, app, provider configuration, price or quota is changed. Source repairs discovered by this batch are reported with a first failing transition before implementation scope expands.
