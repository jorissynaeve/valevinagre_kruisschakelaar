#!/usr/bin/env python3
"""Stuurt een besturingsopdracht naar de simulator (zie sim/udp_sim.cpp), bv.:  simctl.py "press 1 0" """
import socket
import sys

host, port = "127.0.0.1", 47475
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(2)
s.sendto(" ".join(sys.argv[1:]).encode(), (host, port))
try:
    print(s.recvfrom(2048)[0].decode())
except socket.timeout:
    sys.exit("geen antwoord van de simulator (draait tools/demo.sh up?)")
