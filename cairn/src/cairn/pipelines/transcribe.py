"""Speech to text for audio and video files (files pipes): what a recording says, searchable like any document.

Used by ``documents.extract``, so a folder of call recordings, meetings or voice notes becomes text in the files
table, for SQL, search and AI. Transcription runs **locally** (Whisper, through ``faster-whisper``): recordings never
leave the deployment. It runs as its own process per file, with a time limit, on a recording whose length is read
from its header before anything is decoded; a recording that cannot be read gets the reason on its row and never
stops the pipe.

Settings (environment):

* ``CAIRN_TRANSCRIBE``: ``auto`` (default: on when ``faster-whisper`` and the model are there), ``on`` (a recording
  without them gets an error on its row) or ``off``;
* ``CAIRN_TRANSCRIBE_MODEL``: the Whisper model (``base``; ``tiny.en`` … ``large-v3``), or a directory holding one;
* ``CAIRN_TRANSCRIBE_MODEL_DIR``: where models are kept. Cairn never downloads one while loading data: put the
  model there beforehand (``cairn transcribe-model download``), so hosts with no internet work the same;
* ``CAIRN_TRANSCRIBE_LANGUAGE``: a language code (``en``, ``de`` …), or empty to detect it;
* ``CAIRN_TRANSCRIBE_MAX_SECONDS`` (3600): the longest recording transcribed; longer ones are noted, not read;
* ``CAIRN_TRANSCRIBE_TIMEOUT`` (1800): seconds per file;
* ``CAIRN_TRANSCRIBE_THREADS`` (2): CPU threads per file.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Optional

MEDIA = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wma", ".webm", ".mp4", ".mov", ".mkv",
         ".avi", ".m4v"}
MODEL = re.compile(r"^[A-Za-z0-9_.\-/]{1,200}$")


class TranscribeError(Exception):
    pass


def _int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def mode() -> str:
    m = os.environ.get("CAIRN_TRANSCRIBE", "auto").strip().lower()
    return m if m in ("auto", "on", "off") else "auto"


def model_name() -> str:
    m = os.environ.get("CAIRN_TRANSCRIBE_MODEL", "base").strip()
    if not MODEL.match(m) or ".." in m:
        raise TranscribeError(f"CAIRN_TRANSCRIBE_MODEL: a Whisper model name or directory (got {m[:40]!r})")
    return m


def model_dir() -> Optional[str]:
    return os.environ.get("CAIRN_TRANSCRIBE_MODEL_DIR", "").strip() or None


def installed() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def available() -> bool:
    return mode() != "off" and installed()


def unavailable() -> Optional[str]:
    """Why a recording gets no text: None when it is transcribed, or when transcription is not asked for."""
    if mode() == "on" and not installed():
        return "transcription needs faster-whisper (CAIRN_TRANSCRIBE=on), which is not installed"
    return None


def duration(path: str) -> float:
    """The recording's length in seconds, from its container's header (nothing is decoded)."""
    import av

    try:
        with av.open(path) as c:
            if c.duration is not None:
                return c.duration / 1_000_000
            streams = [s for s in c.streams if s.type == "audio" and s.duration and s.time_base]
            if streams:
                return float(streams[0].duration * streams[0].time_base)
            if not any(s.type == "audio" for s in c.streams):
                raise TranscribeError("the file has no audio")
    except av.error.FFmpegError as e:
        raise TranscribeError(f"not a recording that can be read ({type(e).__name__})") from e
    raise TranscribeError("the recording does not say how long it is")


def transcribe(name: str, data: bytes) -> tuple[Optional[str], Optional[float], Optional[str]]:
    """``(text, seconds, error)`` for one recording. Runs Whisper in its own process with a time limit."""
    if not available():
        return None, None, unavailable()
    ext = os.path.splitext(name.lower())[1]
    cap, limit = _int("CAIRN_TRANSCRIBE_MAX_SECONDS", 3600), _int("CAIRN_TRANSCRIBE_TIMEOUT", 1800)
    with tempfile.TemporaryDirectory(prefix="cairn-transcribe-") as d:
        path = os.path.join(d, "media" + ext)
        with open(path, "wb") as f:
            f.write(data)
        try:
            seconds = duration(path)
        except TranscribeError as e:
            return None, None, str(e)
        if seconds > cap:
            return None, seconds, f"a recording of {seconds:.0f} s is over the transcription limit ({cap} s)"
        try:
            model = model_name()
        except TranscribeError as e:
            return None, seconds, str(e)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": d, "HF_HUB_OFFLINE": "1",
               "OMP_NUM_THREADS": str(_int("CAIRN_TRANSCRIBE_THREADS", 2))}
        args = [sys.executable, "-I", "-m", "cairn.pipelines.transcribe", path, model, model_dir() or "",
                os.environ.get("CAIRN_TRANSCRIBE_LANGUAGE", "").strip(), str(_int("CAIRN_TRANSCRIBE_THREADS", 2))]
        try:
            r = subprocess.run(args, capture_output=True, timeout=limit, env={**env, "PYTHONPATH": _package_root()})
        except subprocess.TimeoutExpired:
            return None, seconds, f"transcription took over {limit} s"
        if r.returncode != 0:
            msg = r.stderr.decode("utf-8", "replace").strip().splitlines()
            return None, seconds, f"transcription: {(msg[-1] if msg else f'exit {r.returncode}')[:300]}"
        try:
            out = json.loads(r.stdout.decode("utf-8"))
        except ValueError:
            return None, seconds, "transcription gave no result"
        return out.get("text") or "", seconds, None


def _package_root() -> str:
    import cairn

    return os.path.dirname(os.path.dirname(os.path.abspath(cairn.__file__)))


def _main(argv: list[str]) -> int:
    """The child process: ``path model model_dir language threads`` -> JSON ``{"text", "language"}`` on stdout."""
    path, model, root, language, threads = argv[1], argv[2], argv[3] or None, argv[4] or None, int(argv[5])
    from faster_whisper import WhisperModel

    try:
        m = WhisperModel(model, device="cpu", compute_type="int8", download_root=root, cpu_threads=threads,
                         local_files_only=True)
    except Exception as e:  # noqa: BLE001 - the parent reports the last line
        print(f"the model {model} is not there ({type(e).__name__}): put it in CAIRN_TRANSCRIBE_MODEL_DIR", file=sys.stderr)
        return 3
    segments, info = m.transcribe(_samples(path), language=language, vad_filter=True, beam_size=1)
    text = " ".join(s.text.strip() for s in segments).strip()
    json.dump({"text": text, "language": info.language}, sys.stdout)
    return 0


def _samples(path: str):
    """The recording as 16 kHz mono float samples, which Whisper takes (decoded here, with any PyAV version)."""
    import av
    import numpy as np

    chunks = []
    with av.open(path) as c:
        stream = next((s for s in c.streams if s.type == "audio"), None)
        if stream is None:
            raise TranscribeError("the file has no audio")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in c.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def download(model: str, root: Optional[str]) -> str:
    """Fetches a model into ``root`` beforehand (the operator's step; loads never download)."""
    from faster_whisper import WhisperModel

    WhisperModel(model, device="cpu", compute_type="int8", download_root=root)
    return model


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
