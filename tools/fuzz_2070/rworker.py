"""Persistent render worker: reads JSON {text} lines, replies {status, etype, err}."""
import json
import signal
import sys
import warnings
from pathlib import Path

warnings.simplefilter("ignore")
import nf_metro  # noqa: E402

TREE = Path(sys.argv[1]).resolve()
assert str(Path(nf_metro.__file__).resolve()).startswith(str(TREE)), nf_metro.__file__
from nf_metro.api import render_string  # noqa: E402


def _alarm(s, f):
    raise TimeoutError("timeout")


signal.signal(signal.SIGALRM, _alarm)
for line in sys.stdin:
    req = json.loads(line)
    signal.alarm(60)
    try:
        svg = render_string(req["text"])
        assert svg.lstrip().startswith("<")
        rep = {"status": "ok"}
    except BaseException as e:  # noqa: BLE001
        rep = {"status": "error", "etype": type(e).__name__, "err": str(e).splitlines()[0][:300] if str(e) else ""}
    signal.alarm(0)
    sys.stdout.write(json.dumps(rep) + "\n")
    sys.stdout.flush()
