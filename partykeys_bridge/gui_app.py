"""Desktop app entry point, with GUI startup and visible configuration errors."""
import sys

from . import cli, settings


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv.insert(0, "gui")
    if argv[0] in ("gui", "bridge") and "--smoke-test" not in argv:
        try:
            if not settings.explicit(argv, "--config"):
                settings.prepare_app_config()
            args = cli.parser().parse_args(argv)
            settings.load(args.config or settings.default_path(), required=bool(args.config))
        except (OSError, ValueError) as error:
            from tkinter import messagebox
            messagebox.showerror("PartyKeys Lights", f"Could not open settings:\n{error}")
            return 1
    return cli.main(argv)
