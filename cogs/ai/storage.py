import json
import os
import tempfile
import threading

_locks = {}
_locks_guard = threading.Lock()


def _lock_for(path):
    with _locks_guard:
        if path not in _locks:
            _locks[path] = threading.Lock()
        return _locks[path]


def _load_unlocked(path, default):
    if not os.path.exists(path):
        return default() if callable(default) else default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default() if callable(default) else default


def _save_unlocked(path, data):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path, default):
    lock = _lock_for(path)
    with lock:
        return _load_unlocked(path, default)


def save_json(path, data):
    lock = _lock_for(path)
    with lock:
        _save_unlocked(path, data)


def update_json(path, mutator, default):
    """Load, mutate in place, and save while holding the file's lock.
    Prevents lost updates when multiple threads write the same file."""
    lock = _lock_for(path)
    with lock:
        data = _load_unlocked(path, default)
        mutator(data)
        _save_unlocked(path, data)
        return data