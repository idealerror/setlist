import sys

try:
    from .cli import main
except ImportError as exc:
    # Naming the interpreter is the whole diagnosis. `py -3.12` resolves via
    # PATH, and a Scheduled Task does not inherit an interactive shell's PATH,
    # so a machine with two 3.12 installs will run fine by hand and fail under
    # the task with nothing but a bare ModuleNotFoundError to go on.
    sys.stderr.write(
        f"\nCannot start: {exc}\n\n"
        f"  interpreter: {sys.executable}\n"
        f"  version:     {sys.version.split()[0]}\n\n"
        "The client's dependencies are not installed for THAT interpreter.\n"
        "If it runs when launched by hand but not from the Scheduled Task,\n"
        "the two are using different Pythons.\n\n"
        "Install for this interpreter:\n"
        f"  \"{sys.executable}\" -m pip install -e <repo>\\client\n\n"
        "Then re-register the task, which pins the absolute path:\n"
        "  ops\\install-task.ps1 -ConfigPath <repo>\\client\\config.toml\n"
    )
    raise SystemExit(1)

if __name__ == "__main__":
    sys.exit(main())
