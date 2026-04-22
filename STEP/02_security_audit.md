# Security Audit Steps

- [x] Review `TODO.md` and choose the security audit as the next concrete step.
- [x] Inspect the highest-risk code paths: bash execution, permission checks, sandbox wrapping, and file writes.
- [x] Fix any clear permission-boundary bugs found during the audit.
- [x] Add regression tests for the permission fixes.
- [x] Record the audit outcome and remaining risks.

## Result

- Fixed permission-boundary checks for dream mode and plan mode.
- Added regression tests in `tests/test_permissions.py`.
- Recorded the audit status and remaining hardening work in `SECURITY_AUDIT.md`.
