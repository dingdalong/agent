import importlib
from pathlib import Path
from src.tools.decorator import ToolDict, ToolEntry
from src.tools.policy import AccessKind, DataFlow, PathArgument, PathRole, ToolOrigin, ToolPolicy

package_dir = Path(__file__).parent
package_dir = package_dir / "builtin"
for item in sorted(package_dir.glob("*.py")):
    if item.name == "__init__.py":
        continue
    module_name = item.stem
    module = importlib.import_module(f".{module_name}", package=f"{__package__}.builtin")

__all__ = [
    "AccessKind", "DataFlow", "PathArgument", "PathRole", "ToolDict", "ToolEntry",
    "ToolOrigin", "ToolPolicy",
]
