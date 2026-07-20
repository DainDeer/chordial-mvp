"""the native workspace: chordial's own system of record for plans, goals,
tasks, cycles, wins, check-ins, notes, and occasions (replaces notion).
see docs/NATIVE_WORKSPACE_DESIGN.md."""
from src.services.workspace.store import WorkspaceStore, ResolutionResult

_store = None


def get_store() -> WorkspaceStore:
    """process-wide store instance (it's stateless; this is just convention)."""
    global _store
    if _store is None:
        _store = WorkspaceStore()
    return _store
