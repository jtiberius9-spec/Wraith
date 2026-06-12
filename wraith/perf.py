"""
wraith.perf — performance presets + host capability autodetection.

Presets bundle the four knobs that matter on a weak PC (stream size, fps,
bitrate, codec) so users don't need to understand codecs to get a smooth
mirror. `detect()` probes what THIS machine can actually do:

  * hardware decode (d3d11va/dxva2 on Windows, videotoolbox on macOS) is the
    single biggest factor — software HEVC decode of game footage crushes old
    CPUs, while the same stream hw-decodes nearly for free;
  * CPU core count breaks the tie for everything that still runs in software
    (YUV->RGB conversion, audio, the render loop).

Used by both the launcher UI (fills the Start Config fields) and
`wraith.mirror --preset auto` (command-line runs).
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("wraith.perf")

# name -> the knobs. "high" == the historical defaults, so capable machines
# see zero change in behavior when they pick (or auto-detect lands on) it.
PRESETS: dict[str, dict] = {
    "low":    dict(max_size=1024, fps=30, bitrate_mbps=4,  codec="h264"),
    "medium": dict(max_size=1280, fps=60, bitrate_mbps=8,  codec="h265"),
    "high":   dict(max_size=1920, fps=60, bitrate_mbps=20, codec="h265"),
}


def _hw_decoders() -> set[str]:
    """Codecs ('h264'/'hevc') this PC can decode in hardware. Probing = just
    creating a hwaccel codec context (cheap, no device I/O); backends that are
    wrong for the platform raise and are skipped — same trick as mirror's
    _make_decoder, minus the software fallback."""
    found: set[str] = set()
    if os.environ.get("WRAITH_NO_HWDEC") == "1":
        return found
    try:
        import av
        from av.codec.hwaccel import HWAccel
    except Exception as exc:
        log.debug("PyAV unavailable for hw probe: %s", exc)
        return found
    for name in ("h264", "hevc"):
        for dev in ("d3d11va", "dxva2", "videotoolbox"):
            try:
                hw = HWAccel(dev, allow_software_fallback=False)
                av.CodecContext.create(name, "r", hwaccel=hw)
                found.add(name)
                break
            except Exception:
                continue
    return found


def detect() -> tuple[str, dict]:
    """(preset_name, values) best suited to THIS machine. Conservative on
    purpose: guessing too high means lag and a bad first impression; too low
    just looks slightly soft and is one click away from fixing."""
    cores = os.cpu_count() or 2
    hw = _hw_decoders()
    if "hevc" in hw and cores >= 6:
        choice = "high"
    elif hw and cores >= 4:        # h264-only hw decode, or hevc on few cores
        choice = "medium"
    else:                          # pure software decode -> keep frames small
        choice = "low"
    p = dict(PRESETS[choice])
    if p["codec"] == "h265" and "hevc" not in hw:
        p["codec"] = "h264"        # hevc would software-decode -> h264 instead
    log.info("auto preset: %s (cores=%d, hw decode=%s)",
             choice, cores, ",".join(sorted(hw)) or "none")
    return choice, p


def resolve(name: str) -> dict:
    """Values for a preset name; 'auto' probes the machine first."""
    if name == "auto":
        return detect()[1]
    return dict(PRESETS[name])
