"""Real SQLite/process coordination tests; no model or internet dependency."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import multiprocessing
import os
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace

from jsonschema import Draft202012Validator
import pytest

from sheetbend import Registry, Runtime
from sheetbend.config import load_schema
from sheetbend.errors import ConnectionClosedError, CoordinationError


def _source(config, path):
    return Registry(config, config_path=Path(path)).source()


def _hold_request(config, path, entered, release):
    source = _source(config, path)
    with source._admission.acquire(model=source.default_model, operation="generate", application="worker", tool="batch"):
        entered.set()
        release.wait(10)


def _snapshot_worker(queue):
    queue.put(Runtime().snapshot())


def _admit(source, **kwargs):
    return source._admission.acquire(model=source.default_model, operation="generate", **kwargs)


def test_construction_and_empty_view_do_not_create_state(config):
    registry = Registry.from_dict(config)
    runtime = Runtime()
    assert not runtime.directory.exists()
    assert runtime.snapshot()["requests"] == []
    assert not runtime.directory.exists()
    assert registry.source()


def test_real_cross_process_concurrency_and_app_labels(config, tmp_path):
    config["sources"]["local"]["rate_limit"] = {"max_concurrent": 1}
    ctx = multiprocessing.get_context("spawn")
    entered1, entered2, release1, release2 = (ctx.Event() for _ in range(4))
    path = str(tmp_path / "config.toml")
    first = ctx.Process(target=_hold_request, args=(config, path, entered1, release1))
    second = ctx.Process(target=_hold_request, args=(config, path, entered2, release2))
    first.start()
    try:
        assert entered1.wait(8)
        second.start()
        assert not entered2.wait(0.5)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            rows = Runtime().snapshot()["requests"]
            if any(row["state"] == "waiting" for row in rows):
                break
            time.sleep(0.05)
        assert {row["state"] for row in rows} == {"waiting", "running"}
        assert {row["pid"] for row in rows} == {first.pid, second.pid}
        assert {row["application"] for row in rows} == {"worker"}
        assert {row["tool"] for row in rows} == {"batch"}
        release1.set()
        assert entered2.wait(8)
    finally:
        release1.set()
        release2.set()
        for process in (first, second):
            if process.pid:
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
    assert first.exitcode == second.exitcode == 0
    assert all(row["state"] == "done" for row in Runtime().snapshot()["requests"])


def test_dead_process_releases_concurrency_without_expiring_live_work(config, tmp_path):
    config["sources"]["local"]["rate_limit"] = {"max_concurrent": 1}
    ctx = multiprocessing.get_context("spawn")
    entered, release = ctx.Event(), ctx.Event()
    path = str(tmp_path / "config.toml")
    child = ctx.Process(target=_hold_request, args=(config, path, entered, release))
    child.start()
    try:
        assert entered.wait(8)
        child.terminate()
        child.join(5)
        source = _source(config, path)
        with _admit(source):
            states = {r["state"] for r in Runtime().snapshot()["requests"]}
            assert states == {"abandoned", "running"}
    finally:
        if child.is_alive():
            child.terminate()
            child.join(5)


def test_late_viewer_in_another_process(config, tmp_path):
    source = _source(config, tmp_path / "config.toml")
    with _admit(source, application="downrange", tool="stain-qc"):
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        process = ctx.Process(target=_snapshot_worker, args=(queue,))
        process.start()
        try:
            data = queue.get(timeout=8)
            assert data["requests"][0]["application"] == "downrange"
            assert data["requests"][0]["pid"] == os.getpid()
        finally:
            process.join(8)
            if process.is_alive():
                process.terminate()
                process.join(5)
            queue.close()
            queue.join_thread()
        assert process.exitcode == 0


def test_same_config_models_and_symlink_paths_share_bucket(config, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("placeholder")
    link = tmp_path / "alias.toml"
    link.symlink_to(path)
    first = _source(config, path)
    second = _source(config, link)
    assert first._admission.bucket == second._admission.bucket
    assert first._admission.registry_id == second._admission.registry_id
    assert _source(config, tmp_path / "other.toml")._admission.bucket != first._admission.bucket
    assert Registry.from_dict(config).source()._admission.bucket != Registry.from_dict(config).source()._admission.bucket


def test_config_conflict_fails_closed_even_after_last_call_finishes(config, tmp_path):
    path = tmp_path / "config.toml"
    first = _source(config, path)
    changed = deepcopy(config)
    changed["sources"]["local"]["rate_limit"] = {"requests_per_minute": 1}
    second = _source(changed, path)
    request = _admit(first)
    try:
        with pytest.raises(CoordinationError, match="Conflicting"):
            _admit(second)
    finally:
        request.finish("done")
    with pytest.raises(CoordinationError, match="60-second"):
        _admit(second)


def test_shared_rolling_starts_count_failures(config, tmp_path, monkeypatch):
    import sheetbend.runtime.runtime as module
    clock = [1000.0]
    waits = []
    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(module, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0], sleep=sleep))
    config["sources"]["local"]["rate_limit"] = {"requests_per_minute": 2}
    first = _source(config, tmp_path / "config.toml")
    second = _source(config, tmp_path / "config.toml")
    _admit(first).finish("error")
    clock[0] = 1010.0
    _admit(second).finish("done")
    clock[0] = 1020.0
    _admit(first).finish("done")
    assert 1060 <= clock[0] <= 1060.2
    assert waits
    assert len(Runtime().snapshot()["requests"]) == 3


def test_waiting_request_can_be_cancelled(config, tmp_path):
    config["sources"]["local"]["rate_limit"] = {"max_concurrent": 1}
    source = _source(config, tmp_path / "config.toml")
    cancel = Event()
    with _admit(source), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_admit, source, cancelled=cancel)
        end = time.monotonic() + 3
        while time.monotonic() < end:
            if any(r["state"] == "waiting" for r in Runtime().snapshot()["requests"]):
                break
            time.sleep(0.01)
        cancel.set()
        with pytest.raises(ConnectionClosedError):
            future.result(timeout=3)
    assert {r["state"] for r in Runtime().snapshot()["requests"]} == {"done", "cancelled"}


def test_bounded_terminal_history_does_not_drop_rate_accounting(config, tmp_path, monkeypatch):
    import sheetbend.runtime.runtime as module
    monkeypatch.setattr(module, "_RECENT_LIMIT", 3)
    source = _source(config, tmp_path / "config.toml")
    for _ in range(8):
        with _admit(source):
            pass
    data = Runtime().snapshot()
    assert len(data["requests"]) == 3
    with source._admission.runtime._store.transaction() as db:
        assert db.execute("SELECT count(*) FROM starts").fetchone()[0] == 8


def test_pid_reuse_is_not_mistaken_for_original_process(config, tmp_path):
    source = _source(config, tmp_path / "config.toml")
    request = _admit(source)
    with source._admission.runtime._store.transaction() as db:
        db.execute("UPDATE requests SET process_started=process_started-1 WHERE request_id=?", (request.request_id,))
    assert Runtime().snapshot()["requests"][0]["state"] == "abandoned"


def test_activity_schema_privacy_and_usage(config, tmp_path, monkeypatch):
    monkeypatch.setenv("NOT_FOR_TELEMETRY", "VERY_SECRET_VALUE")
    config["sources"]["local"]["auth"] = {"type": "bearer", "env": "NOT_FOR_TELEMETRY"}
    source = _source(config, tmp_path / "private-study-name.toml")
    request = _admit(source, application="notebook", tool="exploration")
    request.streaming()
    running = Runtime().snapshot()["requests"][0]
    assert running["state"] == "streaming"
    assert running["input_tokens"] is None
    request.finish("done", input_tokens=23, output_tokens=7)
    request.finish("error")  # Idempotent, not a rewrite of a successful result.
    data = Runtime().snapshot()
    Draft202012Validator(load_schema("activity")).validate(data)
    assert data["requests"][0]["input_tokens"] == 23
    assert data["requests"][0]["state"] == "done"
    rendered = json.dumps(data)
    for secret in ("VERY_SECRET_VALUE", "private-study-name", "NOT_FOR_TELEMETRY", source.base_url):
        assert secret not in rendered


def test_unknown_usage_stays_null(config, tmp_path):
    request = _admit(_source(config, tmp_path / "c.toml"))
    request.finish("done", input_tokens=-1, output_tokens=True)
    row = Runtime().snapshot()["requests"][0]
    assert row["input_tokens"] is row["output_tokens"] is None


@pytest.mark.parametrize("mode", [0o777, 0o755, 0o750])
def test_nonprivate_runtime_is_rejected(config, tmp_path, monkeypatch, mode):
    runtime = tmp_path / "insecure"
    runtime.mkdir(mode=mode)
    runtime.chmod(mode)
    monkeypatch.setenv("SHEETBEND_RUNTIME_DIR", str(runtime))
    with pytest.raises(CoordinationError, match="0700"):
        _admit(_source(config, tmp_path / "config.toml"))


def test_symlink_directory_is_rejected(config, tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "symlink"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("SHEETBEND_RUNTIME_DIR", str(link))
    with pytest.raises(CoordinationError):
        _admit(_source(config, tmp_path / "config.toml"))


def test_corrupted_ledger_does_not_become_unlimited(config, tmp_path):
    source = _source(config, tmp_path / "config.toml")
    store = source._admission.runtime._store
    store.directory.mkdir(mode=0o700)
    store.path.write_bytes(b"not a SQLite database")
    store.path.chmod(0o600)
    with pytest.raises(CoordinationError):
        _admit(source)


def test_directory_parent_must_exist(config, tmp_path, monkeypatch):
    monkeypatch.setenv("SHEETBEND_RUNTIME_DIR", str(tmp_path / "missing" / "runtime"))
    with pytest.raises(CoordinationError):
        _admit(_source(config, tmp_path / "c.toml"))


def test_private_database_mode(config, tmp_path):
    source = _source(config, tmp_path / "c.toml")
    with _admit(source):
        for path in source._admission.runtime.directory.iterdir():
            assert path.stat().st_mode & 0o077 == 0


def test_previous_boot_files_removed_but_unrelated_files_preserved(config, tmp_path):
    source = _source(config, tmp_path / "c.toml")
    store = source._admission.runtime._store
    store.directory.mkdir(mode=0o700)
    old = store.directory / ("runtime-" + "0" * 24 + ".sqlite3")
    old.write_bytes(b"old boot")
    old.chmod(0o600)
    unrelated = store.directory / "notes.txt"
    unrelated.write_text("not our file")
    with _admit(source):
        assert not old.exists()
        assert unrelated.exists()


def test_runtime_failure_prevents_catalog_network_request(config, tmp_path, endpoint, monkeypatch):
    config["sources"]["local"]["base_url"] = endpoint["base_url"]
    directory = tmp_path / "unsafe"
    directory.mkdir(mode=0o755)
    monkeypatch.setenv("SHEETBEND_RUNTIME_DIR", str(directory))
    source = _source(config, tmp_path / "c.toml")
    with pytest.raises(CoordinationError):
        source.list_models()
    assert endpoint["requests"] == []


def test_concurrent_short_requests_leave_consistent_counts(config, tmp_path):
    config["sources"]["local"]["rate_limit"] = {"max_concurrent": 3}
    source = _source(config, tmp_path / "c.toml")
    def call(_):
        with _admit(source):
            active = [row for row in Runtime().snapshot()["requests"] if row["state"] == "running"]
            assert len(active) <= 3
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(call, range(40)))
    rows = Runtime().snapshot()["requests"]
    assert len(rows) == 40
    assert all(row["state"] == "done" for row in rows)


def test_macos_runtime_directory_uses_darwin_user_temp(monkeypatch, tmp_path):
    import subprocess
    from types import SimpleNamespace

    import sheetbend.runtime.storage as storage

    monkeypatch.setattr(storage.sys, "platform", "darwin")
    monkeypatch.delenv("SHEETBEND_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return SimpleNamespace(stdout=f"{tmp_path}\n")

    monkeypatch.setattr(storage.subprocess, "run", run)
    assert storage.runtime_directory() == tmp_path / "sheetbend"
    assert seen == [(["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"], {
        "check": True, "capture_output": True, "text": True, "timeout": 5,
    })]


def test_macos_runtime_directory_getconf_failure_is_sheetbend_error(monkeypatch):
    import subprocess

    import sheetbend.runtime.storage as storage

    monkeypatch.setattr(storage.sys, "platform", "darwin")
    monkeypatch.delenv("SHEETBEND_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(storage.subprocess, "run", fail)
    with pytest.raises(CoordinationError, match="macOS user runtime directory"):
        storage.runtime_directory()
