import importlib
from pathlib import Path
from src.tools.decorator import ToolDict, ToolEntry, ToolPermission

package_dir = Path(__file__).parent
package_dir = package_dir / "builtin"
for item in sorted(package_dir.glob("*.py")):
    if item.name == "__init__.py":
        continue
    module_name = item.stem
    module = importlib.import_module(f".{module_name}", package=f"{__package__}.builtin")

__all__ = ["ToolDict", "ToolEntry", "ToolPermission"]
