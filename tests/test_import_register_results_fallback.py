import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "ops" / "import_register_results_fallback.py"
spec = importlib.util.spec_from_file_location("import_register_results_fallback", MODULE_PATH)
fallback = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(fallback)


def _args(tmp_path, **overrides):
    class Args:
        results_dir = None
        base_results_dir = fallback.DEFAULT_RESULTS_DIR
        state_path = tmp_path / "state.json"
        full = False
        latest = False

    args = Args()
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_resolve_results_dir_defaults_to_full_before_state(tmp_path):
    selected, mode, state = fallback.resolve_results_dir(_args(tmp_path))

    assert selected == fallback.DEFAULT_RESULTS_DIR
    assert mode == "initial_full"
    assert state == {}


def test_resolve_results_dir_uses_latest_after_initial_state(tmp_path):
    results = tmp_path / "results"
    old = results / "批次_2026-06-13_10-00-00"
    new = results / "批次_2026-06-13_11-00-00"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"initial_full_completed": True}), encoding="utf-8")

    selected, mode, state = fallback.resolve_results_dir(_args(tmp_path, results_dir=results, state_path=state_path))

    assert selected == results
    assert mode == "explicit"
    assert state == {}

    selected, mode, state = fallback.resolve_results_dir(_args(tmp_path, base_results_dir=results, state_path=state_path))

    assert selected == new
    assert mode == "latest_after_initial"
    assert state["initial_full_completed"] is True


def test_resolve_results_dir_force_latest_uses_latest_batch(tmp_path):
    results = tmp_path / "results"
    old = results / "批次_2026-06-13_10-00-00"
    new = results / "批次_2026-06-13_11-00-00"
    old.mkdir(parents=True)
    new.mkdir(parents=True)

    selected, mode, _ = fallback.resolve_results_dir(_args(tmp_path, results_dir=results, latest=True))

    assert selected == new
    assert mode == "latest_forced"


def test_resolve_results_dir_force_full_uses_results_root(tmp_path):
    results = tmp_path / "results"
    results.mkdir()

    selected, mode, _ = fallback.resolve_results_dir(_args(tmp_path, results_dir=results, full=True))

    assert selected == results
    assert mode == "full_forced"
