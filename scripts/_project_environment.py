"""Select an isolated checkout for nested project Python commands."""
import os
from pathlib import Path


def project_environment(checkout: Path) -> dict[str, str]:
    root = Path(checkout).resolve()
    env = os.environ.copy()
    env.update(UV_PROJECT_ENVIRONMENT=str(root / '.venv'),
               PYTHONPATH=os.pathsep.join((str(root / 'src'), str(root / 'scripts'))),
               PYTHONSAFEPATH='1')
    env.setdefault('UV_CACHE_DIR', str(root / '.uv-cache'))
    env.pop('UV_NO_SYNC', None)
    env.pop('UV_NO_PROJECT', None)
    env.pop('UV_WORKING_DIR', None)
    return env
