from pathlib import Path

import yaml


def test_hk_dashboard_can_persist_config_while_gateway_reads_only():
    compose = yaml.safe_load(Path("compose.hk.yml").read_text(encoding="utf-8"))
    brain_volumes = compose["services"]["ombre-brain"]["volumes"]
    gateway_volumes = compose["services"]["ombre-gateway"]["volumes"]

    assert "/srv/ombre-brain/config.yaml:/app/config.yaml" in brain_volumes
    assert "/srv/ombre-brain/config.yaml:/app/config.yaml:ro" not in brain_volumes
    assert "/srv/ombre-brain/config.yaml:/app/config.yaml:ro" in gateway_volumes
