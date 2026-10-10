"""Exercise the dependency-free launcher without installing or contacting uv."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runner(tmp_path):
    checkout = tmp_path / 'checkout with spaces'
    scripts = checkout / 'scripts'
    scripts.mkdir(parents=True)
    launcher = scripts / 'project_python'
    shutil.copy2(ROOT / 'scripts/project_python', launcher)
    launcher.chmod(0o755)
    (checkout / 'pyproject.toml').write_text('[project]\nname="fixture"\n')
    (checkout / 'uv.lock').write_text('version = 1\n')
    tools = tmp_path / 'tools'
    tools.mkdir()
    uv = tools / 'uv'
    uv.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
from pathlib import Path
Path(os.environ['RUNNER_RECORD']).write_text(json.dumps({
    'argv': sys.argv[1:], 'cwd': os.getcwd(),
    'pythonpath': os.environ.get('PYTHONPATH'),
    'environment': os.environ.get('UV_PROJECT_ENVIRONMENT'),
    'working_dir': os.environ.get('UV_WORKING_DIR'),
    'cache': os.environ.get('UV_CACHE_DIR'),
    'no_sync': os.environ.get('UV_NO_SYNC'),
    'no_project': os.environ.get('UV_NO_PROJECT'),
}))
if os.environ.get('RUNNER_EXEC') == '1':
    os.execv(sys.executable, [sys.executable, *sys.argv[sys.argv.index('python') + 1:]])
print('owned uv reached')
raise SystemExit(int(os.environ.get('RUNNER_EXIT', '0')))
''')
    uv.chmod(0o755)
    record = tmp_path / 'record.json'
    env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ['PATH'],
               RUNNER_RECORD=str(record), PYTHONPATH='/wrong/checkout/src',
               UV_PROJECT_ENVIRONMENT='/wrong/checkout/.venv',
               UV_NO_SYNC='1', UV_NO_PROJECT='1', UV_WORKING_DIR='/wrong/checkout')
    env.pop('UV_CACHE_DIR', None)
    return checkout, launcher, record, env


@pytest.mark.parametrize('arguments', [
    ['-c', 'import skybuild; print(skybuild.__file__)'],
    ['-m', 'pytest', '/a path/tests', '-q'],
    ['/a path/cli.py', '--help', 'literal $HOME; `command`'],
])
def test_exact_checkout_arguments_and_caller_directory(runner, tmp_path, arguments):
    checkout, launcher, record, env = runner
    result = subprocess.run([str(launcher), *arguments], cwd=tmp_path, env=env,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    observed = json.loads(record.read_text())
    assert observed == {
        'argv': ['run', '--locked', '--extra', 'test', '--project', str(checkout), 'python', '-P', *arguments],
        'cwd': str(tmp_path), 'pythonpath': str(checkout / 'src') + os.pathsep + str(checkout / 'scripts'),
        'environment': str(checkout / '.venv'), 'working_dir': None, 'no_sync': None, 'no_project': None,
        'cache': str(checkout / '.uv-cache'),
    }
    assert result.stdout == 'owned uv reached\n'


def test_caller_source_cannot_shadow_checkout_and_script_helpers_work(runner, tmp_path):
    checkout, launcher, _, env = runner
    package = checkout / 'src/skybuild'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('marker = "owned checkout"\n')
    (checkout / 'scripts/owned_helper.py').write_text('marker = "owned helper"\n')
    (tmp_path / 'skybuild.py').write_text('raise AssertionError("wrong checkout imported")\n')
    result = subprocess.run([str(launcher), '-c',
                             'import skybuild, owned_helper; print(skybuild.marker, owned_helper.marker)'],
                            cwd=tmp_path, env=dict(env, RUNNER_EXEC='1'),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'owned checkout owned helper\n'


def test_real_uv_cannot_redirect_relative_script_to_inherited_working_dir(tmp_path):
    if shutil.which('uv') is None:
        pytest.skip('real uv is unavailable')
    caller = tmp_path / 'caller'
    foreign = tmp_path / 'foreign'
    (caller / 'scripts').mkdir(parents=True)
    (foreign / 'scripts').mkdir(parents=True)
    (caller / 'scripts/selected.py').write_text('print("CALLER_SCRIPT_EXECUTED")\n')
    (foreign / 'scripts/selected.py').write_text('print("FOREIGN_SCRIPT_EXECUTED")\n')
    result = subprocess.run(
        [str(ROOT / 'scripts/project_python'), 'scripts/selected.py'], cwd=caller,
        env=dict(os.environ, UV_WORKING_DIR=str(foreign), UV_CACHE_DIR=str(tmp_path / 'uv cache')),
        text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'CALLER_SCRIPT_EXECUTED\n'


def test_explicit_writable_cache_is_preserved(runner, tmp_path):
    _, launcher, record, env = runner
    cache = str(tmp_path / 'shared cache')
    result = subprocess.run([str(launcher), '--help'], cwd=tmp_path,
                            env=dict(env, UV_CACHE_DIR=cache),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(record.read_text())['cache'] == cache


def test_uv_failure_is_the_launchers_exit_status(runner, tmp_path):
    _, launcher, record, env = runner
    result = subprocess.run([str(launcher), '-c', 'raise AssertionError("must not run")'],
                            cwd=tmp_path, env=dict(env, RUNNER_EXIT='37'),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 37
    assert record.exists()
    assert 'AssertionError' not in result.stderr


def test_missing_lock_stops_before_uv(runner, tmp_path):
    checkout, launcher, record, env = runner
    (checkout / 'uv.lock').unlink()
    result = subprocess.run([str(launcher), '--help'], cwd=tmp_path, env=env,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 2
    assert 'expected pyproject.toml and uv.lock' in result.stderr
    assert not record.exists()
