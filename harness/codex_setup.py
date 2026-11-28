"""Install the bundled Personal Harness skill and bind it to this extraction."""

import argparse
import hashlib
import os
import tempfile
from pathlib import Path

from .workflow import json_text


SKILL_NAME = "personal-harness"
PREVIOUS_SKILL_SHA256 = {
    "ac3192f78aa843aa8d56eb08f2e01b4d6afd7e9995fd57ecab8d46a8bf94ed18",  # Standalone
    "7f6d1668a608d3155d7fbccccb302de4d47ca4180ab8db497bcfeca94fd6ad83",  # v0.1.1
}


def user_skill_directory() -> Path:
    home = Path.home()
    user_skills = home / ".agents" / "skills"
    configured_home = os.environ.get("CODEX_HOME")
    legacy_home = Path(configured_home).expanduser() if configured_home else home / ".codex"
    candidates = (user_skills.resolve(), (legacy_home / "skills").resolve())
    existing = [root for root in dict.fromkeys(candidates)
                if (root / SKILL_NAME).exists() or (root / SKILL_NAME).is_symlink()]
    if len(existing) > 1:
        locations = ", ".join(str(root / SKILL_NAME) for root in existing)
        raise ValueError(f"Multiple Personal Harness skills already exist: {locations}")
    return existing[0] if existing else user_skills


def write_binding(destination: Path, bundle: Path) -> None:
    if destination.is_symlink():
        raise ValueError(f"The existing runner binding is a symbolic link: {destination}")
    with tempfile.TemporaryDirectory(prefix=".harness-binding-", dir=destination.parent) as directory:
        staged = Path(directory) / destination.name
        staged.write_text(f"{bundle}\n", encoding="utf-8")
        staged.replace(destination)


def install_codex_skill(bundle: Path, skills_dir: Path) -> Path:
    bundle = bundle.resolve()
    if "\n" in str(bundle) or "\r" in str(bundle):
        raise ValueError("The Personal Harness folder path must fit on one line.")
    launcher = bundle / ("harness.cmd" if os.name == "nt" else "harness")
    example = bundle / "examples" / "research-briefing.json"
    source = bundle / "codex-skill" / SKILL_NAME / "SKILL.md"
    if not launcher.is_file() or not example.is_file() or not source.is_file():
        raise ValueError(f"Extract the complete Personal Harness ZIP before configuring Codex: {bundle}")
    bundled_skill = source.read_text(encoding="utf-8")
    skills_dir = skills_dir.expanduser().resolve()
    target = skills_dir / SKILL_NAME
    destination = target / "SKILL.md"
    binding = target / "runner-location.txt"
    if target.is_symlink() or destination.is_symlink():
        raise ValueError(f"An existing Personal Harness skill uses a symbolic link: {target}")

    if target.exists():
        if not destination.is_file():
            raise ValueError(f"An existing folder is using the Personal Harness skill name: {target}")
        installed = destination.read_text(encoding="utf-8")
        if installed != bundled_skill:
            if hashlib.sha256(installed.encode("utf-8")).hexdigest() not in PREVIOUS_SKILL_SHA256:
                raise ValueError(f"A different Personal Harness skill is already installed: {target}")
        write_binding(binding, bundle)
        if installed != bundled_skill:
            with tempfile.TemporaryDirectory(prefix=".harness-skill-", dir=target) as directory:
                staged = Path(directory) / destination.name
                staged.write_text(bundled_skill, encoding="utf-8")
                staged.replace(destination)
        return target

    skills_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".harness-skill-", dir=skills_dir) as directory:
        staged = Path(directory) / SKILL_NAME
        staged.mkdir()
        (staged / destination.name).write_text(bundled_skill, encoding="utf-8")
        (staged / binding.name).write_text(f"{bundle}\n", encoding="utf-8")
        staged.rename(target)
    return target


def setup_codex(bundle: Path, argv: list[str]) -> int:
    cli = argparse.ArgumentParser(prog="harness setup-codex", description=__doc__)
    cli.add_argument("--skills-dir", type=Path, help="Use an explicit skills directory for a project-level install")
    args = cli.parse_args(argv)
    try:
        target = install_codex_skill(bundle, args.skills_dir or user_skill_directory())
    except (OSError, UnicodeError, ValueError) as error:
        cli.exit(1, f"harness setup-codex: {error}\n")
    print(json_text({"status": "configured", "skill": str(target), "runner": str(bundle.resolve())}))
    return 0
