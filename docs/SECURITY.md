# Security model and deployment checklist

## Implemented baseline

- Argon2id password hashing through `argon2-cffi`.
- Short-lived signed JWT access tokens and opaque refresh tokens stored as hashes; refresh token rotation on use.
- TOTP multi-factor authentication with one-time recovery codes. TOTP secrets are Fernet-encrypted at rest using a key derived from `APP_SECRET_KEY`; protect and back up that key securely. `MFA_REQUIRED=true` enforces enrollment for all authenticated roles by default.
- Repeated sign-in attempt counters and temporary account lockout.
- Redis-backed IP/account rate limits. `RATE_LIMIT_FAIL_CLOSED=true` returns a service error when Redis is unavailable rather than silently dropping protection.
- Server-side role checks on protected API routes.
- Audit records for setup, authentication, user changes, report decisions and integration actions.
- Security response headers, restricted CORS, upload size/MIME/signature validation and image re-encoding to remove EXIF metadata.
- Local evidence storage outside the source tree; interchangeable S3-compatible storage adapter.
- Admin controls to deactivate accounts and reset another user's MFA; both actions are audited. Deactivation revokes that user's refresh tokens.
- Database/evidence backups with SHA-256 checksums.

## Before exposing any instance

1. Set `APP_ENV=production`, `DEMO_MODE=false`, `MFA_REQUIRED=true` and `RATE_LIMIT_FAIL_CLOSED=true`.
2. Generate unique strong `APP_SECRET_KEY`, `JWT_SECRET_KEY`, `POSTGRES_PASSWORD` and `REDIS_PASSWORD` values. Do not reuse secrets. Back up `APP_SECRET_KEY`; losing it makes enrolled MFA secrets unavailable.
3. Set `chmod 600 .env`; do not commit `.env`, dumps, evidence or backups.
4. Remove/demo-seed data only in a newly created production database; do not expose the shared demo password.
5. Use HTTPS through a proper tunnel or TLS reverse proxy. Browser push requires a secure context.
6. Keep database and Redis ports un-published. Expose only the reverse proxy. Do not publish port 8000 or 8080 directly to all interfaces.
7. Restrict Quick Tunnel links; by default, anyone with the random URL can access the app. A Quick Tunnel is temporary testing ingress, not a production access gateway.
8. Configure SMTP and VAPID only if those channels are required. Store provider secrets only in `.env`/secret management.
9. Back up the database and evidence, validate the backup, and copy it off-host. A backup on the same disk is not disaster recovery.
10. Review and test account deactivation, MFA reset, recovery-code procedures, retention, restore and incident response before collecting real reports.

## Important limitations

- There is no third-party penetration-test report or formal compliance certification in this package.
- There is no external SIEM integration, malware-scanning daemon, email-verification flow or self-service password reset workflow in this first release.
- PDF content is signature-checked but not sanitized; do not treat uploaded PDFs as trusted executable-free documents. Consider adding antivirus/content-disarm-and-reconstruction in a hardened deployment.
- Citizen-provided text may itself contain personal data. Image EXIF is removed, but text content needs human moderation/redaction. Retention cleanup removes old evidence and the report's submitter link; it does not rewrite all personal details that may appear in the text body.
- Access tokens passed to the WebSocket endpoint as a query parameter should be short-lived and reverse-proxy access logs should not record query strings. The bundled Caddy configuration does not enable request access logging by default.
- Local storage and database backups contain potentially sensitive information. Apply filesystem permissions and off-host encryption appropriate to your threat model.
