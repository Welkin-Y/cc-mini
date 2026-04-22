# Security Audit

## Completed

- Fixed dream-mode write isolation in [src/core/permissions.py](/home/welkin/Dev/cc-mini/src/core/permissions.py):
  - previous logic used `realpath(...).startswith(...)`
  - this allowed false positives such as `/tmp/memory-escaped` matching `/tmp/memory`
  - current logic uses `os.path.commonpath(...)` for real directory-boundary checks
- Fixed plan-mode plan-file matching in [src/core/permissions.py](/home/welkin/Dev/cc-mini/src/core/permissions.py):
  - previous logic compared raw path strings
  - current logic compares resolved paths
- Added regression coverage in [tests/test_permissions.py](/home/welkin/Dev/cc-mini/tests/test_permissions.py)

## Remaining Risks

- `BashTool` still executes with `shell=True` by design in [src/tools/bash.py](/home/welkin/Dev/cc-mini/src/tools/bash.py).
  - This is expected for an agent shell tool, but it keeps command execution high risk outside sandboxed mode.
- File tools do not enforce a repository-root allowlist on their own.
  - Protection currently relies on the permission layer and sandbox settings, not the tool implementations themselves.
- Read-before-write tracking is process-global via `FileEditTool._read_files`.
  - This is functional, but not a security boundary.

## Recommended Next Hardening

1. Add path policy enforcement for `Read`, `Edit`, and `Write` so sensitive locations can be denied centrally.
2. Audit unsandboxed `Bash` flows and document when `dangerously_disable_sandbox` is acceptable.
3. Add tests for symlink-based path escapes around dream mode and plan mode.
