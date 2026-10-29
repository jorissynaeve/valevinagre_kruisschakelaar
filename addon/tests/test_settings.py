import json

import pytest

from kkaddon import settings as cfg
from kkaddon.transport import split_hostport


def test_key_is_generated_once_and_persisted(tmp_path):
    env = {"KK_DATA": str(tmp_path)}
    s1 = cfg.load(env)
    assert s1.key_generated and len(s1.key) == 32
    s2 = cfg.load(env)
    assert not s2.key_generated and s2.key == s1.key  # blijft dezelfde sleutel
    assert (tmp_path / "network.key").read_text().strip() == s1.key_hex


def test_key_from_options_and_env_precedence(tmp_path):
    k1, k2 = "ab" * 32, "cd" * 32
    (tmp_path / "options.json").write_text(json.dumps({"network_key": k1, "peers": ["bridge1", "10.0.0.5:5000"], "hub": False, "peer_id": 61441}))
    s = cfg.load({"KK_DATA": str(tmp_path)})
    assert s.key_hex == k1 and s.peers == ["bridge1", "10.0.0.5:5000"] and s.hub is False and s.peer_id == 61441
    s = cfg.load({"KK_DATA": str(tmp_path), "KK_NETWORK_KEY": k2, "KK_PEERS": "a, b", "KK_DEV_MODE": "1"})
    assert s.key_hex == k2 and s.peers == ["a", "b"] and s.dev_mode


@pytest.mark.parametrize("bad", ["", "zz" * 32, "ab" * 31, "ab" * 33])
def test_bad_keys_are_refused(bad):
    with pytest.raises(ValueError):
        cfg.parse_key(bad)


def test_key_header_is_valid_c():
    h = cfg.key_header(bytes(range(32)))
    assert "#define KK_NETWORK_KEY { 0x00, 0x01," in h and h.rstrip().endswith("0x1f }")


def test_hostport():
    assert split_hostport("bridge", 47474) == ("bridge", 47474)
    assert split_hostport("10.0.0.5:5000", 47474) == ("10.0.0.5", 5000)
    assert split_hostport("[::1]:99", 1) == ("::1", 99)
