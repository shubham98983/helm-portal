# Security notes

- Helm is read-only: GitHub integration uses GET requests only. It never approves, merges, comments, or writes repository data.
- A single configured GitHub token is shared by Helm's server-side requests. Anyone with Helm access can see the project data exposed by that token, so grant it read-only, least-privilege access and protect the Helm host.
- GitHub titles, names, branches, labels, and issue content are untrusted. The frontend renders them as text; outgoing GitHub links are limited to `https://github.com/`.
- Passwords use scrypt; session identifiers are random, stored server-side, expire after eight hours, and are delivered in HttpOnly SameSite cookies. Login attempts are rate-limited. Anonymous access is a development-only opt-in.
- The audit ledger applies configured approver/check rules and is advisory. It does not prove intent or replace human review. Admin merge overrides and other privileged GitHub actions are not detectable from this evidence.
- SQLite and config files contain sensitive operational data and must be protected with appropriate filesystem and volume permissions. Copilot is optional; Helm makes bounded GET requests without forwarding the GitHub token.
