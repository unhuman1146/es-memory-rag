"""Run the offline 1.4.0 acceptance set and report suite counts."""
from pathlib import Path
import re
import subprocess
import sys
import os

root = Path(__file__).resolve().parents[1]
commands = [
    ("memory_engine", "scripts/test_memory_engine.py"),
    ("context_budget", "scripts/test_context_budget.py"),
    ("adapter", "scripts/test_adapter.py"),
    ("chat_path", "scripts/test_chat_path.py"),
    ("slow_forget", "scripts/test_slow_forget.py"),
    ("release", "scripts/test_release.py"),
    ("context_pager", "scripts/test_context_pager.py"),
    ("demo_memory", "scripts/demo_140_alpha.py"),
    ("demo_paging", "scripts/demo_paging.py"),
]
failed = False
total = 0
for name, path in commands:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, path], cwd=root, text=True,
                            encoding="utf-8", errors="replace",
                            env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    match = re.search(r"Ran (\d+) tests?", result.stdout)
    count = int(match.group(1)) if match else None
    if count is not None:
        total += count
    ok = result.returncode == 0
    print(f"{name}: {'PASS' if ok else 'FAIL'}" +
          (f" ({count} tests)" if count is not None else ""))
    if not ok:
        print(result.stdout[-6000:])
        failed = True
print(f"test_total={total}")
raise SystemExit(1 if failed else 0)
