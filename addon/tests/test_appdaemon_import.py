import pytest

from kkaddon import appdaemon_import as imp
from kkaddon.model import ModelError

# geanonimiseerd voorbeeld in de vorm van het oude apps.yaml
SAMPLE = """
kruisschakelaar_lamp_keuken:
  module: kruisschakelaar
  class: Kruisschakelaar
  entities:
    - switch.relaisbox_relay_4
    - light.touch1_relay_1
    - switch.tasmota
kruisschakelaar_lamp_quarto_das_visitas_bed_links:
  module: kruisschakelaar
  class: Kruisschakelaar
  entities:
    - switch.relaisbox_relay_3
    - light.touch5_relay_3
kruisschakelaar_lamp_eetplaats:
  module: kruisschakelaar
  class: Kruisschakelaar
  entities:
    - switch.relaisbox_relay_2
    #- light.touch11_relay_4
    - light.touch9_relay_4
kruisschakelaar_lamp_enkel:
  module: kruisschakelaar
  class: Kruisschakelaar
  entities:
    - switch.alleen_ik
ander_app:
  module: iets_anders
  class: Iets
  entities:
    - switch.x
    - switch.y
globals:
  log: true
"""


def test_parses_groups_names_and_skips_what_is_not_a_group():
    r = imp.parse(SAMPLE)
    by = {g["id"]: g for g in r["groups"]}
    assert list(by) == ["kruisschakelaar_lamp_keuken", "kruisschakelaar_lamp_quarto_das_visitas_bed_links", "kruisschakelaar_lamp_eetplaats"]
    assert by["kruisschakelaar_lamp_keuken"]["name"] == "Keuken"
    assert by["kruisschakelaar_lamp_quarto_das_visitas_bed_links"]["name"] == "Quarto das visitas bed links"
    assert by["kruisschakelaar_lamp_eetplaats"]["entities"] == ["switch.relaisbox_relay_2", "light.touch9_relay_4"], "een uitgecommentarieerde regel blijft weg"
    reasons = {s["key"]: s["reason"] for s in r["skipped"]}
    assert "minder dan 2" in reasons["kruisschakelaar_lamp_enkel"]
    assert "andere app" in reasons["ander_app"] and "geen kring" in reasons["globals"]


def test_bad_entities_and_duplicates_become_warnings():
    r = imp.parse("g:\n  entities: [switch.a, switch.a, 'Switch A', switch.b]\n")
    g = r["groups"][0]
    assert g["entities"] == ["switch.a", "switch.b"] and len(g["warnings"]) == 2


@pytest.mark.parametrize("bad", ["a: [", "- 1\n- 2", "gewoon tekst", "x" * 200_001])
def test_unreadable_input_is_refused(bad):
    with pytest.raises(ModelError):
        imp.parse(bad)
