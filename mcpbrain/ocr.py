"""Provisioning for the OCR dependency (the `tesseract` CLI).

Scanned, image-only PDFs have no text layer, so the only way to read them is
OCR. `sync/extractors.py` shells out to the standalone `tesseract` binary for
that, and treats it as an OPTIONAL external dependency — an image-only PDF
degrades to an empty text layer when it is missing, rather than failing.

Graceful degradation was the right call for robustness and the wrong one for
onboarding: nothing ever installed it, so every install silently had OCR off.
Measured on the author's store before this module existed: 247 PDF files held
under 200 characters of text in total (a scan with a header stamp and nothing
else), plus an uncountable number of fully image-only PDFs that produced no
chunks at all and were therefore invisible. Scanned PDFs skew heavily towards
signed contracts, letters, invoices and forms — the documents where content
matters most — so "optional" in the code should not have meant "absent" in
practice.

This module makes it installable from the two places that own machine setup:
`mcpbrain setup` (every platform's post-install step) and `mcpbrain doctor
--repair`. Both are best-effort: OCR being unavailable must never block
onboarding or fail a health check, because everything else still works without
it.
"""

import shutil
import subprocess
import sys

# Homebrew is the only sane install route on macOS and is user-level, but the
# daemon's launchd PATH typically omits its bin dir — the same reason
# extractors._TESSERACT_FALLBACK_PATHS exists. Resolve brew the same way rather
# than trusting PATH.
_BREW_PATHS = ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")

# UB Mannheim's build is the de-facto Windows distribution and is in winget's
# default source. --silent keeps `mcpbrain setup` non-interactive.
_WINGET_ARGS = ("install", "--id", "UB-Mannheim.TesseractOCR", "--silent",
                "--accept-package-agreements", "--accept-source-agreements")

# A first-time `brew install tesseract` pulls leptonica and friends; measured at
# roughly 1-2 minutes on a warm cache. Generous, but bounded: setup must not
# hang forever on a wedged package manager.
_TIMEOUT_S = 600


def tesseract_available() -> bool:
    """True when extractors can actually find a tesseract binary.

    Delegates to the resolver the extraction path itself uses, so this can never
    disagree with it — including its non-PATH fallback locations and the
    TESSERACT_BIN override.
    """
    from mcpbrain.sync.extractors import _tesseract_available, _tesseract_bin
    # The resolver memoises its answer, so a lookup from before an install would
    # otherwise stick for the life of the process.
    import mcpbrain.sync.extractors as _ex
    _ex._tesseract_cache = None
    _tesseract_bin()
    return _tesseract_available()


def _brew() -> str | None:
    return shutil.which("brew") or next((p for p in _BREW_PATHS
                                         if shutil.os.path.exists(p)), None)


def install_command(platform: str | None = None) -> list[str] | None:
    """The command that would install tesseract here, or None if we won't try.

    Returns None on Linux deliberately: every route there (`apt-get`, `dnf`,
    `pacman`) needs root, and `mcpbrain setup` runs as the user. Silently
    attempting `sudo` from a setup wizard would be a worse failure than telling
    the user one line to run.
    """
    platform = platform or sys.platform
    if platform == "darwin":
        brew = _brew()
        return [brew, "install", "tesseract"] if brew else None
    if platform == "win32":
        winget = shutil.which("winget")
        return [winget, *_WINGET_ARGS] if winget else None
    return None


def manual_hint(platform: str | None = None) -> str:
    """What to tell a user we cannot install it for."""
    platform = platform or sys.platform
    if platform == "darwin":
        return "install Homebrew (https://brew.sh), then: brew install tesseract"
    if platform == "win32":
        return ("install it from https://github.com/UB-Mannheim/tesseract/wiki "
                "(or: winget install UB-Mannheim.TesseractOCR)")
    return ("install it with your package manager, e.g. "
            "'sudo apt-get install -y tesseract-ocr' or 'sudo dnf install -y tesseract'")


def install_tesseract(platform: str | None = None, *,
                      timeout_s: int = _TIMEOUT_S) -> tuple[bool, str]:
    """Install tesseract if it is missing. Returns (ok, human-readable message).

    Never raises: a missing package manager, a non-zero exit, a timeout and a
    permission error all resolve to (False, why). OCR is optional, so a failure
    here is information, not an error.
    """
    if tesseract_available():
        return True, "OCR available (tesseract already installed)"
    cmd = install_command(platform)
    if cmd is None:
        return False, f"cannot install tesseract automatically here — {manual_hint(platform)}"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return False, (f"tesseract install timed out after {timeout_s}s "
                       f"— {manual_hint(platform)}")
    except Exception as exc:  # noqa: BLE001 — optional dependency; never fatal
        return False, f"tesseract install failed ({exc}) — {manual_hint(platform)}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit {proc.returncode}"
        return False, f"tesseract install failed ({tail}) — {manual_hint(platform)}"
    if not tesseract_available():
        # The package manager reported success but the binary is not resolvable —
        # e.g. installed somewhere neither PATH nor the fallback list covers.
        return False, ("tesseract installed but could not be located; set "
                       "TESSERACT_BIN to its full path")
    return True, "OCR enabled (tesseract installed)"
