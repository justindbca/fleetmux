"""P8p: `start --security PATH` / `--no-security`, finding ``config_dir()/security.yaml``, pinning the policy into the
run config.json, and `start --resume` (brief v2.2 §8.1; core-2/P8-start-plan.md §2-§4.2, option A).

Two tests only (supervisor M7/M25): T1 the refusals of `start`, all before any provisioning; T2 every `--resume`
branch. Fakes only: ``test_pi.mock_start`` (fake cmux / Redis provisioning), a resume driven up to its config save
(the next step, ``manager_for``, raises a sentinel), the private test Redis for the running-fleet half. The config
dir is a temp dir; no network, no real credential, no listener.

A refusal must be ours, not argparse's: every refusal asserts a text ``SystemExit`` naming security (an unknown
``--security`` flag exits with code 2 and would otherwise look like a refusal).
"""
import builtins
import os
import pathlib
from pathlib import Path

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import fx  # noqa: F401  (fixture: a v2 service fleet)
from test_pi import mock_start
from cmux_fleet import channels, cmux_manager, fleet
from cmux_fleet.channels import security

SECRET = 'zq9x7-never-echo'  # written into bad files; must never show up in an error


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    d = tmp_path / 'cfgdir'
    monkeypatch.setenv(channels.CONFIG_ENV, str(d))
    return d


def ours(exc):
    """A SystemExit raised by the security wiring (a text naming security), not argparse's exit 2."""
    code = exc.value.code
    assert isinstance(code, str), 'expected a refusal text, got exit code ' + repr(code)
    assert 'security' in code
    assert SECRET not in code
    return code


def defaults():
    policy, errors = security.parse_security('')
    assert not errors
    return policy


def deny_reads_of(monkeypatch, target, error=PermissionError):
    """Reading ``target`` raises ``error`` (an unreadable file without chmod: root reads a 000 file anyway)."""
    target = str(Path(target).resolve())
    real_read_text, real_read_bytes, real_open = pathlib.Path.read_text, pathlib.Path.read_bytes, builtins.open

    def hit(p):
        try:
            return str(Path(p).resolve()) == target
        except (TypeError, OSError):
            return False

    def read_text(self, *a, **k):
        if hit(self):
            raise error(13, 'Permission denied', str(self))
        return real_read_text(self, *a, **k)

    def read_bytes(self):
        if hit(self):
            raise error(13, 'Permission denied', str(self))
        return real_read_bytes(self)

    def open_(file, *a, **k):
        if isinstance(file, (str, os.PathLike)) and hit(file):
            raise error(13, 'Permission denied', str(file))
        return real_open(file, *a, **k)
    monkeypatch.setattr(pathlib.Path, 'read_text', read_text)
    monkeypatch.setattr(pathlib.Path, 'read_bytes', read_bytes)
    monkeypatch.setattr(builtins, 'open', open_)


# ====================================================================== T1: start refusals
class Spies:
    """Counts every provisioning / Redis / cmux step of `start` (each spied on its own)."""

    def __init__(self, monkeypatch, runs):
        self.calls, self.runs = [], runs

        def wrap(owner, name):
            real = getattr(owner, name)

            def spy(*a, **k):
                self.calls.append(name)
                return real(*a, **k)
            monkeypatch.setattr(owner, name, spy)
        wrap(fleet.redis_runtime, 'prepare')    # provisioning
        wrap(cmux_manager, 'cmux_available')    # cmux
        wrap(cmux_manager, 'CmuxSessionManager')
        wrap(fleet, 'connect')                  # Redis
        wrap(fleet, 'start_redis')

    def reset(self):
        self.calls.clear()
        self.before = self.listing()

    def listing(self):
        return sorted(p.name for p in self.runs.iterdir()) if self.runs.is_dir() else []

    def assert_untouched(self):
        assert self.calls == [], 'refusal reached ' + ', '.join(self.calls)
        assert self.listing() == self.before, 'a run directory was created'


