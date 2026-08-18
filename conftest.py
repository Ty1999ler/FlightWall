"""Root conftest so pytest puts the repo root on sys.path (CI runs bare
`pytest`, which unlike `python -m pytest` does not add the cwd), letting
tests import server.py."""
