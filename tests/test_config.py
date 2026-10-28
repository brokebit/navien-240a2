import pytest

from navien import config

from conftest import ROOT


def test_defaults():
    cfg = config.load()
    assert cfg.serial.baud == 19200 and cfg.state.history_seconds == 300
    assert cfg.rawlog.rotate == "hourly" and not cfg.influx.enabled


def test_example_file_matches_defaults():
    assert config.load(ROOT / "navien.example.toml") == config.load()


def test_override_and_int_to_float(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[state]\nhistory_seconds = 60\nbus_timeout_s = 5\n')
    cfg = config.load(p)
    assert cfg.state.history_seconds == 60 and cfg.state.bus_timeout_s == 5.0


@pytest.mark.parametrize("text", ['[state]\nnope = 1\n', '[serial]\nbaud = "fast"\n',
                                  '[rawlog]\nrotate = "weekly"\n', 'serial = 3\n'])
def test_rejects_bad_config(tmp_path, text):
    p = tmp_path / "c.toml"
    p.write_text(text)
    with pytest.raises(ValueError):
        config.load(p)
