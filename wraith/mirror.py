"""
wraith.mirror — integrated scrcpy client (video in Wraith's OWN window).

Unlike the old design (external scrcpy.exe + global input hooks), this owns the
whole pipeline like QtScrcpy:
  * starts scrcpy-server over a REVERSE tunnel (device connects back to PC),
  * pulls a raw H.264 elementary stream (all metadata disabled) and decodes it
    with PyAV (its own parser finds frame boundaries — version-proof),
  * renders into a pygame/SDL window we control.

Input + control land on top of this in M2 (in-window capture, single channel).

scrcpy-server gotchas baked in here (learned the hard way):
  * scid is parsed as a SIGNED 32-bit hex int -> must be <= 0x7fffffff, else the
    server crashes on startup ("NumberFormatException ... under radix 16").
  * reverse tunnel: the PC listens first, then `adb reverse`, then the server
    connects back. Forward-tunnel video silently closes.
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import subprocess
import threading
import time

# Heavy native libs imported at MODULE level (main thread). Importing cv2/av for
# the first time inside a worker thread can deadlock on Windows (OpenCV spins up
# its thread pool at import) — that produced a reader that silently never ran.
import av
import cv2
import numpy as np

from pathlib import Path

from .control import (_adb_base, find_server_jar, detect_scrcpy_version, _run,
                      ScrcpyControl)
from .injector import Injector
from .keymap import Keymap
from .runtime import keymaps_dir, ffmpeg_path, NO_WINDOW

KEYMAPS_DIR = keymaps_dir()

# pygame key-name -> wraith key-name (matches keymap.py / capture.py vocabulary)
_KEY_FIXUP = {
    "escape": "esc", "return": "enter", "space": "space", "tab": "tab",
    "left ctrl": "ctrl", "right ctrl": "ctrl_r",
    "left shift": "shift", "right shift": "shift",
    "left alt": "alt", "right alt": "alt",
}


def _norm_key(pygame, keycode) -> str:
    n = pygame.key.name(keycode).lower()
    return _KEY_FIXUP.get(n, n)        # letters/digits/f-keys already correct


def list_devices() -> list[str]:
    """adb serials currently in 'device' state."""
    from .runtime import adb_path
    out = subprocess.run([adb_path(), "devices"], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout
    devs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devs.append(parts[0])
    return devs


def first_device() -> str | None:
    devs = list_devices()
    return devs[0] if devs else None

log = logging.getLogger("wraith.mirror")

REMOTE_JAR = "/data/local/tmp/wraith-scrcpy-server.jar"


class VideoStream:
    """Owns a scrcpy-server video connection; decodes to the latest RGB frame."""

    def __init__(self, serial: str | None, *, scid: str = "1234abcd",
                 port: int = 27300, max_size: int = 1920, max_fps: int = 60,
                 bitrate: int = 20_000_000, audio: bool = True,
                 codec: str = "h265"):
        assert int(scid, 16) <= 0x7FFFFFFF, "scid must be <= 0x7fffffff (server parses signed)"
        self.serial = serial
        self.scid = scid
        self.port = port
        self.max_size = max_size
        self.max_fps = max_fps
        self.bitrate = bitrate
        self.audio = audio
        # H.265/HEVC is ~40-50% more efficient than H.264 → far smaller frames in
        # complex/bright scenes (less transfer+decode → less latency), and the SD888
        # encoder + Intel HD620 decoder both do it in hardware. "h264" stays as a
        # fallback for devices/PCs without HEVC hw support.
        self.codec = codec
        self._av_codec = {"h265": "hevc", "h264": "h264", "av1": "av1"}.get(codec, "hevc")
        # Low-latency encoder tuning. CBR caps per-frame bits (complex scenes
        # can't spike a frame -> bounded latency); realtime priority; keyframe
        # every 1s so F12 recording can start fast. (Overridable for tuning.)
        self.codec_opts = os.environ.get(
            "WRAITH_CODEC_OPTS", "i-frame-interval:int=1,bitrate-mode:int=2")

        self._listener: socket.socket | None = None
        self._sock: socket.socket | None = None
        self.audio_sock: socket.socket | None = None  # raw PCM audio (if enabled)
        self.ctrl_sock: socket.socket | None = None   # control socket (same server)
        self.dev_w = 0                                 # device landscape res (W>H)
        self.dev_h = 0
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._run = False
        # render loop sets this while the window is minimized: keep DECODING
        # (so the stream stays at the live edge) but skip the YUV->RGB convert
        # + copy — the most expensive per-frame CPU work nobody can see.
        self.suspended = False

        self._lock = threading.Lock()
        self._frame = None          # latest RGB ndarray (H, W, 3)
        self._w = 0
        self._h = 0
        self._seq = 0               # bumps each new frame
        self.decoded = 0            # total frames decoded
        self.first_frame_evt = threading.Event()
        self.recorder = None        # set by run_window to tee encoded packets
        self.extradata = b""        # param sets (annex-b), captured from the stream
        self._vps = b""             # HEVC only
        self._sps = b""
        self._pps = b""

    # -- lifecycle ------------------------------------------------------------
    def start(self, connect_timeout: float = 6.0) -> bool:
        base = _adb_base(self.serial)
        ver = detect_scrcpy_version()
        jar = find_server_jar()
        subprocess.run(base + ["push", str(jar), REMOTE_JAR], capture_output=True,
                       creationflags=NO_WINDOW)

        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", self.port))
        self._listener.listen(8)
        self._listener.settimeout(connect_timeout)

        subprocess.run(base + ["reverse", f"localabstract:scrcpy_{self.scid}",
                               f"tcp:{self.port}"], capture_output=True,
                       creationflags=NO_WINDOW)

        cmd = base + [
            "shell", f"CLASSPATH={REMOTE_JAR}", "app_process", "/",
            "com.genymobile.scrcpy.Server", ver, f"scid={self.scid}",
            "log_level=info", f"audio={'true' if self.audio else 'false'}",
            "control=true", "video=true",
            f"video_codec={self.codec}", f"max_size={self.max_size}",
            f"video_bit_rate={self.bitrate}", f"max_fps={self.max_fps}",
            f"video_codec_options={self.codec_opts}",
            "cleanup=false",
            # raw streams: no metadata. video -> PyAV parses NALs; audio -> PCM.
            "send_device_meta=false", "send_codec_meta=false",
            "send_frame_meta=false", "send_dummy_byte=false",
        ]
        if self.audio:
            cmd.append("audio_codec=raw")          # raw PCM s16le 48k stereo
        # NOTE: server stdout -> DEVNULL. Piping it and draining in a thread can
        # deadlock the server on a blocked stdout write (zero video bytes then).
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL,
                                      creationflags=NO_WINDOW)

        # Server opens connections in a fixed order: video, [audio], control.
        try:
            self._sock, _ = self._listener.accept()
            if self.audio:
                self.audio_sock, _ = self._listener.accept()
            self.ctrl_sock, _ = self._listener.accept()
        except socket.timeout:
            log.error("device never connected back (reverse tunnel)")
            self.close()
            return False
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.ctrl_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self.audio_sock:
            self.audio_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        log.info("sockets connected (video%s + control)",
                 " + audio" if self.audio_sock else "")

        # device landscape resolution (W>H) for touch-coord scaling
        try:
            out = _run(base + ["shell", "wm", "size"])
            import re
            m = re.findall(r"(\d+)x(\d+)", out)
            w, h = int(m[-1][0]), int(m[-1][1])
            self.dev_w, self.dev_h = max(w, h), min(w, h)
        except Exception as exc:
            log.warning("wm size failed: %s", exc)
            self.dev_w, self.dev_h = 2400, 1080

        self._run = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        return True

    def _log_server(self):
        if not self._proc or not self._proc.stdout:
            return
        for line in self._proc.stdout:
            log.info("[server] %s", line.rstrip())

    def _scan_param_sets(self, data: bytes):
        """Cache the latest parameter sets (annex-B) so a recording can prepend
        them and stay decodable from frame 1. H.264: SPS(7)+PPS(8). HEVC:
        VPS(32)+SPS(33)+PPS(34). Must stay current — an orientation flip emits
        fresh sets for the new resolution. Only the head leads the access unit."""
        head = data[:512]
        i = head.find(b"\x00\x00\x01")
        if i < 0:
            return
        starts = []
        while i != -1:
            starts.append(i + 3)
            i = head.find(b"\x00\x00\x01", i + 3)
        hevc = self._av_codec == "hevc"
        changed = False
        for j, st in enumerate(starts):
            end = (starts[j + 1] - 3) if j + 1 < len(starts) else len(head)
            nal = head[st:end]
            if nal.endswith(b"\x00"):
                nal = nal[:-1]
            if not nal:
                continue
            if hevc:
                t = (nal[0] >> 1) & 0x3F        # HEVC: 2-byte NAL header
                if t == 32 and nal != self._vps[4:]:
                    self._vps = b"\x00\x00\x00\x01" + nal; changed = True
                elif t == 33 and nal != self._sps[4:]:
                    self._sps = b"\x00\x00\x00\x01" + nal; changed = True
                elif t == 34 and nal != self._pps[4:]:
                    self._pps = b"\x00\x00\x00\x01" + nal; changed = True
            else:
                t = nal[0] & 0x1F
                if t == 7 and nal != self._sps[4:]:
                    self._sps = b"\x00\x00\x00\x01" + nal; changed = True
                elif t == 8 and nal != self._pps[4:]:
                    self._pps = b"\x00\x00\x00\x01" + nal; changed = True
        if hevc:
            if changed and self._vps and self._sps and self._pps:
                self.extradata = self._vps + self._sps + self._pps
                log.info("HEVC param sets VPS+SPS+PPS updated")
        elif changed and self._sps and self._pps:
            self.extradata = self._sps + self._pps
            log.info("param sets updated SPS(%dB)+PPS(%dB)",
                     len(self._sps), len(self._pps))

    def _make_decoder(self):
        """HW-accelerated decoder (d3d11va/dxva2 ≈ 4-5x sw throughput, NO frame-
        threading so no added latency), for whatever codec the server negotiated
        (hevc/h264). HD620 (Kaby Lake Quick Sync) hw-decodes HEVC Main too.
        Why it matters: sw decode of complex game frames averaged ~78fps at 1920
        on this box — heavy scenes dipped under 60, the TCP stream backpressured,
        and latency built up. Set WRAITH_NO_HWDEC=1 (--no-hwdec) to force sw."""
        name = self._av_codec
        if os.environ.get("WRAITH_NO_HWDEC") != "1":
            try:
                from av.codec.hwaccel import HWAccel
                # d3d11va/dxva2 = Windows; videotoolbox = macOS. The wrong ones
                # for the platform just raise and are skipped.
                for dev in ("d3d11va", "dxva2", "videotoolbox"):
                    try:
                        hw = HWAccel(dev, allow_software_fallback=False)
                        dec = av.CodecContext.create(name, "r", hwaccel=hw)
                        log.info("hw decode: %s (%s)", dev, name)
                        return dec
                    except Exception as exc:
                        log.debug("hwaccel %s unavailable: %s", dev, exc)
            except Exception as exc:
                log.debug("no hwaccel API: %s", exc)
        log.info("software decode (%s)", name)
        return av.CodecContext.create(name, "r")

    @staticmethod
    def _to_rgb(frame):
        # hw decode hands back NV12 (transferred to CPU by to_ndarray); the sw
        # path stays I420. PyAV swscale is ENOSYS in this wheel -> cv2 in C.
        code = (cv2.COLOR_YUV2RGB_NV12 if frame.format.name == "nv12"
                else cv2.COLOR_YUV2RGB_I420)
        return cv2.cvtColor(frame.to_ndarray(), code)

    def _packet_is_keyframe(self, pb: bytes) -> bool:
        """Detect a random-access (keyframe) access unit by NAL type — PyAV's
        parser does NOT set pkt.is_keyframe for HEVC, so recording would never
        start. H.264: IDR = NAL type 5. HEVC: IRAP = NAL types 16-21 (the IDR
        slice follows the leading VPS/SPS/PPS, within the first ~512 bytes)."""
        head = pb[:512]
        hevc = self._av_codec == "hevc"
        i = head.find(b"\x00\x00\x01")
        while i != -1:
            st = i + 3
            if st < len(head):
                if hevc:
                    if 16 <= ((head[st] >> 1) & 0x3F) <= 21:
                        return True
                elif (head[st] & 0x1F) == 5:
                    return True
            i = head.find(b"\x00\x00\x01", st)
        return False

    def _read_loop(self):
        import traceback
        dec = self._make_decoder()
        hw_failsafe = True          # may swap to sw once if hw yields nothing
        total_bytes = 0
        try:
            while self._run:
                try:
                    chunk = self._sock.recv(1 << 16)
                except OSError as exc:
                    log.info("recv stopped: %s", exc)
                    break
                if not chunk:
                    log.info("stream ended (empty recv) after %d bytes", total_bytes)
                    break
                total_bytes += len(chunk)
                newest = None
                try:
                    for pkt in dec.parse(chunk):
                        frames = dec.decode(pkt)
                        pb = bytes(pkt)
                        self._scan_param_sets(pb)      # keep current SPS/PPS
                        rec = self.recorder
                        if rec is not None and rec.active:
                            if not rec.extradata and self.extradata:
                                rec.extradata = self.extradata
                            rec.write_video(pb, self._packet_is_keyframe(pb))
                            if rec._started:
                                rec._frames += len(frames)   # count real frames
                        for fr in frames:
                            newest = fr
                except Exception as exc:  # pragma: no cover
                    log.debug("decode error: %s", exc)
                    continue
                # failsafe: a hw context that creates fine but can't actually
                # decode would spin forever — after ~3MB with zero frames out,
                # swap to software (stream re-syncs at the next keyframe, ~1s).
                if hw_failsafe and self.decoded == 0 and total_bytes > 3_000_000:
                    hw_failsafe = False
                    log.warning("hw decoder produced nothing — falling back to sw")
                    os.environ["WRAITH_NO_HWDEC"] = "1"
                    dec = self._make_decoder()
                    continue
                if newest is not None:
                    if self.suspended:
                        continue           # minimized -> decoded, not converted
                    try:
                        arr = self._to_rgb(newest)
                    except Exception as exc:
                        log.error("convert failed (%s): %s", newest.format.name, exc)
                        continue
                    with self._lock:
                        self._frame = arr
                        self._h, self._w = arr.shape[0], arr.shape[1]
                        self._seq += 1
                        self.decoded += 1
                    if not self.first_frame_evt.is_set():
                        log.info("first frame decoded %dx%d", self._w, self._h)
                        self.first_frame_evt.set()
        except Exception:
            log.error("reader crashed:\n%s", traceback.format_exc())
        log.info("video reader stopped (decoded %d frames, %d bytes)",
                 self.decoded, total_bytes)

    def latest(self):
        """Return (rgb_ndarray, w, h, seq) or (None, 0, 0, 0)."""
        with self._lock:
            return self._frame, self._w, self._h, self._seq

    def make_control(self) -> ScrcpyControl:
        """ScrcpyControl wired to THIS session's control socket (no 2nd server).

        Reuses the proven touch-injection protocol; coords scale against the
        device landscape resolution (matches scrcpy's screenSize handling).
        """
        sc = ScrcpyControl(self.serial)
        sc.sock = self.ctrl_sock
        sc.alive = True              # shares THIS live session's socket (no 2nd
                                     # server) — it's connected, so don't let the
                                     # default False trip a false "DISCONNECTED".
        sc.width, sc.height = self.dev_w, self.dev_h
        return sc

    def set_screen_power(self, on: bool) -> None:
        """Turn the device display on/off while mirroring continues.
        SET_DISPLAY_POWER control message (type 10) + 1-byte bool."""
        if not self.ctrl_sock:
            return
        try:
            self.ctrl_sock.sendall(struct.pack(">BB", 10, 1 if on else 0))
        except OSError as exc:
            log.debug("set_screen_power failed: %s", exc)

    def close(self):
        self._run = False
        for s in (self._sock, self.audio_sock, self.ctrl_sock, self._listener):
            try:
                if s:
                    s.close()
            except Exception:
                pass
        self._sock = self.audio_sock = self.ctrl_sock = self._listener = None
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        try:
            subprocess.run(_adb_base(self.serial) +
                           ["reverse", "--remove", f"localabstract:scrcpy_{self.scid}"],
                           capture_output=True, creationflags=NO_WINDOW)
        except Exception:
            pass


class AudioPlayer:
    """Plays scrcpy's raw PCM audio (s16le, 48kHz, stereo) at low, bounded latency.

    A reader thread fills a ring buffer; a sounddevice callback drains it. If the
    buffer grows past ~80ms (we fell behind -> growing delay), the oldest audio
    is dropped so playback stays near the live edge instead of lagging.
    """
    SR = 48000
    CH = 2
    FRAME = CH * 2                       # bytes per stereo sample
    MAX_BYTES = int(0.08 * SR) * FRAME   # ~80ms latency cap

    def __init__(self, sock, gain: float = 1.0):
        self.sock = sock
        self.gain = max(0.0, gain)
        self._run = False
        self._thread = None
        self._stream = None
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.recorder = None        # set by run_window to tee raw PCM

    def start(self) -> bool:
        try:
            import sounddevice as sd
        except Exception as exc:
            log.warning("no audio (sounddevice unavailable): %s", exc)
            return False
        try:
            self._stream = sd.RawOutputStream(
                samplerate=self.SR, channels=self.CH, dtype="int16",
                blocksize=480, latency="low", callback=self._cb)   # 480 = 10ms
            self._stream.start()
        except Exception as exc:
            log.error("audio output open failed: %s", exc)
            return False
        self._run = True
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        return True

    def _cb(self, outdata, frames, t, status):
        need = frames * self.FRAME
        with self._lock:
            avail = min(need, len(self._buf))
            avail -= avail % self.FRAME
            chunk = bytes(self._buf[:avail])
            del self._buf[:avail]
        if self.gain != 1.0 and chunk:
            s = np.frombuffer(chunk, "<i2").astype(np.float32) * self.gain
            # soft limiter: quiet parts stay linear, peaks compress smoothly so a
            # big boost gets LOUD without the harsh distortion of hard clipping.
            lim = 32767.0
            s = lim * np.tanh(s / lim)
            chunk = s.astype("<i2").tobytes()
        outdata[:len(chunk)] = chunk
        if len(chunk) < need:                       # underrun -> pad silence
            outdata[len(chunk):need] = b"\x00" * (need - len(chunk))

    def _reader(self):
        try:
            cid = self.sock.recv(4)                 # skip 4-byte codec id
            log.info("audio codec id: %s", cid.hex())
        except OSError:
            return
        while self._run:
            try:
                data = self.sock.recv(16384)
            except OSError:
                break
            if not data:
                break
            if self.recorder is not None and self.recorder.active:
                self.recorder.write_audio(data)     # tee clean (un-boosted) PCM
            with self._lock:
                self._buf += data
                excess = len(self._buf) - self.MAX_BYTES
                if excess > 0:                      # behind -> drop oldest, stay live
                    del self._buf[:excess]
        log.info("audio stopped")

    def stop(self):
        self._run = False
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._stream:
            try:
                self._stream.stop(); self._stream.close()
            except Exception:
                pass


class PhoneMic:
    """Captures the DEVICE microphone via a SECOND scrcpy-server instance (audio
    only — no video, no control) as raw PCM s16le/48k/stereo, and tees it into
    the recorder so clips include the phone-side voice (what your friend hears),
    not the laptop mic. Independent scid/port so it never disturbs the main
    video + playback-audio stream.
    """
    SR = 48000
    CH = 2

    # mic-voice-communication = Android VOICE_COMMUNICATION source: echo
    # cancellation + noise suppression + auto-gain (tuned to capture a close
    # voice and reject ambient like the speaker playing the game). Falls back to
    # plain mic if the device's voice-comm source won't open.
    SOURCES = ("mic-voice-communication", "mic")

    def __init__(self, serial: str | None, *, scid: str = "2b2b2b2b",
                 port: int = 27301):
        assert int(scid, 16) <= 0x7FFFFFFF, "scid must be <= 0x7fffffff"
        self.serial = serial
        self.scid = scid
        self.port = port
        self._listener: socket.socket | None = None
        self._sock: socket.socket | None = None
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._run = False
        self.recorder = None          # set by run_window to tee mic PCM
        self.ok = False               # True once mic samples actually arrive
        self.source = None            # which audio_source actually connected

    def start(self, connect_timeout: float = 5.0) -> bool:
        base = _adb_base(self.serial)
        ver = detect_scrcpy_version()
        # the server jar is already on-device (pushed by VideoStream.start)
        for src in self.SOURCES:
            if self._open(base, ver, src, connect_timeout):
                self.source = src
                self._run = True
                self._reader = threading.Thread(target=self._read_loop, daemon=True)
                self._reader.start()
                log.info("phone-mic stream connected (source=%s)", src)
                return True
            log.warning("phone-mic source '%s' did not connect; trying next", src)
        log.warning("phone-mic capture off (no mic source would open — device "
                    "may deny RECORD_AUDIO)")
        return False

    def _open(self, base, ver, source, timeout) -> bool:
        """One connection attempt for a given audio_source. Cleans up on failure."""
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", self.port))
        self._listener.listen(2)
        self._listener.settimeout(timeout)
        subprocess.run(base + ["reverse", f"localabstract:scrcpy_{self.scid}",
                               f"tcp:{self.port}"], capture_output=True,
                       creationflags=NO_WINDOW)
        cmd = base + [
            "shell", f"CLASSPATH={REMOTE_JAR}", "app_process", "/",
            "com.genymobile.scrcpy.Server", ver, f"scid={self.scid}",
            "log_level=info", "audio=true", "video=false", "control=false",
            f"audio_source={source}", "audio_codec=raw", "cleanup=false",
            "send_device_meta=false", "send_codec_meta=false",
            "send_frame_meta=false", "send_dummy_byte=false",
        ]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL,
                                      creationflags=NO_WINDOW)
        try:
            self._sock, _ = self._listener.accept()   # audio-only -> one socket
        except socket.timeout:
            self._cleanup_attempt()
            return False
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return True

    def _cleanup_attempt(self):
        for s in (self._sock, self._listener):
            try:
                if s:
                    s.close()
            except Exception:
                pass
        self._sock = self._listener = None
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self._proc = None

    def _read_loop(self):
        try:
            self._sock.recv(4)            # skip 4-byte codec id (matches playback)
        except OSError:
            return
        while self._run:
            try:
                data = self._sock.recv(16384)
            except OSError:
                break
            if not data:
                break
            self.ok = True
            rec = self.recorder
            if rec is not None and rec.active:
                rec.write_mic(data)       # gated on first keyframe inside recorder
        log.info("phone-mic stream stopped")

    def stop(self):
        self.close()

    def close(self):
        self._run = False
        for s in (self._sock, self._listener):
            try:
                if s:
                    s.close()
            except Exception:
                pass
        self._sock = self._listener = None
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        try:
            subprocess.run(_adb_base(self.serial) +
                           ["reverse", "--remove", f"localabstract:scrcpy_{self.scid}"],
                           capture_output=True, creationflags=NO_WINDOW)
        except Exception:
            pass


class PCMic:
    """Captures YOUR voice from the PC microphone (where you actually sit) into
    recordings — via sounddevice, NOT the phone mic. This is the key to letting
    in-game voice chat keep working even WHILE recording: the phone's single mic
    is never touched by Wraith, so the game always has it. Your friend's voice
    comes into the clip through the game's own audio (the main output stream).

    Emits s16le / 48k / stereo to match the recorder's mic track (mono mics are
    duplicated to stereo). Starts instantly (no device round-trip), so a clip is
    mic-aligned from frame 1 — no late-join like the old phone-mic path.
    """
    SR = 48000
    CH = 2

    def __init__(self):
        self._stream = None
        self.recorder = None
        self.ok = False
        self._in_ch = 1

    def start(self) -> bool:
        try:
            import sounddevice as sd
        except Exception as exc:
            log.warning("no PC mic (sounddevice unavailable): %s", exc)
            return False
        try:
            info = sd.query_devices(kind="input")
            self._in_ch = 2 if int(info.get("max_input_channels", 1)) >= 2 else 1
        except Exception:
            self._in_ch = 1

        def cb(indata, frames, t, status):
            rec = self.recorder
            if rec is None or not rec.active:
                return
            buf = indata
            if self._in_ch == 1:                 # mono mic -> duplicate to stereo
                buf = np.repeat(buf, 2, axis=1)
            self.ok = True
            rec.write_mic(buf.tobytes())

        try:
            self._stream = sd.InputStream(samplerate=self.SR, channels=self._in_ch,
                                          dtype="int16", blocksize=480, callback=cb)
            self._stream.start()
        except Exception as exc:
            log.warning("PC mic open failed: %s", exc)
            self._stream = None
            return False
        log.info("PC mic capturing (%dch -> stereo)", self._in_ch)
        return True

    def stop(self):
        if self._stream:
            try:
                self._stream.stop(); self._stream.close()
            except Exception:
                pass
        self._stream = None


class Recorder:
    """Records the LIVE encoded H.264 + raw PCM to files, muxes to MP4 on stop.

    No re-encode (video is copied), so recording costs ~no extra CPU and zero
    extra work on the phone. Video teeing starts at the next keyframe (with the
    cached SPS/PPS prepended) so the file is decodable from frame 1.

    On stop the game audio is boosted (rec_gain) and, if mic capture is on, mixed
    with the PC microphone so both voices land in the clip — not just the phone's.
    """

    def __init__(self, save_dir: Path, fps: int, *, rec_gain: float = 1.0,
                 mic: bool = True, mic_gain: float = 1.0, vcodec: str = "hevc"):
        self.save_dir = save_dir
        self.fps = fps
        self.rec_gain = max(0.0, rec_gain)     # boost applied to game audio in file
        self.mic_enabled = mic
        self.mic_gain = max(0.0, mic_gain)
        self.vcodec = vcodec                   # "hevc" | "h264" — raw stream demuxer
        self._vext = "h265" if vcodec == "hevc" else "h264"
        self.active = False
        self.extradata = b""           # SPS/PPS, set by the video reader
        self._vf = None
        self._af = None
        self._base = None
        self._started = False          # wrote first keyframe yet?
        self._t0 = None                # wall-clock when first keyframe written
        self._frames = 0               # video packets written
        self._micf = None              # phone-mic PCM file (teed by PhoneMic)
        self._mic_ok = False           # any mic bytes actually written?

    def toggle(self) -> str | None:
        return self.stop() if self.active else self.start()

    def start(self) -> None:
        self.save_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        self._base = self.save_dir / f"wraith-{ts}"
        self._vf = open(f"{self._base}.{self._vext}", "wb")
        self._af = open(f"{self._base}.pcm", "wb")
        self._micf = open(f"{self._base}.mic.pcm", "wb") if self.mic_enabled else None
        self._mic_ok = False
        self._started = False
        self.extradata = b""           # re-grab CURRENT param sets (orientation!)
        self.active = True
        log.info("● recording -> %s.mp4 (waiting for keyframe)", self._base)
        return None

    def write_video(self, pkt_bytes: bytes, is_keyframe: bool):
        if not self.active or not self._vf:
            return
        if not self._started:
            if not is_keyframe:
                return                 # wait for a clean GOP boundary
            if self.extradata:
                self._vf.write(self.extradata)
            self._started = True
            self._t0 = time.monotonic()
            self._frames = 0
        self._vf.write(pkt_bytes)
        # NOTE: frame COUNT is tracked by the reader (decoded frames), not here —
        # counting packets would include SPS/PPS and inflate the muxed fps (drift).

    def write_audio(self, data: bytes):
        # only after video actually starts (first keyframe), so A/V start aligned
        if self.active and self._af and self._started:
            self._af.write(data)

    def write_mic(self, data: bytes):
        # phone-mic voice track; gated on first keyframe like write_audio so all
        # three streams (video, game audio, mic) start aligned.
        if self.active and self._micf and self._started:
            self._micf.write(data)
            self._mic_ok = True

    def stop(self) -> str | None:
        if not self.active:
            return None
        self.active = False
        for f in (self._vf, self._af, self._micf):
            try:
                if f:
                    f.close()
            except Exception:
                pass
        mic_pcm = None
        cand = f"{self._base}.mic.pcm"
        if self.mic_enabled and self._mic_ok and os.path.exists(cand) \
                and os.path.getsize(cand) > 0:
            mic_pcm = cand
        vid, pcm = f"{self._base}.{self._vext}", f"{self._base}.pcm"
        out = f"{self._base}.mp4"
        # mux at the ACTUAL average fps (frames / wall-clock) so the video
        # duration matches real time -> stays in sync with the realtime audio.
        rate = self.fps
        if self._t0 and self._frames > 1:
            dur = max(0.001, time.monotonic() - self._t0)
            rate = round(self._frames / dur, 3)
        # game audio is boosted in the file (the live boost never reached disk);
        # if the mic was captured, sum the two so both voices are in the clip.
        # normalize=0 keeps amix from auto-attenuating each input.
        # game audio boosted; mic (if any) mixed in WITHOUT normalize (amix would
        # otherwise halve each input). alimiter tames the summed peaks so the
        # loud game track doesn't clip in the file.
        if mic_pcm:
            filt = (f"[1:a]volume={self.rec_gain}[g];"
                    f"[2:a]volume={self.mic_gain}[m];"
                    f"[g][m]amix=inputs=2:normalize=0:dropout_transition=0[mx];"
                    f"[mx]alimiter=limit=0.95[a]")
        else:
            filt = f"[1:a]volume={self.rec_gain},alimiter=limit=0.95[a]"
        log.info("mux at %.3f fps (%d frames)%s", rate, self._frames,
                 " + mic" if mic_pcm else "")
        cmd = [ffmpeg_path(), "-y",
               "-r", str(rate), "-f", self.vcodec, "-i", vid,
               "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", pcm]
        if mic_pcm:
            cmd += ["-f", "s16le", "-ar", "48000", "-ac", "2", "-i", mic_pcm]
        cmd += ["-filter_complex", filt,
                "-map", "0:v:0", "-map", "[a]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", out]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                               creationflags=NO_WINDOW)
            if r.returncode == 0:
                log.info("saved recording: %s", out)
                for p in (vid, pcm, mic_pcm):
                    if not p:
                        continue
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            else:
                log.error("mux failed:\n%s", r.stderr[-800:])
        except Exception as exc:
            log.error("mux error: %s", exc)
        return out


def run_window(serial: str | None = None, keymap_name: str = "df.json",
               run_seconds: float | None = None, *, max_size: int = 1920,
               bitrate: int = 20_000_000, fps: int = 60, screen_off: bool = False,
               audio: bool = True, gain: float = 1.0, mic: bool = True,
               mic_gain: float = 1.0, show_toolbar: bool = True,
               save_dir: str | None = None, codec: str = "h265"):
    """M2: live video + in-window input. ONE control channel, no global hooks.

    Switch key (from the keymap, default Ctrl) toggles:
      * GAME MODE  — cursor hidden+grabbed, mouse = aim, keys = keymap touches.
      * MOUSE MODE — normal cursor; click the mirror to tap the phone (lobby).
    F10 opens the in-window keymap editor (drag-and-drop, saves live). F9 quits.
    Because input is read only from THIS window (not OS-global hooks), it can
    never lock your keyboard/mouse.
    """
    import pygame

    from .editor import KeymapEditor   # imports pygame -> keep out of launcher

    if serial is None:
        serial = first_device()
        if serial is None:
            print("no adb device connected")
            return

    keymap = Keymap.load(KEYMAPS_DIR / keymap_name)
    switch_key = keymap.switch_key or "ctrl"

    stream = VideoStream(serial, max_size=max_size, bitrate=bitrate, max_fps=fps,
                         audio=audio, codec=codec)
    if not stream.start():
        print("FAILED to start session")
        return
    if screen_off:
        stream.set_screen_power(False)

    audio_player = None
    if audio and stream.audio_sock:
        audio_player = AudioPlayer(stream.audio_sock, gain=gain)
        audio_player.start()

    # recorded game audio is boosted to match the live playback level; the mic
    # (if enabled) is mixed in so a clip captures both sides of the conversation.
    rec_dir = Path(save_dir) if save_dir else (Path.home() / "Videos" / "Wraith")
    recorder = Recorder(rec_dir, fps,
                        rec_gain=gain, mic=mic, mic_gain=mic_gain,
                        vcodec=stream._av_codec)
    stream.recorder = recorder
    if audio_player:
        audio_player.recorder = recorder

    # Your voice is recorded from the PC mic (see PCMic), opened only while a
    # recording runs. The phone's mic is never touched, so in-game voice chat
    # works during normal play AND while recording.
    pc_mic = None

    print("waiting for first frame...", flush=True)
    if not stream.first_frame_evt.wait(timeout=8.0):
        print("no frames decoded (is the phone screen on/moving?)")
        stream.close()
        return

    control = stream.make_control()

    _, fw, fh, _ = stream.latest()
    cur_w, cur_h = fw, fh
    # touch coords scale against the CURRENT video frame (matches scrcpy's
    # screenSize handling) so taps land correctly in any orientation.
    control.width, control.height = cur_w, cur_h
    injector = Injector(control, keymap)
    injector.start()

    pygame.init()
    try:
        from .runtime import icon_png
        ip = icon_png()
        if ip:
            pygame.display.set_icon(pygame.image.load(str(ip)))
    except Exception:
        pass
    info = pygame.display.Info()          # desktop size (before first set_mode)
    desk_w, desk_h = info.current_w, info.current_h
    screen = None

    def make_display(size, flags=pygame.RESIZABLE):
        """Create the display requesting VSYNC, so every present locks to the
        monitor's refresh = even frame pacing (no microjudder from the phone's
        slightly jittery frame arrival). DOUBLEBUF is needed for vsync to engage;
        fall back to a plain window if the driver won't give us a vsync surface."""
        try:
            return pygame.display.set_mode(size, flags | pygame.DOUBLEBUF, vsync=1)
        except pygame.error:
            return pygame.display.set_mode(size, flags)

    def fit_window(vw, vh, extra_w=0):
        """Size the window to the video aspect, capped to fit the screen, and
        re-center it. Called on first frame AND on orientation flips.
        extra_w widens it for the editor sidebar without squishing the video."""
        nonlocal screen
        # leave headroom for title bar + taskbar so it never exceeds the monitor
        scale = min(desk_w * 0.85 / vw, (desk_h - 80) * 0.92 / vh, 1.0)
        win = (max(240, int(vw * scale)) + extra_w, max(240, int(vh * scale)))
        os.environ["SDL_VIDEO_CENTERED"] = "1"   # center on (re)create
        screen = make_display(win)
        return win

    def video_dest(area_w, area_h, vw, vh):
        """Aspect-preserving centered rect for the video inside the window —
        maximized/fullscreen gets letterbox bars instead of a squeezed image."""
        s = min(area_w / vw, area_h / vh)
        dw, dh = max(1, int(vw * s)), max(1, int(vh * s))
        return pygame.Rect((area_w - dw) // 2, (area_h - dh) // 2, dw, dh)

    win = fit_window(fw, fh)
    print(f"first frame {fw}x{fh} — window {win} (keymap '{keymap.name}', "
          f"switch={switch_key.upper()}, F10=edit keymap)", flush=True)
    pygame.display.set_caption("Wraith — MOUSE MODE (press %s for game)" % switch_key.upper())
    clock = pygame.time.Clock()

    def rebuild_injector(nw, nh):
        """Orientation/size changed -> recompute keymap anchors for new dims."""
        nonlocal injector, cur_w, cur_h
        cur_w, cur_h = nw, nh
        control.width, control.height = nw, nh
        try:
            injector.stop()
        except Exception:
            pass
        injector = Injector(control, keymap)
        injector.start()
        log.info("re-fit injector for %dx%d", nw, nh)

    has_relmode = hasattr(pygame.mouse, "set_relative_mode")
    game_mode = False
    warp_ignore = False
    mdown = False                      # mouse-mode left button held (for drag)
    MOUSE_PID = 99                     # pointer id for mouse-mode touches
    edit_mode = False                  # F10: in-window keymap editor
    editor: KeymapEditor | None = None
    last_arr = None                    # latest decoded frame (editor redraws need it)

    def to_px(pos):
        # map through the letterboxed video rect (bars clamp to the edge)
        dst = video_dest(*screen.get_size(), cur_w, cur_h)
        nx = min(max((pos[0] - dst.x) / dst.w, 0.0), 1.0)
        ny = min(max((pos[1] - dst.y) / dst.h, 0.0), 1.0)
        return control.norm_to_px(nx, ny)

    def send_key(keycode):
        # INJECT_KEYCODE (type 0): action, keycode, repeat, metaState — down+up
        for action in (0, 1):
            if not control.sock:
                return
            try:
                control.sock.sendall(struct.pack(">BBiii", 0, action, keycode, 0, 0))
            except OSError as exc:
                control._mark_dead(exc)
                return

    def send_text(s):
        # INJECT_TEXT (type 1): 4-byte BE length + UTF-8 — types into the focused
        # phone text field via the IME (so in-game chat works with the real keyboard)
        data = s.encode("utf-8")
        try:
            control.sock.sendall(struct.pack(">BI", 1, len(data)) + data)
        except OSError:
            pass

    KEYCODE_HOME, KEYCODE_BACK = 3, 4
    # MOUSE-mode typing: printable chars -> INJECT_TEXT; these special keys ->
    # Android keycodes so backspace/enter/arrows behave in a text field.
    TEXT_KEYCODES = {
        pygame.K_BACKSPACE: 67, pygame.K_RETURN: 66, pygame.K_KP_ENTER: 66,
        pygame.K_TAB: 61, pygame.K_DELETE: 112, pygame.K_ESCAPE: 111,
        pygame.K_LEFT: 21, pygame.K_RIGHT: 22, pygame.K_UP: 19, pygame.K_DOWN: 20,
    }

    def type_into_phone(ev):
        kc = TEXT_KEYCODES.get(ev.key)
        if kc is not None:
            send_key(kc)
        elif ev.unicode and ev.unicode.isprintable():
            send_text(ev.unicode)

    # portrait nav sidebar (power/volume/screen/back/home/recents/notifications)
    tb = None
    if show_toolbar:
        from .toolbar import Toolbar

        def _expand_notif():
            if control.sock:
                try:
                    control.sock.sendall(struct.pack(">B", 5))  # EXPAND_NOTIFICATION_PANEL
                except OSError as exc:
                    control._mark_dead(exc)

        tb = Toolbar(send_key=send_key, set_screen=stream.set_screen_power,
                     expand_notif=_expand_notif)

    def toggle_record():
        """F12: start/stop recording. Your voice is captured from the PC mic
        (sounddevice), so the phone's mic is NEVER taken — in-game voice chat
        keeps working even while recording. Your friend's voice is already in
        the clip via the game's audio. PC mic starts instantly (no hitch)."""
        nonlocal pc_mic
        if recorder.active:
            path = recorder.stop()
            if pc_mic:
                pc_mic.stop()
                pc_mic = None
            return path
        recorder.start()
        if mic:
            pm = PCMic()
            if pm.start():
                pm.recorder = recorder
                pc_mic = pm
            # else: no PC mic -> clip keeps game + friend's voice only
        return None

    def update_caption():
        if not control.alive:
            pygame.display.set_caption(
                "Wraith — ⚠ PHONE DISCONNECTED   (F9 = quit, then relaunch)")
            return
        if edit_mode:
            mode = "EDIT MODE (drag keys onto the screen, F10 = play)"
        elif game_mode:
            mode = "GAME MODE (aim/keymap live)"
        else:
            mode = "MOUSE MODE (click=tap · type to chat · %s=game)" % switch_key.upper()
        rec = "   ● REC" if recorder.active else ""
        pygame.display.set_caption(f"Wraith — {mode}{rec}")

    def set_game_mode(on: bool):
        nonlocal game_mode
        game_mode = on
        pygame.event.set_grab(on)
        pygame.mouse.set_visible(not on)
        if has_relmode:
            pygame.mouse.set_relative_mode(on)
        if not on:
            # lift planted fingers (aim/joystick/holds): left down, the next
            # real tap becomes a two-finger pinch — DF's tactical map would
            # zoom instead of placing the barrage marker.
            injector.release_all()
        update_caption()

    def apply_keymap():
        """Editor changed the keymap -> re-apply it to the LIVE session."""
        nonlocal switch_key
        switch_key = keymap.switch_key or "ctrl"
        rebuild_injector(cur_w, cur_h)
        update_caption()

    def set_edit_mode(on: bool):
        """Toggle the drag-and-drop keymap editor. The window grows a sidebar;
        game/mouse input is suspended while editing, and edits go live on exit."""
        nonlocal edit_mode, editor, screen, mdown
        if on == edit_mode:
            return
        if on and game_mode:
            set_game_mode(False)
        if on and mdown:                       # don't leave a finger stuck down
            control.touch_up(MOUSE_PID, *to_px(pygame.mouse.get_pos()))
            mdown = False
        edit_mode = on
        w, h = screen.get_size()
        if on:
            if editor is None:
                editor = KeymapEditor(
                    keymap, KEYMAPS_DIR / keymap_name,
                    norm_key=lambda code: _norm_key(pygame, code),
                    get_frame=lambda: last_arr,
                    on_apply=apply_keymap)
            rebuild_injector(cur_w, cur_h)     # releases any held touches
            screen = make_display((w + KeymapEditor.SIDEBAR_W, h))
        else:
            screen = make_display((max(240, w - KeymapEditor.SIDEBAR_W), h))
            apply_keymap()                     # edits go live the moment you play
        update_caption()

    rendered = 0
    last_seq = -1
    prev_alive = control.alive
    t0 = time.monotonic()
    running = True
    while running:
        win_w, win_h = screen.get_size()
        video_rect = video_dest(
            max(1, win_w - (KeymapEditor.SIDEBAR_W if edit_mode else 0)),
            win_h, cur_w, cur_h)
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.VIDEORESIZE:
                screen = make_display(ev.size)
            elif ev.type == getattr(pygame, "WINDOWRESTORED", -1):
                # un-maximize / un-minimize -> snap the window back to the phone's
                # aspect. A maximized (wide) window leaves a portrait phone
                # pillarboxed, and restoring kept that wide shape = "stuck in
                # landscape"; re-fitting returns it to a portrait window.
                fit_window(cur_w, cur_h, KeymapEditor.SIDEBAR_W if edit_mode else 0)
            elif ev.type == pygame.KEYDOWN:
                name = _norm_key(pygame, ev.key)
                if name == "f10":          # in-window keymap editor
                    set_edit_mode(not edit_mode)
                elif edit_mode and editor.handle(ev, video_rect):
                    pass                   # editor consumed it (capture/edit key)
                elif name == "f12":        # record toggle (PrtSc is eaten by Windows)
                    path = toggle_record()
                    update_caption()
                    print("recording stopped:" if path else "recording started",
                          path or "", flush=True)
                elif name == "f9":
                    running = False
                elif edit_mode:
                    pass                   # never leak game keys while editing
                elif name == switch_key:
                    set_game_mode(not game_mode)
                elif game_mode:
                    injector.feed_key(name, True)
                else:
                    type_into_phone(ev)    # MOUSE mode: type into phone (chat!)
            elif edit_mode:
                if ev.type in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONDOWN,
                               pygame.MOUSEBUTTONUP, pygame.MOUSEWHEEL):
                    editor.handle(ev, video_rect)
            elif ev.type == pygame.KEYUP:
                if game_mode:
                    injector.feed_key(_norm_key(pygame, ev.key), False)
            elif ev.type == pygame.MOUSEMOTION:
                if tb and not game_mode:
                    tb.set_hover(ev.pos)
                if game_mode:
                    if warp_ignore:
                        warp_ignore = False
                    else:
                        dx, dy = ev.rel
                        if dx or dy:
                            injector.feed_mouse_delta(dx, dy)
                    if not has_relmode:
                        # keep cursor off the window edge (infinite travel)
                        win_w, win_h = screen.get_size()
                        mx, my = ev.pos
                        if mx < 8 or my < 8 or mx > win_w - 8 or my > win_h - 8:
                            warp_ignore = True
                            pygame.mouse.set_pos((win_w // 2, win_h // 2))
                elif mdown:
                    # MOUSE MODE drag: move the held finger
                    px, py = to_px(ev.pos)
                    control.touch_move(MOUSE_PID, px, py)
            elif ev.type == pygame.MOUSEBUTTONDOWN:
                btn = {1: "mouse_left", 2: "mouse_middle", 3: "mouse_right"}.get(ev.button)
                if (tb and not game_mode and ev.button == 1
                        and tb.visible(cur_w, cur_h, game_mode, edit_mode)
                        and tb.handle_click(ev.pos)):
                    pass                       # toolbar consumed the click
                elif game_mode and btn:
                    injector.feed_button(btn, True)
                elif not game_mode and ev.button == 1:
                    # MOUSE MODE: real finger DOWN (tap/drag) — single channel
                    px, py = to_px(ev.pos)
                    control.touch_down(MOUSE_PID, px, py)
                    mdown = True
                elif not game_mode and ev.button == 2:
                    send_key(KEYCODE_HOME)     # middle click -> HOME (like QtScrcpy)
                elif not game_mode and ev.button == 3:
                    send_key(KEYCODE_BACK)     # right click -> BACK
            elif ev.type == pygame.MOUSEBUTTONUP:
                btn = {1: "mouse_left", 2: "mouse_middle", 3: "mouse_right"}.get(ev.button)
                if game_mode and btn:
                    injector.feed_button(btn, False)
                elif not game_mode and ev.button == 1 and mdown:
                    px, py = to_px(ev.pos)
                    control.touch_up(MOUSE_PID, px, py)
                    mdown = False

        # minimized -> the reader skips YUV->RGB (decode-only keeps the stream
        # at the live edge so restoring is instant) and we idle the loop harder.
        stream.suspended = not pygame.display.get_active() and not edit_mode

        # surface a control-link drop (USB suspend / device doze or reboot during
        # a long idle session) in the title bar instead of crashing on the tap.
        if control.alive != prev_alive:
            update_caption()
            prev_alive = control.alive

        arr, fw, fh, seq = stream.latest()
        new_frame = arr is not None and seq != last_seq
        if new_frame:
            last_seq = seq
            if (fw, fh) != (cur_w, cur_h):
                # re-fit window on orientation flip (keep the editor sidebar)
                fit_window(fw, fh, KeymapEditor.SIDEBAR_W if edit_mode else 0)
                rebuild_injector(fw, fh)
            last_arr = arr

        if edit_mode:
            # redraw EVERY tick (markers/ghost follow the mouse even between
            # frames); FREEZE pins the captured frame like a screenshot.
            src = editor.frozen_arr if (editor.frozen and
                                        editor.frozen_arr is not None) else last_arr
            win_w, win_h = screen.get_size()
            video_rect = video_dest(max(1, win_w - KeymapEditor.SIDEBAR_W),
                                    win_h, cur_w, cur_h)
            screen.fill((10, 10, 12))
            if src is not None:
                sh, sw = src.shape[0], src.shape[1]
                surf = pygame.image.frombuffer(src.tobytes(), (sw, sh), "RGB")
                if video_rect.size != (sw, sh):
                    surf = pygame.transform.scale(surf, video_rect.size)
                screen.blit(surf, video_rect.topleft)
            editor.draw(screen, video_rect)
            pygame.display.flip()
            rendered += 1
        else:
            # redraw on a new frame, OR every tick while the portrait toolbar is
            # up (so hover/press feedback stays live on an otherwise static screen)
            tb_on = bool(tb and last_arr is not None
                         and tb.visible(cur_w, cur_h, game_mode, edit_mode))
            if new_frame or tb_on:
                src = last_arr
                sh, sw = src.shape[0], src.shape[1]
                surf = pygame.image.frombuffer(src.tobytes(), (sw, sh), "RGB")
                win_w, win_h = screen.get_size()
                dst = video_dest(win_w, win_h, sw, sh)
                if dst.size != (sw, sh):
                    surf = pygame.transform.scale(surf, dst.size)
                if dst.size != (win_w, win_h):
                    screen.fill((0, 0, 0))      # letterbox bars
                screen.blit(surf, dst.topleft)
                if tb_on:
                    tb.draw(screen, dst)
                pygame.display.flip()
                rendered += 1
        if run_seconds and (time.monotonic() - t0) >= run_seconds:
            running = False
        # 120Hz polling keeps input latency low while playing; a minimized
        # window only needs enough ticks to notice events + the restore.
        clock.tick(15 if stream.suspended else 120)

    dt = time.monotonic() - t0
    print(f"RESULT: decoded {stream.decoded} (~{stream.decoded/dt:.0f} fps), "
          f"rendered {rendered} over {dt:.1f}s", flush=True)
    if recorder.active:
        recorder.stop()                    # finalize the MP4 if still recording
    injector.stop()
    if audio_player:
        audio_player.stop()
    if pc_mic:
        pc_mic.stop()
    if screen_off:
        stream.set_screen_power(True)      # restore device screen on exit
    stream.close()
    pygame.quit()


def cli(argv=None):
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(prog="wraith.mirror",
                                 description="Integrated scrcpy client (video + keymap in one window).")
    ap.add_argument("--serial", help="adb serial (default: first device)")
    ap.add_argument("--keymap", default="df.json", help="keymap file in keymaps/")
    ap.add_argument("--max-size", type=int, default=1920)
    ap.add_argument("--bitrate", type=int, default=20_000_000)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--screen-off", action="store_true",
                    help="black out the phone screen while playing")
    ap.add_argument("--no-audio", action="store_true", help="disable audio")
    ap.add_argument("--gain", type=float, default=1.0,
                    help="audio volume multiplier (live playback AND recorded game audio)")
    ap.add_argument("--no-mic", action="store_true",
                    help="don't capture the PHONE microphone into recordings")
    ap.add_argument("--mic-gain", type=float, default=1.0,
                    help="phone-mic volume multiplier in recordings")
    ap.add_argument("--no-hwdec", action="store_true",
                    help="force software H.264 decode (hw d3d11va/dxva2 is default)")
    ap.add_argument("--codec", default="h265", choices=["h265", "h264"],
                    help="video codec (h265=HEVC, ~50%% more efficient, default)")
    ap.add_argument("--preset", choices=["auto", "low", "medium", "high"],
                    help="performance preset — overrides --max-size/--fps/--bitrate/"
                         "--codec. 'auto' probes THIS PC (hw decode + cores) and "
                         "picks the best fit; omit the flag for fully custom values")
    ap.add_argument("--no-toolbar", action="store_true",
                    help="hide the portrait nav sidebar in the mirror")
    ap.add_argument("--save-dir", help="folder for F12 recordings (default ~/Videos/Wraith)")
    ap.add_argument("--seconds", type=float, help="auto-quit after N seconds (testing)")
    a = ap.parse_args(argv)
    if a.no_hwdec:
        os.environ["WRAITH_NO_HWDEC"] = "1"   # before resolve() so the probe sees it
    if a.preset:
        from .perf import resolve
        p = resolve(a.preset)
        a.max_size, a.fps = p["max_size"], p["fps"]
        a.bitrate, a.codec = p["bitrate_mbps"] * 1_000_000, p["codec"]
    run_window(a.serial, keymap_name=a.keymap, run_seconds=a.seconds,
               max_size=a.max_size, bitrate=a.bitrate, fps=a.fps,
               screen_off=a.screen_off, audio=not a.no_audio, gain=a.gain,
               mic=not a.no_mic, mic_gain=a.mic_gain, show_toolbar=not a.no_toolbar,
               save_dir=a.save_dir, codec=a.codec)


if __name__ == "__main__":
    cli()
