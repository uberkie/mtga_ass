"""Local filesystem discovery for the standard MTG Arena log."""
from __future__ import annotations

import os
from pathlib import Path


_ARENA_LOG_SUFFIX = Path('AppData') / 'LocalLow' / 'Wizards Of The Coast' / 'MTGA' / 'Player.log'


def default_arena_log(
    *, home: Path | None = None, windows_users_root: Path | None = None,
) -> Path:
    """Find the standard Arena log, including a Windows profile mounted in WSL.

    A path under the current profile is preferred. A mounted Windows profile
    is used only when it contains the sole discovered Arena log; multiple
    profiles are ambiguous and must not silently select another user's log.
    If none exists, return the conventional path under the current home
    directory so CLI errors remain actionable.
    """
    home = Path.home() if home is None else Path(home)
    windows_users_root = (Path('/mnt/c/Users') if windows_users_root is None
                          else Path(windows_users_root))
    home_candidate = home / _ARENA_LOG_SUFFIX
    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        local_candidate = (Path(local_app_data).parent / 'LocalLow' /
                           'Wizards Of The Coast' / 'MTGA' / 'Player.log')
        if local_candidate.is_file():
            return local_candidate
    if home_candidate.is_file():
        return home_candidate
    windows_candidates = []
    if windows_users_root.is_dir():
        windows_candidates.extend(user / _ARENA_LOG_SUFFIX for user in windows_users_root.iterdir()
                                  if user.is_dir())
    existing: dict[str, Path] = {}
    for candidate in windows_candidates:
        try:
            if candidate.is_file():
                existing[os.path.normcase(str(candidate.resolve()))] = candidate
        except OSError:
            continue
    if len(existing) == 1:
        return next(iter(existing.values()))
    return home_candidate
