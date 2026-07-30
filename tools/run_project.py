"""tools/run_project.py —— 辅助识别并启动常见项目，供 Agent 参考使用。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass
class RunHint:
    kind: str
    command: str
    url_hint: str = ""
    reason: str = ""


def detect_run_hint(project_dir: str) -> RunHint | None:
    package_json = os.path.join(project_dir, "package.json")
    if os.path.isfile(package_json):
        try:
            with open(package_json, "r", encoding="utf-8") as f:
                package = json.load(f)
            scripts = package.get("scripts", {})
            if "dev" in scripts:
                return RunHint("node", "npm run dev", "http://127.0.0.1:3000", "检测到 package.json 的 dev 脚本")
            if "start" in scripts:
                return RunHint("node", "npm start", "http://127.0.0.1:3000", "检测到 package.json 的 start 脚本")
        except Exception:
            pass

    if os.path.isfile(os.path.join(project_dir, "manage.py")):
        return RunHint("python", "py -3.11 manage.py runserver", "http://127.0.0.1:8000", "检测到 Django manage.py")

    if os.path.isfile(os.path.join(project_dir, "app.py")):
        return RunHint("python", "py -3.11 app.py", "", "检测到 app.py")

    if os.path.isfile(os.path.join(project_dir, "main.py")):
        return RunHint("python", "py -3.11 main.py", "", "检测到 main.py")

    return None
