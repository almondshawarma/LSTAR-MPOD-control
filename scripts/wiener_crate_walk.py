import argparse
import asyncio
import struct
import sys

try:
    from puresnmp import V2C, Client
    from x690.types import ObjectIdentifier
except ImportError:
    sys.exit("ERROR: puresnmp not installed.  Run:  pip install puresnmp")

CRATE_OID = "1.3.6.1.4.1.19947.1"


def decode_value(raw) -> str:
    raw_bytes = None
    try:
        raw_bytes = bytes(raw)
    except (TypeError, ValueError):
        pass

    # WIENER Opaque Float: BER tag 9f 78 04 + 4-byte big-endian IEEE 754
    if raw_bytes is not None:
        try:
            if len(raw_bytes) >= 9 and raw_bytes[0] == 0x44 and raw_bytes[2:5] == bytes([0x9f, 0x78, 0x04]):
                return f"Opaque: Float: {struct.unpack('>f', raw_bytes[5:9])[0]:.6f}"
            if len(raw_bytes) >= 7 and raw_bytes[0:3] == bytes([0x9f, 0x78, 0x04]):
                return f"Opaque: Float: {struct.unpack('>f', raw_bytes[3:7])[0]:.6f}"
        except struct.error:
            pass

    # Plain integer
    try:
        return f"INTEGER: {int(raw)}"
    except (TypeError, ValueError):
        pass

    # Printable ASCII string
    if raw_bytes is not None:
        try:
            s = raw_bytes.decode("ascii")
            if s.isprintable() or s == "":
                return f"STRING: \"{s}\""
        except UnicodeDecodeError:
            pass
        return f"Hex-STRING: {raw_bytes.hex(' ').upper()}"

    return repr(raw)


async def crate_walk(host: str, community: str, port: int, root: str) -> list[str]:
    client = Client(host, V2C(community), port=port)
    lines: list[str] = []
    async for vb in client.walk(ObjectIdentifier(root)):
        lines.append(f"{vb.oid} = {decode_value(vb.value)}")
    return lines


def main() -> None:
    p = argparse.ArgumentParser(
        description='SNMP walk of the WIENER-CRATE-MIB "crate" subtree.')
    p.add_argument("host", help="MPOD/crate IP address")
    p.add_argument("--community", default="public", help="SNMP read community (default: public)")
    p.add_argument("--port", type=int, default=161, help="SNMP UDP port (default: 161)")
    p.add_argument("--root", default=CRATE_OID,
                   help=f"Override walk root OID (default: {CRATE_OID} = 'crate')")
    p.add_argument("--out", default=None, metavar="FILE",
                   help="Save output to a text file (e.g. crate_dump.txt)")
    args = p.parse_args()

    print(f"\nWalking {args.root}  (crate)  on {args.host}:{args.port} ...\n")
    lines = asyncio.run(crate_walk(args.host, args.community, args.port, args.root))

    for line in lines:
        print(line)
    print(f"\nTotal OIDs: {len(lines)}")

    if args.out:
        with open(args.out, "w") as f:
            f.write(f"# WIENER crate walk - host={args.host}  root={args.root}\n")
            f.write("\n".join(lines) + "\n")
        print(f"Saved to {args.out}")


if __name__ == "__main__":
    main()
