"""PyInstaller entry point; keep imports absolute for the frozen executable."""
from partykeys_bridge.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
