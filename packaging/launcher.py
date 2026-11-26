"""Entry point shared by the portable packages."""

import sys
from pathlib import Path

from harness.__main__ import main
from harness.adapters import built_in_adapters
from harness.runner import Runner
from harness.store import Store
from harness.workflow import parse_workflow


def guided_demo(bundle: Path) -> int:
    print("Personal Harness | Offline research briefing")
    print("This demo uses sample notes and saves progress on this computer.")
    if input("Press Enter to start, or type q to exit: ").strip().lower() == "q":
        return 0

    workspace = Path.cwd()
    adapters = built_in_adapters()
    sample = (bundle / "examples" / "research-briefing.json").read_text(encoding="utf-8")
    workflow = parse_workflow(sample, adapters)
    store = Store(workspace / ".harness" / "state.sqlite3", create=True)
    runner = Runner(store, adapters)
    run_id = store.create(workflow, workspace)
    runner.resume(run_id, {"mock"})
    report = runner.status(run_id, include_outputs=True)
    draft = report["steps"][1]["output"]
    print(f"\nDraft: {draft['summary']}")
    print(f"Next: {draft['next_actions'][0]}")
    print(f"Run: {run_id}")
    answer = input("Approve [y], reject [n], or press Enter to leave pending: ").strip().lower()
    if answer in {"y", "n"}:
        report = runner.decide(run_id, "review_briefing", approve=answer == "y")
    print(f"Status: {report['status']}")
    print(f"Run record: {store.path}")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--guided-demo"]:
        folder = Path(sys.executable).resolve().parent.parent if getattr(sys, "frozen", False) else (
            Path(__file__).resolve().parent
        )
        raise SystemExit(guided_demo(folder))
    raise SystemExit(main())
