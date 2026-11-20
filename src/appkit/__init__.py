"""Shared building blocks for the jay-withers apps on Container Apps.

Each module was once copied between career, finances and gym-log; a fix now
lands here once and reaches every app through a version bump. Install with the
extras a module needs: `appkit[web]` for `headers`, `health` and `passcode`,
`appkit[azure]` for `keyvault` and `appkit[backup]` for running a backup.
`integrity`, `manifest` and `selfcheck` need only the core, which has no dependencies. `appkit[e2e]`
(pytest-playwright) is for `testing`, in an app's dev extra.
"""
