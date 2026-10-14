"""Open a search hit in the right app: PDFs at the page, notes in Obsidian."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pymupdf

if sys.platform == "win32":
    import winreg


def _default_pdf_exe() -> str | None:
    try:
        key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.pdf\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            progid = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{progid}\shell\open\command") as k:
            cmd = winreg.QueryValueEx(k, "")[0]
        return cmd.split('"')[1] if cmd.startswith('"') else cmd.split()[0]
    except OSError:
        return None


def _edge() -> str | None:
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if Path(p).exists():
            return p
    return shutil.which("msedge")


def _obsidian_vault(path: Path) -> Path | None:
    for parent in path.parents:
        if (parent / ".obsidian").is_dir():
            return parent
    return None


def _start(target: str | Path) -> None:
    """Hand a file or URI to the system, like double-clicking it."""
    if sys.platform == "win32":
        os.startfile(target)
    else:
        subprocess.Popen(["open", str(target)])


# Skim is the macOS PDF viewer that can be told which page to show.
_SKIM_GOTO = """on run argv
    tell application "Skim"
        activate
        set d to open (POSIX file (item 1 of argv))
        go d to page (item 2 of argv as integer) of d
    end tell
end run"""


def _skim() -> Path | None:
    for p in (Path("/Applications/Skim.app"), Path.home() / "Applications" / "Skim.app"):
        if p.exists():
            return p
    return None


# Preview has no scripting command to change page; preview_goto.applescript drives its
# Go > Go to Page… through System Events, which needs the Accessibility permission (macOS asks
# for it once). The file is opened first with `open` (no permissions needed), so if anything
# fails the PDF simply stays open on its first page, as before.
_PREVIEW_GOTO = Path(__file__).with_name("preview_goto.applescript")


def _numeral(n: int, style: str) -> str:
    if style == "D":
        return str(n)
    if style in ("R", "r"):
        out = ""
        for value, digits in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"),
                              (90, "xc"), (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"),
                              (4, "iv"), (1, "i")):
            while n >= value:
                out, n = out + digits, n - value
        return out.upper() if style == "R" else out
    if style in ("A", "a"):  # a..z, then aa..zz, ...
        letter = chr(ord("a") + (n - 1) % 26) * ((n - 1) // 26 + 1)
        return letter.upper() if style == "A" else letter
    return ""


def _decode(s: str) -> str:
    """MuPDF returns label prefixes as raw PDF strings: <FEFF0047002D> is "G-"."""
    if s.startswith("<") and s.endswith(">"):
        try:
            raw = bytes.fromhex(s[1:-1])
        except ValueError:
            return s
        if raw[:2] == b"\xfe\xff":
            return raw[2:].decode("utf-16-be", "replace")
        return raw.decode("latin-1")
    return s


def page_labels(path: Path) -> list[str]:
    """The printed label of every page (PDF page labels), or [] if the PDF defines none."""
    try:
        with pymupdf.open(path) as doc:
            rules = sorted(doc.get_page_labels(), key=lambda r: r["startpage"])
            count = doc.page_count
    except Exception:
        return []
    labels = [""] * count
    for i, rule in enumerate(rules):
        stop = rules[i + 1]["startpage"] if i + 1 < len(rules) else count
        prefix, first = _decode(rule.get("prefix", "")), rule.get("firstpagenum", 1)
        for page in range(rule["startpage"], min(stop, count)):
            labels[page] = prefix + _numeral(first + page - rule["startpage"], rule.get("style", ""))
    return labels


def preview_label(labels: list[str], page: int) -> str:
    """What to enter in Preview's Go to Page to land on, or next to, 1-based physical `page`.

    Preview reads it as a printed label when the PDF has labels (many books: cover "1",
    then ii, iii… and "1" again at the first page of content) and goes to the first page with
    it, while hits carry physical pages. A label that is empty or that an earlier page also has
    can't be typed: use the nearest one that can; the script then steps to the page itself.
    """
    if not labels or not 0 < page <= len(labels):
        return str(page)
    first: dict[str, int] = {}
    for i, label in enumerate(labels):
        first.setdefault(label, i)
    for distance in range(len(labels)):
        for i in (page - 1 - distance, page - 1 + distance):
            if 0 <= i < len(labels) and labels[i] and first[labels[i]] == i:
                return labels[i]
    return str(page)


_SETTINGS = "x-apple.systempreferences:com.apple.preference.security?"
_shown_settings: set[str] = set()


def _preview_goto(path: Path, page: int) -> str:
    """Open the PDF in Preview and jump to `page`; returns a note for the user, or ""."""
    subprocess.run(["open", "-a", "Preview", str(path)], timeout=15)
    label = preview_label(page_labels(path), page)
    try:
        r = subprocess.run(["osascript", str(_PREVIEW_GOTO), label, str(page), path.stem],
                           capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        print(f"Preview: couldn't go to page {page} (timed out)", file=sys.stderr)
        return ""
    if r.returncode == 0:
        return ""
    print(f"Preview: couldn't go to page {page}: {r.stderr.strip()}", file=sys.stderr)
    err = r.stderr.lower()
    if "-1743" in err:  # not allowed to send Apple events to System Events
        pane, where = "Privacy_Automation", "Automation (Foresight → System Events)"
    elif any(k in err for k in ("1002", "-1719", "-25211", "assistive", "not allowed", "permiso")):
        pane, where = "Privacy_Accessibility", "Accessibility"
    else:
        return ""  # Preview has the file open; only the jump failed
    if pane not in _shown_settings:  # take the user to the right switch, once per run
        _shown_settings.add(pane)
        _start(_SETTINGS + pane)
    return ("The PDF opened on its first page. To let Preview jump to the exact page, enable "
            f"Foresight in System Settings › Privacy & Security › {where} and open it again.")


def _open_pdf_mac(path: Path, page: int) -> str:
    if page and _skim():
        subprocess.Popen(["osascript", "-e", _SKIM_GOTO, str(path), str(page)])
        return ""
    if page:
        return _preview_goto(path, page)
    _start(path)
    return ""


def open_hit(corpus: Path, rel: str, page: int) -> str:
    """Open a hit; returns a note for the user (e.g. a missing permission), or ""."""
    path = (corpus / rel).resolve()
    if not path.is_relative_to(corpus.resolve()) or not path.exists():
        raise FileNotFoundError(rel)

    if path.suffix.lower() == ".md":
        vault = _obsidian_vault(path)
        if vault:
            file = path.relative_to(vault).with_suffix("").as_posix()
            _start(f"obsidian://open?vault={quote(vault.name)}&file={quote(file)}")
            return ""
        _start(path)
        return ""

    if sys.platform == "darwin":
        return _open_pdf_mac(path, page)

    exe = _default_pdf_exe()
    name = Path(exe).name.lower() if exe else ""
    if page and "sumatra" in name:
        subprocess.Popen([exe, "-reuse-instance", "-page", str(page), str(path)])
    elif page and ("acrobat" in name or "acrord" in name):
        subprocess.Popen([exe, "/A", f"page={page}", str(path)])
    elif page and (edge := _edge()):
        subprocess.Popen([edge, path.as_uri() + f"#page={page}"])
    else:
        os.startfile(path)
    return ""
