"""Shared building blocks for the jay-withers apps on Container Apps.

Each module was once copied between career, finances and gym-log; a fix now
lands here once and reaches every app through a version bump. Install with the
extras a module needs: `appkit[azure]` for `keyvault` and `appkit[backup]` for
running a backup. `headers`, `passcode` and `selfcheck` need only the core.
"""
