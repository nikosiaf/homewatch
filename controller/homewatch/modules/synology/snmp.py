"""Synology hardware status over SNMPv3 (SYNOLOGY-SYSTEM-MIB): what DSM's Web API doesn't report."""

from __future__ import annotations

# SYNOLOGY-SYSTEM-MIB, numeric so no MIB files are needed; 1 = normal, 2 = failed
OIDS = {
    "system": "1.3.6.1.4.1.6574.1.1.0",
    "power": "1.3.6.1.4.1.6574.1.3.0",
    "system_fan": "1.3.6.1.4.1.6574.1.4.1.0",
    "cpu_fan": "1.3.6.1.4.1.6574.1.4.2.0",
}
NAMES = {"system": "system", "power": "power supply", "system_fan": "system fan", "cpu_fan": "CPU fan"}
AUTH = ("SHA", "MD5", "SHA256")  # as DSM offers them; SHA is the default, MD5 only for old setups
PRIV = ("AES", "DES")


class SnmpError(Exception):
    pass


async def read(host: str, user: str, auth_key: str, priv_key: str, auth: str = "SHA", priv: str = "AES",
               timeout: float = 3) -> dict:
    """{name: 1 | 2 | None} for each OID; priv "" = no privacy (authNoPriv).
    Raises SnmpError when the NAS doesn't answer or refuses the user."""
    from pysnmp.hlapi.v3arch.asyncio import (ContextData, ObjectIdentity, ObjectType, SnmpEngine, UdpTransportTarget,
                                             UsmUserData, get_cmd, usmAesCfb128Protocol, usmDESPrivProtocol,
                                             usmHMAC192SHA256AuthProtocol, usmHMACMD5AuthProtocol,
                                             usmHMACSHAAuthProtocol, usmNoPrivProtocol)
    auth_p = {"SHA256": usmHMAC192SHA256AuthProtocol, "MD5": usmHMACMD5AuthProtocol}.get(auth, usmHMACSHAAuthProtocol)
    priv_p = {"AES": usmAesCfb128Protocol, "DES": usmDESPrivProtocol}.get(priv, usmNoPrivProtocol)
    engine = SnmpEngine()
    try:
        user_data = UsmUserData(user, auth_key, priv_key if priv else None, authProtocol=auth_p, privProtocol=priv_p)
        target = await UdpTransportTarget.create((host, 161), timeout=timeout, retries=1)
        err, status, _, binds = await get_cmd(engine, user_data, target, ContextData(),
                                              *(ObjectType(ObjectIdentity(o)) for o in OIDS.values()))
    except Exception as e:  # noqa: BLE001 - pysnmp raises many kinds; all mean "can't read"
        raise SnmpError(f"SNMP failed ({type(e).__name__}: {e})") from None
    finally:
        engine.close_dispatcher()
    if err:
        raise SnmpError(f"SNMP: {err}; check the user, the protocols and passwords, and UDP 161 from this host")
    if status:
        raise SnmpError(f"SNMP: the NAS answered {status.prettyPrint()}")
    out = {}
    for name, (_, value) in zip(OIDS, binds, strict=True):
        try:
            out[name] = int(value)
        except (TypeError, ValueError):
            out[name] = None  # noSuchObject: this model has no such sensor
    return out


def evaluate(hw: dict) -> tuple[str, list[str]]:
    """('ok' | 'failed', texts)."""
    failed = [NAMES[k] for k, v in hw.items() if v == 2]
    if failed:
        return "failed", [f"{n} failed" for n in failed]
    have = [NAMES[k] for k, v in hw.items() if v == 1]
    return "ok", [", ".join(have) + " normal" if have else "no hardware status reported"]
