"""Trust the local stack's forged TLS chain — import this before roborock/aiohttp.

Our tools talk to nothing but the local stack, whose TLS is minted by
apicn/mint_leaf.py. Python's ssl (aiohttp HTTP + paho MQTT) honors
SSL_CERT_FILE, so trust stays process-scoped instead of installing the
compromised chain system-wide.

Import order matters: aiohttp builds its default SSL context once, when
aiohttp.connector is first imported, so the variable must be set before
any roborock import.
"""
import os
import pathlib

ROOT_CERT = pathlib.Path(__file__).with_name("apicn") / "aaa-root.pem"
os.environ.setdefault("SSL_CERT_FILE", str(ROOT_CERT))
