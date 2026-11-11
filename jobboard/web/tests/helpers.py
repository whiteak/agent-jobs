import json, os, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo,
                          check=True, capture_output=True, text=True).stdout


def make_py_project(tmp: Path, passing=True, extra_test=False) -> Path:
    p = tmp / "proj"
    (p / "tests").mkdir(parents=True)
    (p / "calc.py").write_text("def add(a, b):\n    return a + b\n" if passing else "def add(a, b):\n    return a - b\n")
    (p / "tests" / "test_calc.py").write_text(
        "import sys, os, unittest\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "from calc import add\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
        "    def test_zero(self):\n        self.assertEqual(add(0, 0), 0)\n")
    (p / "AGENT_CHECKS.json").write_text(json.dumps({"checks": [
        {"name": "tests", "cmd": f"{sys.executable} -m unittest discover -s tests -p 'test_*.py'", "timeout_s": 60}]}))
    (p / "PLAN.md").write_text("# Plan\n- [ ] step one\n- [ ] step two\n- [ ] step three\n")
    (p / ".gitignore").write_text("__pycache__/\n")
    git(p, "init", "-q", "-b", "main")
    git(p, "add", "-A")
    git(p, "commit", "-qm", "base")
    return p


def fake_agent_runner(project: Path, scenario: dict, log: Path):
    sc = project.parent / "scenario.json"
    sc.write_text(json.dumps(scenario))
    def run_agent(prompt, rnd):
        (project.parent / f"prompt-{rnd}.txt").write_text(prompt)
        subprocess.run([sys.executable, "-m", "agentjobs_upgrades.fakeagent", str(sc), "--round", str(rnd),
                        "--log", str(log)], cwd=project, check=False, env=dict(os.environ, PYTHONPATH=str(ROOT)))
    return run_agent
