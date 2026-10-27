from pathlib import Path
import sys
import traceback


PROJECT_ROOT = Path(__file__).resolve().parent
for package_root in ("06_web_tools", "07_web_config"):
    path = str(PROJECT_ROOT / package_root)
    if path not in sys.path:
        sys.path.insert(0, path)

from web_tools.browser_bridge import main


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        error_file = PROJECT_ROOT / "09_web_logs" / "browser_bridge_startup_error.log"
        error_file.parent.mkdir(parents=True, exist_ok=True)
        error_file.write_text(traceback.format_exc(), encoding="utf-8")
        raise