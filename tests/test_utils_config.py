from utils import load_config


def test_load_config_defaults_relationship_weather_off(tmp_path):
    config = load_config(str(tmp_path / "missing-config.yaml"))

    assert config["gateway"]["relationship_weather_interval_rounds"] == 0
    assert config["gateway"]["cooldown_hours"] == 6
    assert config["gateway"]["skip_recent_rounds"] == 5


def test_load_config_reads_runtime_config_before_env_override(tmp_path, monkeypatch):
    runtime_path = tmp_path / "state" / "config.runtime.yaml"
    runtime_path.parent.mkdir()
    runtime_path.write_text(
        "dream:\n  enabled: false\n  base_url: https://runtime.example\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMBRE_STATE_DIR", str(runtime_path.parent))
    monkeypatch.setenv("OMBRE_DREAM_BASE_URL", "https://env.example")

    config = load_config(str(tmp_path / "missing-config.yaml"))

    assert config["dream"]["enabled"] is False
    assert config["dream"]["base_url"] == "https://env.example"
