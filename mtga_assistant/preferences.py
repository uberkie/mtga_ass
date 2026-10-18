"""Small user-local preferences for the MTGA overlay."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile


def match_model_preference_path() -> Path:
    """Return the per-user file containing only the last ready model name."""
    if os.name == 'nt':
        root = os.environ.get('APPDATA')
        base = Path(root) if root else Path.home() / 'AppData' / 'Roaming'
    else:
        root = os.environ.get('XDG_CONFIG_HOME')
        base = Path(root) if root else Path.home() / '.config'
    return base / 'mtga-assistant' / 'match-model'


def _clean_model_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 128 or any(not char.isprintable() for char in value):
        return None
    return value


def load_match_model(*, path: Path | None = None) -> str:
    """Load the last verified model name, returning empty when unavailable."""
    source = match_model_preference_path() if path is None else Path(path)
    try:
        content = source.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return ''
    return _clean_model_name(content) or ''


def save_match_model(value: object, *, path: Path | None = None) -> bool:
    """Atomically save just a validated model name for the next GUI launch."""
    model = _clean_model_name(value)
    if model is None:
        return False
    destination = match_model_preference_path() if path is None else Path(path)
    temporary_path = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
                'w', encoding='utf-8', dir=destination.parent,
                prefix=destination.name + '.', suffix='.tmp', delete=False) as stream:
            temporary_path = Path(stream.name)
            stream.write(model + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, destination)
        return True
    except OSError:
        return False
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
