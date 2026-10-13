from pathlib import Path

import duckdb
import pytest
import yaml
from typer.testing import CliRunner

from rollout.cli import app
from rollout.discovery import discover_seeders
from rollout.execution import ExecutionStatus
from rollout.models import ModelId
from rollout.seeders import execute_seeder


def write_seeder(project, name, body, outputs):
    path = project / 'seeders' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('def run(connection, staging_dir):\n' + ''.join(
        '    ' + line + '\n' for line in body.splitlines()
    ))
    path.with_suffix('.yaml').write_text(yaml.safe_dump({'outputs': outputs}, sort_keys=False))


def test_project_seeder_before_model_and_shared_connection(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    (project / 'models' / 'silver').mkdir(parents=True)
    (project / 'models' / 'silver' / 'orders.sql').write_text(
        'SELECT orders.id FROM staging.orders AS orders JOIN session_marker USING (id)'
    )
    write_seeder(project, 'source.py',
                 "connection.execute('CREATE TEMP TABLE session_marker AS SELECT 7 AS id')\n"
                 "return {'orders': connection.sql('SELECT 7 AS id')}", {'orders': {}})
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ['run', '--project-dir', str(project)])
    assert result.exit_code == 0, result.output
    assert '🟢 seeders/source.py' in result.stdout
    assert '🟢 staging.orders' in result.stdout
    assert '🟢 silver.orders' in result.stdout
    with duckdb.connect(str(tmp_path / '.rollout' / 'warehouse.duckdb')) as connection:
        assert connection.sql('SELECT * FROM silver.orders').fetchall() == [(7,)]
    report = next((tmp_path / '.rollout' / 'runs').glob('*.md')).read_text()
    assert report.index('## Seeder results') < report.index('## Seeder outputs') < report.index('## Model results')
    graph = report.split('```mermaid')[1]
    assert 'source.py' not in graph
    assert 'classDef external stroke-dasharray' in graph
    assert len(list((tmp_path / '.rollout' / 'models').rglob('*.sql'))) == 1
    dag = CliRunner().invoke(app, ['dag', '--project-dir', str(project)])
    assert dag.exit_code == 0, dag.output
    assert 'staging.orders' in dag.stdout


@pytest.mark.parametrize('failure', ['python', 'quality', 'warning'])
def test_failure_continuation_order_and_report(tmp_path, monkeypatch, failure):
    project = tmp_path / 'project'
    (project / 'models').mkdir(parents=True)
    (project / 'models' / 'report.sql').write_text('SELECT * FROM staging.later')
    marker = tmp_path / 'order.txt'
    prefix = f"from pathlib import Path\np = Path({str(marker)!r})\np.write_text(p.read_text() + 'a' if p.exists() else 'a')\n"
    body = prefix + ("raise ValueError('source failed')" if failure == 'python' else
                     "return {'bad': connection.sql('SELECT 1 AS id'), 'good': connection.sql('SELECT 2 AS id')}")
    outputs = {'bad': {'quality': [{'type': 'row_count', 'min': 2,
                                  'severity': 'WARNING' if failure == 'warning' else 'ERROR'}]}}
    if failure != 'python':
        outputs['good'] = {}
    write_seeder(project, 'a.py', body, outputs)
    write_seeder(project, 'z.py',
                 f"from pathlib import Path\np = Path({str(marker)!r})\np.write_text(p.read_text() + 'z')\n"
                 "return {'later': connection.sql('SELECT 3 AS id')}", {'later': {}})
    monkeypatch.chdir(tmp_path)
    output = tmp_path / 'report.md'
    result = CliRunner().invoke(app, ['run', '--project-dir', str(project), '-o', str(output)])
    assert result.exit_code == (0 if failure == 'warning' else 1), result.output
    assert marker.read_text() == 'az'
    assert '🟢 seeders/z.py' in result.stdout
    assert '🟢 public.report' in result.stdout
    report = output.read_text()
    for section in ('Seeder results', 'Seeder outputs', 'Model results', 'Quality details', 'Failure details'):
        assert f'## {section}' in report
    if failure == 'python':
        assert 'failed before outputs' in report
        assert 'source failed' in report
        assert '| staging.bad |' not in report
    else:
        assert '| staging.good | SUCCESS |' in report
        assert ('| staging.bad | SUCCESS |' if failure == 'warning' else '| staging.bad | FAILED |') in report
        assert '| row_count | 1 | >= 2 | FAIL |' in report
    with duckdb.connect(str(tmp_path / '.rollout' / 'warehouse.duckdb')) as connection:
        assert connection.sql('SELECT * FROM public.report').fetchall() == [(3,)]


def test_models_required_and_old_option_removed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ['run'])
    assert result.exit_code == 1
    assert 'Models directory does not exist' in result.stderr
    for command in ('run', 'dag'):
        result = CliRunner().invoke(app, [command, '--models-dir', '.'])
        assert result.exit_code != 0
        assert 'No such option' in result.output


@pytest.mark.parametrize('fails', [False, True])
def test_seeder_timing_and_cleanup(tmp_path, monkeypatch, fails):
    from rollout import seeders

    marker = tmp_path / 'temporary.txt'
    write_seeder(tmp_path, 'source.py',
                 f"from pathlib import Path\nPath({str(marker)!r}).write_text(str(staging_dir))\n" +
                 ("raise ValueError('timed failure')" if fails else
                  "return {'orders': connection.sql('SELECT 1 AS id')}"), {'orders': {}})
    ticks = iter([10.0, 11.0, 14.0, 20.0])
    monkeypatch.setattr(seeders, 'perf_counter', lambda: next(ticks))
    with duckdb.connect() as connection:
        result = execute_seeder(discover_seeders(tmp_path / 'seeders')[0], connection)
    assert result.execution_duration == 3.0
    assert result.total_duration == 10.0
    assert result.status is (ExecutionStatus.FAILED if fails else ExecutionStatus.SUCCESS)
    assert not Path(marker.read_text()).exists()
    assert list(result.outputs) == ([] if fails else [ModelId('staging', 'orders')])
