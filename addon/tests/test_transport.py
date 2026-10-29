from kkaddon.transport import UdpTransport


class FakeSock:
    def __init__(self):
        self.out = []

    def sendto(self, raw, addr):
        self.out.append((raw, addr))


def make(hub=True, broadcast="255.255.255.255"):
    t = UdpTransport(47474, broadcast, ["a", "b"], hub)
    t._sock = FakeSock()
    t._resolved = [("10.0.0.1", 47474), ("10.0.0.2", 47474)]
    return t


def test_send_goes_to_broadcast_and_every_peer():
    t = make()
    t.send(b"x" * 30)
    assert [a for _, a in t._sock.out] == [("255.255.255.255", 47474), ("10.0.0.1", 47474), ("10.0.0.2", 47474)]
    t = make(broadcast="")
    t.send(b"x" * 30)
    assert [a for _, a in t._sock.out] == [("10.0.0.1", 47474), ("10.0.0.2", 47474)]


def test_oversized_frames_are_not_sent():
    t = make()
    t.send(b"x" * 251)
    assert t._sock.out == []


def test_hub_relays_to_the_other_peers_only():
    t = make()
    t.relay(b"frame", ("10.0.0.1", 5555))  # van peer 1: alleen naar peer 2, niet terug en niet naar broadcast
    assert [a for _, a in t._sock.out] == [("10.0.0.2", 47474)]
    t = make(hub=False)
    t.relay(b"frame", ("10.0.0.1", 5555))
    assert t._sock.out == []


def test_peers_info_ignores_own_addresses_and_marks_configured():
    t = make()
    t._own_ips = {"127.0.0.1", "10.0.0.9"}
    t.datagram_received(b"a", ("10.0.0.9", 1))  # eigen echo
    t.datagram_received(b"a", ("10.0.0.1", 1))
    t.datagram_received(b"a", ("10.0.0.7", 1))
    info = {p["ip"]: p for p in t.peers_info()}
    assert set(info) == {"10.0.0.1", "10.0.0.7"}
    assert info["10.0.0.1"]["configured"] and not info["10.0.0.7"]["configured"]
