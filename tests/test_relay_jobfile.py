"""In-repo job definition: `.relay/<job>/job.yaml` and `<segment>/job.spec.yaml` (R2, R4, R24)."""
import pytest

from fleetmux.relay import jobfile
from relay_flow_support import write_job


def test_init_records_mark_dependent_blocked(tmp_path):
    write_job(tmp_path)
    doc = jobfile.load_job(tmp_path, 'demo')
    recs = jobfile.initial_records(doc, base='abc123')
    assert recs['M0']['state'] == 'pending'
    assert recs['M3']['state'] == 'pending'
    assert recs['M1']['state'] == 'blocked'
    assert recs['M1']['depends_on'] == ['M3']
    assert recs['merge']['state'] == 'blocked'
    assert recs['merge']['depends_on'] == ['M0', 'M1', 'M3']
    m3 = recs['M3']
    assert m3['needs'] == ['rust'] and m3['weight'] == 'heavy' and m3['prefer'] == 'laptop'
    assert m3['dir'] == '.relay/demo/M3'
    assert m3['base'] == 'abc123' and m3['owner'] is None and m3['epoch'] == 0
    assert m3['owners'] == [] and m3['seq'] == 0


@pytest.mark.parametrize('name', ['merge', 'merged', 'coordinator'])
def test_reserved_segment_names_refused(tmp_path, name):
    write_job(tmp_path, job_yaml=(
        'job: demo\nnodes: [laptop]\ntasks:\n  %s: {title: x, weight: light}\n' % name), segments=())
    with pytest.raises(jobfile.JobError, match='reserved'):
        jobfile.load_job(tmp_path, 'demo')


def test_unknown_dependency_refused(tmp_path):
    write_job(tmp_path, job_yaml=(
        'job: demo\nnodes: [laptop]\ntasks:\n  A: {title: x, weight: light, depends_on: [Z]}\n'), segments=())
    with pytest.raises(jobfile.JobError, match='unknown'):
        jobfile.load_job(tmp_path, 'demo')


def test_dependency_cycle_refused(tmp_path):
    write_job(tmp_path, job_yaml=(
        'job: demo\nnodes: [laptop]\ntasks:\n'
        '  A: {title: x, weight: light, depends_on: [B]}\n'
        '  B: {title: y, weight: light, depends_on: [A]}\n'), segments=())
    with pytest.raises(jobfile.JobError, match='cycle'):
        jobfile.load_job(tmp_path, 'demo')


def test_bad_weight_and_job_name_mismatch_refused(tmp_path):
    write_job(tmp_path, job_yaml='job: demo\nnodes: [laptop]\ntasks:\n  A: {title: x, weight: huge}\n',
              segments=())
    with pytest.raises(jobfile.JobError, match='weight'):
        jobfile.load_job(tmp_path, 'demo')
    write_job(tmp_path, job_yaml='job: other\nnodes: [laptop]\ntasks:\n  A: {title: x, weight: light}\n',
              segments=())
    with pytest.raises(jobfile.JobError, match='job'):
        jobfile.load_job(tmp_path, 'demo')


@pytest.mark.parametrize('line', ['repo: /Users/someone/proj', 'see ~/notes', 'cd /home/x/y',
                                  'path C:\\work\\x', 'cd $HOME/x'])
def test_absolute_paths_in_job_files_refused(tmp_path, line):
    root = write_job(tmp_path)
    (root / 'M3' / 'brief.md').write_text('# brief\n' + line + '\n')
    hits = jobfile.scan_abs_paths(tmp_path, 'demo')
    assert hits and hits[0][0] == '.relay/demo/M3/brief.md' and hits[0][1] == 2
    with pytest.raises(jobfile.JobError, match='absolute'):
        jobfile.check_job_tree(tmp_path, 'demo')


def test_clean_job_tree_passes_and_shebang_allowed(tmp_path):
    write_job(tmp_path)
    assert jobfile.scan_abs_paths(tmp_path, 'demo') == []
    jobfile.check_job_tree(tmp_path, 'demo')


def test_load_spec_reads_segment_spec(tmp_path):
    write_job(tmp_path)
    spec = jobfile.load_spec(tmp_path, 'demo', 'M3')
    assert spec['deliver']['test_cmd'] == ['/bin/bash', '.relay/demo/M3/gate.sh']
    assert spec['hours'] == 8


def test_load_spec_refuses_program_filled_keys(tmp_path):
    root = write_job(tmp_path)
    p = root / 'M3' / 'job.spec.yaml'
    p.write_text(p.read_text().replace('  test_timeout_s: 1800', '  test_timeout_s: 1800\n  repo: x'))
    with pytest.raises(jobfile.JobError, match='filled by the program'):
        jobfile.load_spec(tmp_path, 'demo', 'M3')


def test_missing_spec_is_an_error(tmp_path):
    write_job(tmp_path, segments=('M0',))
    with pytest.raises(jobfile.JobError, match='job.spec.yaml'):
        jobfile.load_spec(tmp_path, 'demo', 'M3')
