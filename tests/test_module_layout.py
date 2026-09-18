"""验证 Manager 与公共模块的目录边界。"""

from pathlib import Path


ROOT = Path(__file__).parents[1]
MGR_DIR = ROOT / "src" / "mgr"
COMMON_DIR = ROOT / "src" / "common"


def test_mgr_top_level_contains_only_managers_and_packages() -> None:
    unexpected = {
        path.name
        for path in MGR_DIR.iterdir()
        if path.is_file() and path.name != "__init__.py" and path.suffix == ".py" and not path.stem.endswith("_mgr")
    }
    assert not unexpected


def test_common_modules_do_not_depend_on_mgr() -> None:
    offenders = []
    for path in COMMON_DIR.glob("*.py"):
        if "src.mgr" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert not offenders


def test_moved_modules_have_single_definition_location() -> None:
    moved = (
        "data_guard", "env_baseline", "features", "frozen", "patch", "path_resolver",
        "paths", "ripgrep", "sandbox", "secure_io", "session_state",
    )
    for name in moved:
        assert not (MGR_DIR / f"{name}.py").exists()
        assert (COMMON_DIR / f"{name}.py").exists()
