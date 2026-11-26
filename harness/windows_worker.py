"""Release a command only after its Windows process job is assigned."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main(root: Path) -> int:
    if sys.stdin.buffer.read(1) != b"1":
        return 1
    argv, cwd = json.loads((root / "request.json").read_text(encoding="utf-8"))
    with (root / "stdin").open("rb") as input_file:
        try:
            os.chdir(cwd)
            executable = shutil.which(argv[0]) or argv[0]
            return subprocess.call(argv, executable=executable, stdin=input_file)
        except OSError:
            (root / "start-failed").touch()
            return 1


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
