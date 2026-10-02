#!/usr/bin/env python3
"""
Stand-in Modbus TCP device, for demonstrating the ingestion path without a PLC.

This is a *device* simulator, not a protocol simulator: it serves genuine Modbus
TCP on a socket, so `app.services.modbus_source` talks to it over the real
protocol with no test-mode branch anywhere in the application. Point that adapter
at a physical PLC, VFD or gateway instead and nothing in the app changes.

Implemented on the standard library alone, handling function code 3 (read
holding registers). Two reasons for hand-rolling rather than using pymodbus's
server:

  * pymodbus 3.15 deprecates the server context classes, and its replacement
    SimData API has no clean way to rewrite register values on a timer — which
    is this tool's whole purpose.
  * Using pymodbus on both ends would prove only that it agrees with itself.
    A stdlib server read by pymodbus's real client is a genuine cross-check of
    the frame format.

Three machines are published, one degrading, so ingestion can be seen producing
a rising risk score rather than a flat line:

    PLC-PUMP-01   healthy, oscillating around its reference
    PLC-CNC-02    healthy
    PLC-PRESS-03  degrading: vibration and temperature climb steadily

Register layout (holding registers, matching REGISTER_LAYOUT in the adapter):

    machine 0 -> addresses 0,1,2    vibration x100 | temperature x10 signed | rpm
    machine 1 -> addresses 3,4,5
    machine 2 -> addresses 6,7,8

Scaling exists because Modbus registers are 16-bit integers carrying no units:
2.45 mm/s has to travel as the integer 245, and both ends must agree on the
divisor. That agreement is what the MODBUS_*_SCALE settings express.

Run:
    python tools/modbus_plc_sim.py
    python tools/modbus_plc_sim.py --host 0.0.0.0 --port 5020 --interval 2
"""
import argparse
import asyncio
import logging
import math
import random
import struct
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("plc-sim")

REGISTERS_PER_MACHINE = 3

# (label, vibration mm/s, temperature C, rpm, degrading)
MACHINES = [
    ("PLC-PUMP-01", 2.4, 62.0, 1480, False),
    ("PLC-CNC-02", 1.8, 55.0, 2400, False),
    ("PLC-PRESS-03", 3.1, 68.0, 980, True),
]

VIBRATION_SCALE = 100.0
TEMPERATURE_SCALE = 10.0
RPM_SCALE = 1.0

# Ceilings for the degrading machine. Without them a long-running sim climbs
# without bound and ends up publishing 64 mm/s at 200 C, which no press would
# survive and which makes the demo data obviously fake. These sit well past
# ISO 10816-1 zone D (unacceptable starts around 7.1 mm/s for Class II), so the
# machine still trips every alarm — it just stays physically credible while
# doing it.
DEGRADED_VIBRATION_CEILING = 12.0   # mm/s
DEGRADED_TEMPERATURE_CEILING = 115.0  # C
DEGRADED_RPM_FLOOR = 0.75           # fraction of nominal

# Modbus function codes and exception codes used here.
FC_READ_HOLDING = 3
EXC_ILLEGAL_FUNCTION = 0x01
EXC_ILLEGAL_ADDRESS = 0x02
EXC_ILLEGAL_VALUE = 0x03

# The protocol caps a single FC3 response at 125 registers, because the byte
# count is one byte and the MBAP length field is bounded.
MAX_REGISTERS_PER_READ = 125

MBAP_HEADER_LEN = 7


def encode(value: float, scale: float) -> int:
    """Engineering value to one 16-bit register, two's complement for negatives.

    A real device does exactly this, which is why the adapter has to decode
    temperature as signed: nothing on the wire says that it is.
    """
    raw = int(round(value * scale))
    raw = max(-32768, min(32767, raw))
    return raw & 0xFFFF


def build_block(step: int) -> list:
    """Register values for one update cycle."""
    words = []
    for _label, vib, temp, rpm, degrading in MACHINES:
        if degrading:
            # Monotonic climb plus noise, reaching roughly ISO 10816 zone D
            # within a minute or two so the risk score has something real to
            # respond to, then levelling off so the values stay physically
            # credible however long this runs.
            progress = step * 0.04
            vib_now = min(vib + progress, DEGRADED_VIBRATION_CEILING)
            temp_now = min(temp + progress * 2.2, DEGRADED_TEMPERATURE_CEILING)
            rpm_now = max(rpm - progress * 3, rpm * DEGRADED_RPM_FLOOR)
        else:
            # A small oscillation, because a perfectly constant signal makes the
            # rolling std and slope features degenerate — not what a running
            # machine looks like.
            vib_now = vib + math.sin(step / 7.0) * 0.08 + random.uniform(-0.03, 0.03)
            temp_now = temp + math.sin(step / 11.0) * 0.9 + random.uniform(-0.2, 0.2)
            rpm_now = rpm + random.uniform(-6, 6)

        words.extend([
            encode(max(0.0, vib_now), VIBRATION_SCALE),
            encode(temp_now, TEMPERATURE_SCALE),
            encode(max(0.0, rpm_now), RPM_SCALE),
        ])
    return words


