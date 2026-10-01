"""Run with: uv run python tests/test_config.py

Covers reading config.toml: the per-account notify flag, and its default.
"""

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from wima import config

AUTH = """
[auth]
username = "me"
password_hash = "scrypt$not-a-real-hash"
"""


def _load(accounts: str) -> config.Config:
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "config.toml"
        path.write_text(f"{AUTH}{accounts}")
        return config.load_config(path)


def test_notify_flag_is_read() -> None:
    cfg = _load('[[account]]\nhash = "abc"\nlabel = "Bills"\nnotify = true\n')
    assert cfg.accounts[0].notify is True


def test_notify_defaults_to_off() -> None:
    cfg = _load('[[account]]\nhash = "abc"\nlabel = "Bills"\n')
    assert cfg.accounts[0].notify is False


def test_quoted_notify_exits() -> None:
    try:
        _load('[[account]]\nhash = "abc"\nlabel = "Bills"\nnotify = "false"\n')
    except SystemExit as e:
        assert "Bills" in str(e.code)
    else:
        raise AssertionError("expected SystemExit for a quoted notify value")


if __name__ == "__main__":
    for name, test in sorted(globals().items()):
        if name.startswith("test_"):
            test()
            print(f"ok  {name}")
    print("all passed")
