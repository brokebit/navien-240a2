import time

from navien.source import Reader


class FlakySource:
    """A serial port that delivers bytes on some opens and goes silent on others,
    like ftdi_sio after it gives up on its read URBs ("urb stopped: -32")."""

    port = "/dev/fake"
    finished = False

    def __init__(self, silent_opens: set[int], bytes_per_open: int = 5) -> None:
        self.silent_opens = silent_opens          # 1-based open numbers that stay silent
        self.bytes_per_open = bytes_per_open
        self.opens = 0
        self.open_times: list[float] = []
        self._left = 0

    def open(self) -> None:
        self.opens += 1
        self.open_times.append(time.monotonic())
        self._left = 0 if self.opens in self.silent_opens else self.bytes_per_open

    def read(self) -> tuple[float, bytes]:
        time.sleep(0.005)
        if self._left:
            self._left -= 1
            return time.time(), b"\x00"
        return time.time(), b""

    def close(self) -> None:
        pass


def run_reader(source, until, timeout=5.0, **kw) -> Reader:
    r = Reader(source, on_frames=lambda frames: None, **kw)
    r.start()
    deadline = time.monotonic() + timeout
    while not until(source) and time.monotonic() < deadline:
        time.sleep(0.01)
    r.stop()
    assert until(source), "condition not reached"
    return r


def test_silent_port_is_reopened():
    src = FlakySource(silent_opens=set())
    r = run_reader(src, lambda s: s.opens >= 3, stall_s=0.1, stall_max_s=0.1)
    assert r.stall_reopens >= 2 and r.last_stall_reopen_at is not None
    assert r.bytes_total >= 10                     # each reopen brought the bytes back


def test_wait_doubles_while_silent_and_resets_when_data_returns():
    # opens 1-3 silent (waits 0.1, 0.2, 0.4 capped at 0.3); open 4 delivers ~25 ms of bytes
    # and then goes silent, so its wait starts again from stall_s
    src = FlakySource(silent_opens={1, 2, 3})
    run_reader(src, lambda s: s.opens >= 5, stall_s=0.1, stall_max_s=0.3)
    gaps = [b - a for a, b in zip(src.open_times, src.open_times[1:])]
    assert 0.08 < gaps[0] < 0.18
    assert 0.18 < gaps[1] < 0.28
    assert 0.28 < gaps[2] < 0.38                   # capped at stall_max_s
    assert 0.1 < gaps[3] < 0.2                     # back to stall_s after data


def test_no_reopen_when_disabled():
    src = FlakySource(silent_opens={1})
    r = Reader(src, on_frames=lambda frames: None, stall_s=0.0)
    r.start()
    time.sleep(0.3)
    r.stop()
    assert src.opens == 1 and r.stall_reopens == 0
