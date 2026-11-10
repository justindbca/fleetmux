"""Project-local private Redis paths shared by startup, hooks and control.

No processes or directories are created on import. The config pins each run's
endpoint; existing legacy /tmp fleet configs remain usable without migration.
"""
import os
from pathlib import Path
import re
import stat
import sys


DEFAULT_ROOT = Path(__file__).resolve().parents[1] / '.runtime' / 'redis'
# sockaddr_un.sun_path includes a terminating NUL: 104 bytes on macOS, 108 on Linux.
SOCKET_PATH_LIMIT = 103 if sys.platform == 'darwin' else 107
LEGACY_SOCKET = re.compile(r'/tmp/cmux-fleet-[A-Za-z0-9_.-]+\.sock\Z')
INSTANCE_ID = re.compile(r'[0-9a-f]{8,32}\Z')
MANAGED_FIELDS = ('redis_runtime_root', 'redis_runtime_dir', 'redis_instance_id')


def _absolute(value):
    if not isinstance(value, (str, os.PathLike)) or not str(value):
        raise ValueError('private Redis paths must be absolute filesystem paths')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or '\x00' in str(path):
        raise ValueError('private Redis paths must be absolute without traversal')
    return path


def _length(socket):
    size = len(os.fsencode(socket))
    if size > SOCKET_PATH_LIMIT:
        raise ValueError('private Redis Unix socket path is too long (%d bytes; limit %d); '
                         'choose a shorter --redis-runtime-dir; no /tmp fallback' %
                         (size, SOCKET_PATH_LIMIT))


def paths(root, instance_id):
    """Describe a fresh, short per-instance directory without filesystem writes."""
    if not isinstance(instance_id, str) or not INSTANCE_ID.fullmatch(instance_id):
        raise ValueError('private Redis instance id must be 8-32 lowercase hex characters')
    root = Path(root).expanduser().absolute()
    if root.is_symlink():
        raise ValueError('private Redis runtime root must not be a symlink')
    root = root.resolve()
    directory = root / instance_id
    cfg = {'redis_runtime_root': str(root), 'redis_runtime_dir': str(directory),
           'redis_instance_id': instance_id, 'redis_socket': str(directory / 'redis.sock')}
    validate_socket(cfg)
    return cfg


def validate_socket(cfg):
    """Accept only the pinned managed endpoint or a legacy fleet-specific socket.

    Partial/invalid managed metadata must never downgrade to the legacy path.
    This validates routing, not OS isolation from other processes of this user.
    """
    socket = _absolute(cfg.get('redis_socket'))
    if any(field in cfg for field in MANAGED_FIELDS):
        root = _absolute(cfg.get('redis_runtime_root'))
        directory = _absolute(cfg.get('redis_runtime_dir'))
        instance_id = cfg.get('redis_instance_id')
        if (not isinstance(instance_id, str) or not INSTANCE_ID.fullmatch(instance_id)
                or directory != root / instance_id or socket != directory / 'redis.sock'):
            raise ValueError('private Redis socket must match its configured instance directory')
        # Detect symlink escape, including a swapped instance directory after startup.
        if root.resolve() != root or directory.resolve() != directory or socket.is_symlink():
            raise ValueError('private Redis runtime path must not escape through symlinks')
    elif not LEGACY_SOCKET.fullmatch(str(socket)) or socket.is_symlink():
        raise ValueError('private cmux-fleet Unix socket required')
    _length(socket)
    return socket


def prepare(root, instance_id):
    """Reserve an owner-only directory; never reuse/delete another run's files."""
    cfg = paths(root, instance_id)  # Length/containment checks before any mkdir or spawn.
    root = Path(cfg['redis_runtime_root'])
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = root.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('private Redis runtime root must belong to the current user '
                         'and have owner-only permissions (700): ' + str(root))
    Path(cfg['redis_runtime_dir']).mkdir(mode=0o700, exist_ok=False)
    validate_socket(cfg)
    return cfg