class HoldingRegisters:
    """The device's register memory, rewritten on a timer."""

    def __init__(self, values: list):
        self._values = list(values)

    def replace(self, values: list) -> None:
        self._values = list(values)

    def read(self, address: int, count: int):
        """Registers at `address`, or None if the range is not addressable.

        A real device answers an out-of-range read with exception code 2 rather
        than a short frame, so the caller turns None into that.
        """
        if address < 0 or count <= 0:
            return None
        if address + count > len(self._values):
            return None
        return self._values[address:address + count]

    def __len__(self) -> int:
        return len(self._values)


def exception_frame(txn: int, unit: int, func_code: int, code: int) -> bytes:
    """MBAP + exception response: the function code with its high bit set."""
    body = struct.pack(">BBB", unit, func_code | 0x80, code)
    return struct.pack(">HHH", txn, 0, len(body)) + body


def read_response_frame(txn: int, unit: int, registers) -> bytes:
    """MBAP + FC3 response: byte count, then big-endian registers."""
    payload = b"".join(struct.pack(">H", r & 0xFFFF) for r in registers)
    body = struct.pack(">BBB", unit, FC_READ_HOLDING, len(payload)) + payload
    return struct.pack(">HHH", txn, 0, len(body)) + body


def handle_request(frame: bytes, registers: HoldingRegisters):
    """One request frame in, one response frame out.

    Separated from the socket handling so the framing can be tested directly.
    """
    if len(frame) < MBAP_HEADER_LEN + 1:
        return None
    txn, proto, _length = struct.unpack(">HHH", frame[:6])
    unit = frame[6]
    func_code = frame[7]

    if proto != 0:
        return None
    if func_code != FC_READ_HOLDING:
        return exception_frame(txn, unit, func_code, EXC_ILLEGAL_FUNCTION)
    if len(frame) < MBAP_HEADER_LEN + 5:
        return exception_frame(txn, unit, func_code, EXC_ILLEGAL_VALUE)

    address, count = struct.unpack(">HH", frame[8:12])
    if count < 1 or count > MAX_REGISTERS_PER_READ:
        return exception_frame(txn, unit, func_code, EXC_ILLEGAL_VALUE)

    values = registers.read(address, count)
    if values is None:
        return exception_frame(txn, unit, func_code, EXC_ILLEGAL_ADDRESS)
    return read_response_frame(txn, unit, values)


async def serve_client(reader, writer, registers: HoldingRegisters) -> None:
    peer = writer.get_extra_info("peername")
    logger.info("client connected: %s", peer)
    try:
        while True:
            header = await reader.readexactly(MBAP_HEADER_LEN)
            length = struct.unpack(">H", header[4:6])[0]
            # The length field counts the unit id, which is already in header.
            remaining = max(0, length - 1)
            body = await reader.readexactly(remaining) if remaining else b""
            response = handle_request(header + body, registers)
            if response is None:
                break
            writer.write(response)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("client %s error: %s", peer, exc)
    finally:
        logger.info("client disconnected: %s", peer)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def drive(registers: HoldingRegisters, interval: float) -> None:
    """Rewrite the register values on a timer, as a running machine would."""
    step = 0
    try:
        while True:
            words = build_block(step)
            registers.replace(words)
            if step % 10 == 0:
                readable = ", ".join(
                    f"{MACHINES[i][0]}="
                    f"{words[i * REGISTERS_PER_MACHINE] / VIBRATION_SCALE:.2f}mm/s"
                    for i in range(len(MACHINES))
                )
                logger.info("step %s: %s", step, readable)
            step += 1
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Without this the task would die silently and the registers would
        # freeze while the server kept serving stale values — which looks like
        # a working device publishing a constant signal.
        logger.exception("register driver stopped unexpectedly")
        raise


async def main() -> int:
    parser = argparse.ArgumentParser(description="Modbus TCP stand-in device")
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (default loopback)")
    parser.add_argument("--port", type=int, default=5020,
                        help="TCP port; 502 is the registered Modbus port but "
                             "binding it needs privileges")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="seconds between register updates")
    args = parser.parse_args()

    registers = HoldingRegisters(build_block(0))
    driver = asyncio.create_task(drive(registers, args.interval))

    server = await asyncio.start_server(
        lambda r, w: serve_client(r, w, registers), args.host, args.port
    )

    logger.info(
        "Serving Modbus TCP on %s:%s - %s machines, %s holding registers, "
        "updating every %ss",
        args.host, args.port, len(MACHINES), len(registers), args.interval,
    )
    logger.info("Point the adapter at it with:")
    logger.info("  MODBUS_ENABLED=true")
    logger.info("  MODBUS_HOST=%s", args.host)
    logger.info("  MODBUS_PORT=%s", args.port)
    logger.info("  MODBUS_MACHINES=%s", ",".join(m[0] for m in MACHINES))

    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        driver.cancel()
        try:
            await driver
        except asyncio.CancelledError:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        logger.info("stopped")
