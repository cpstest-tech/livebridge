import sys


def main():
    # `livebridge` with no args (or with a live-studio-app:// URI) opens the GUI;
    # any known subcommand runs the CLI.
    from .cli import COMMANDS
    if len(sys.argv) > 1 and (sys.argv[1] in COMMANDS or sys.argv[1] in ("-h", "--help", "--version")):
        from .cli import main as cli_main
        return cli_main()
    from .gui.app import main as gui_main
    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
