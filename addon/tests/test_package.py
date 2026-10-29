"""De bestanden die Home Assistant zelf leest (config.yaml, build.yaml, vertalingen, repository.yaml) moeten geldige YAML zijn
en kloppen met wat de add-on werkelijk als optie kent. Een fout hier maakt de add-on onzichtbaar in de store."""
import os
from pathlib import Path

import pytest
import yaml

# in de testcontainer is het hele project alleen-lezen gemount op KK_PROJECT (zie tools/test.sh)
ROOT = Path(os.environ.get("KK_PROJECT") or Path(__file__).resolve().parents[2])
ADDON = ROOT / "addon"
pytestmark = pytest.mark.skipif(not (ADDON / "config.yaml").exists(), reason="projectmap niet beschikbaar")


def load(p: Path):
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def test_config_yaml_is_valid_and_complete():
    c = load(ADDON / "config.yaml")
    for k in ("name", "version", "slug", "arch", "options", "schema", "ingress", "ingress_port"):
        assert k in c, k
    assert set(c["options"]) == set(c["schema"]), "elke optie heeft een schema en omgekeerd"
    assert c["homeassistant_api"] is True and c["host_network"] is True
    assert isinstance(c["options"]["peers"], list) and c["schema"]["peers"] == ["str"]


def test_every_option_is_known_to_the_settings_loader():
    src = (ADDON / "kkaddon" / "settings.py").read_text(encoding="utf-8")
    for k in load(ADDON / "config.yaml")["options"]:
        assert f'"{k}"' in src, f"optie {k} wordt niet gelezen in settings.py"


@pytest.mark.parametrize("p", ["build.yaml", "translations/nl.yaml"])
def test_addon_support_files_are_valid_yaml(p):
    assert load(ADDON / p)


def test_repository_yaml_points_at_the_real_repository():
    r = load(ROOT / "repository.yaml")
    assert r["name"] and r["url"].startswith("https://github.com/") and "example.invalid" not in r["url"]
    assert "example.invalid" not in (ADDON / "config.yaml").read_text(encoding="utf-8")


def test_build_images_are_given_for_every_arch():
    c, b = load(ADDON / "config.yaml"), load(ADDON / "build.yaml")
    assert set(c["arch"]) <= set(b["build_from"])