def test_start_refuses_bad_or_unreadable_security_before_provisioning(tmp_path, monkeypatch, config_dir):
    p = fleet.cli()
    assert p.parse_args(['start', '--security', 'x.yaml']).security == 'x.yaml'
    assert p.parse_args(['start', '--no-security']).no_security is True
    args, _ = mock_start(tmp_path, monkeypatch)
    spies = Spies(monkeypatch, tmp_path / 'runs')
    bad = tmp_path / 'bad.yaml'
    cases = {
        'unknown key': 'zz_unknown_key: ' + SECRET + '\n',
        'bad value': 'sandbox: {mode: seatbelt-' + SECRET + '}\n',
        'yaml syntax': 'confirm: {wait_s: [' + SECRET + '\n',
    }
    for why, text in cases.items():
        bad.write_text(text)
        spies.reset()
        with pytest.raises(SystemExit) as exc:
            fleet.main(args + ['--security', str(bad)])
        msg = ours(exc)
        assert str(bad.resolve()) in msg or str(bad) in msg, why
        if why == 'unknown key':
            assert 'zz_unknown_key' in msg
        if why == 'bad value':
            assert 'sandbox.mode' in msg
        spies.assert_untouched()
    # the default path, found without a flag, takes the same road
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / 'security.yaml').write_text(cases['unknown key'])
    spies.reset()
    with pytest.raises(SystemExit) as exc:
        fleet.main(args)
    ours(exc)
    spies.assert_untouched()
    (config_dir / 'security.yaml').unlink()
    # unreadable: injected PermissionError, never chmod 000 (a privileged user reads it anyway)
    good = tmp_path / 'good.yaml'
    good.write_text('')
    with monkeypatch.context() as m:
        deny_reads_of(m, good)
        spies.reset()
        with pytest.raises(SystemExit) as exc:
            fleet.main(args + ['--security', str(good)])
        ours(exc)
        spies.assert_untouched()
    # a directory is not a policy file
    spies.reset()
    with pytest.raises(SystemExit) as exc:
        fleet.main(args + ['--security', str(tmp_path)])
    ours(exc)
    spies.assert_untouched()
    # both flags: argparse refuses (exit 2), still nothing provisioned
    spies.reset()
    with pytest.raises(SystemExit) as exc:
        fleet.main(args + ['--security', str(good), '--no-security'])
    assert exc.value.code == 2
    spies.assert_untouched()
    # confirm.wait_s is checked against THIS run's stall_window, not a fixed 300
    waits = tmp_path / 'waits.yaml'
    waits.write_text('confirm: {wait_s: 200}\n')
    spies.reset()
    with pytest.raises(SystemExit) as exc:
        fleet.main(args + ['--security', str(waits), '--stall-window', '150'])
    assert 'wait_s' in ours(exc)
    spies.assert_untouched()
    fleet.main(args + ['--security', str(waits), '--stall-window', '300'])
    cfg = fleet.load(tmp_path / 'latest.json')
    assert cfg['security']['confirm']['wait_s'] == 200
    assert cfg['security_source'] == str(waits.resolve()) and Path(cfg['security_source']).is_absolute()


# ====================================================================== T2: resume branches
class Reached(Exception):
    """resume got past its config save (manager_for is the next step)."""


def resumable_run(tmp_path, name, **extra):
    run = tmp_path / name
    (run / 'workspace').mkdir(parents=True)
    (run / 'redis').mkdir()
    cfg = {'run': str(run), 'transport_version': 2, 'mode': 'service', 'lifecycle': 'service',
           'supervisor_cli': 'pi', 'worker_cli': 'pi', 'workers': ['worker-1'], 'stall_window': 300,
           'backend': 'cmux', 'codex_queue': False, 'codex_version': None, 'redis_runtime_dir': str(run / 'redis'),
           'allow_development_v2': True, 'allow_unsandboxed_pi': True}
    cfg.update(extra)
    fleet.save(run / 'config.json', cfg)
    return run


def drive_resume(monkeypatch, run, *flags):
    """`start --resume <run> [flags]` with fakes up to the config save. Returns the saved config, or raises the
    SystemExit of a refusal."""
    monkeypatch.setattr(fleet, 'daemon_running', lambda run_: False)
    monkeypatch.setattr(fleet, 'resolve_backend', lambda b: (b, ''))
    monkeypatch.setattr(fleet, 'backends_check', lambda b: True)
    monkeypatch.setattr(fleet, 'sweep_orphan_redis', lambda keep=None: None)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: object())

    def stop_here(cfg):
        raise Reached
    monkeypatch.setattr(fleet, 'manager_for', stop_here)
    with pytest.raises(Reached):
        fleet.main(['start', '--resume', str(run), *flags])
    return fleet.load(run / 'config.json')


