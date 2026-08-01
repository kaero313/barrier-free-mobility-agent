"""Persistence adapters.

PostgreSQL is intentionally not imported here so memory-only environments do not load an
optional database driver while the repository integration is being wired.
"""