def refused(monkeypatch, run, *flags):
    before = (run / 'config.json').read_bytes()
    with pytest.raises(SystemExit) as exc:
        drive_resume(monkeypatch, run, *flags)
    ours(exc)
    assert (run / 'config.json').read_bytes() == before, 'a refused resume must leave config.json as it was'


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_resume_keeps_or_rereads_pinned_security_never_silently_off(fx, tmp_path, monkeypatch, config_dir):  # noqa: F811
    base = defaults()
    # 1. fixed while the run lives: editing the file changes nothing until --resume
    f = fx
    src = write(tmp_path / 'live.yaml', '')
    f.cfg.update(security=base, security_source=str(src.resolve()))
    fleet.save(f.path, dict(fleet.load(f.path), security=base, security_source=str(src.resolve())))
    src.write_text('links: {allow: ["*"]}\n')
    f.roster_tick()
    f.tick()
    assert f.cfg['security'] == base and fleet.load(f.path)['security'] == base
    # 2. a source and no flag: re-read it; gone or broken -> refused, config unchanged
    src = write(tmp_path / 'src.yaml', '')
    run = resumable_run(tmp_path, 'r-src', security=base, security_source=str(src.resolve()))
    src.write_text('links: {allow: ["*"]}\n')
    cfg = drive_resume(monkeypatch, run)
    assert cfg['security']['links']['allow'] == ['*'] and cfg['security_source'] == str(src.resolve())
    src.write_text('sandbox: {mode: ' + SECRET + '}\n')
    refused(monkeypatch, run)
    src.unlink()
    refused(monkeypatch, run)
    # 3. --security P replaces the policy and the source (absolute)
    other = write(tmp_path / 'other.yaml', 'confirm: {wait_s: 120}\n')
    cfg = drive_resume(monkeypatch, run, '--security', os.path.relpath(other))
    assert cfg['security']['confirm']['wait_s'] == 120 and cfg['security_source'] == str(other.resolve())
    # 4. --no-security drops both keys
    cfg = drive_resume(monkeypatch, run, '--no-security')
    assert 'security' not in cfg and 'security_source' not in cfg
    # 5. D2: a run without security never picks up a default file put there later; only --security turns it on
    write(config_dir / 'security.yaml', '')
    plain = resumable_run(tmp_path, 'r-plain')
    cfg = drive_resume(monkeypatch, plain)
    assert 'security' not in cfg and 'security_source' not in cfg
    cfg = drive_resume(monkeypatch, plain, '--security', str(other))
    assert cfg['security']['confirm']['wait_s'] == 120
    # 6. legacy config: security pinned, no source -> kept and validated again; never a source key (not even null)
    calls = []
    real_parse = security.parse_security
    monkeypatch.setattr(security, 'parse_security', lambda *a, **k: calls.append(1) or real_parse(*a, **k))
    legacy = resumable_run(tmp_path, 'r-legacy', security=base)
    cfg = drive_resume(monkeypatch, legacy)
    assert cfg['security'] == base and 'security_source' not in cfg and len(calls) == 1
    broken = dict(base, sandbox=dict(base['sandbox'], mode='seatbelt'))
    for n, value in enumerate((broken, {}, 'on')):  # invalid, empty, not a mapping: refused, never "off"
        refused(monkeypatch, resumable_run(tmp_path, 'r-legacy-bad-' + str(n), security=value))
    # 7. a sub-fleet follows its parent: no local override, no re-read of the source, the pinned policy validated
    parent_src = write(tmp_path / 'parent.yaml', '')
    child = resumable_run(tmp_path, 'r-child', security=base, security_source=str(parent_src.resolve()),
                          parent_config=str(tmp_path / 'parent-run' / 'config.json'))
    refused(monkeypatch, child, '--security', str(other))
    refused(monkeypatch, child, '--no-security')
    with monkeypatch.context() as m:
        deny_reads_of(m, parent_src, error=AssertionError)  # touching the parent's file fails the test
        cfg = drive_resume(m, child)
    assert cfg['security'] == base and cfg['security_source'] == str(parent_src.resolve())
    bad_child = resumable_run(tmp_path, 'r-child-bad', security=broken,
                              security_source=str(parent_src.resolve()),
                              parent_config=str(tmp_path / 'parent-run' / 'config.json'))
    refused(monkeypatch, bad_child)


def test_resume_no_security_refused_when_run_carries_security_taint(tmp_path, monkeypatch, config_dir):
    """M164: outside content reached the run (security_taint set): `--resume --no-security` is refused with a text
    naming the mark, config.json unchanged; without the flag the run resumes and keeps both its policy and the mark."""
    from cmux_fleet.channels import strangers
    base = defaults()
    src = write(tmp_path / 'tainted.yaml', '')
    for n, taint in enumerate(({'kinds': ['forwarded']}, 'not-a-mapping')):  # well-formed or malformed: both set
        run = resumable_run(tmp_path, 'r-taint-' + str(n), security=base, security_source=str(src.resolve()),
                            **{strangers.TAINT_KEY: taint})
        before = (run / 'config.json').read_bytes()
        with pytest.raises(SystemExit) as exc:
            drive_resume(monkeypatch, run, '--no-security')
        assert strangers.TAINT_KEY in ours(exc)
        assert (run / 'config.json').read_bytes() == before
        cfg = drive_resume(monkeypatch, run)
        assert cfg[strangers.TAINT_KEY] == taint and cfg['security'] == base
    # a mark without a policy is still a mark
    bare = resumable_run(tmp_path, 'r-taint-bare', **{strangers.TAINT_KEY: {'kinds': ['email_body']}})
    with pytest.raises(SystemExit) as exc:
        drive_resume(monkeypatch, bare, '--no-security')
    assert strangers.TAINT_KEY in ours(exc)
