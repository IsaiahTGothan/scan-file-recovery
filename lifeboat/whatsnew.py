"""What changed in each version, as the app's "What's new" window shows it (newest first)."""

from __future__ import annotations

# version -> [(icon name, title, text)]
NOTES: dict[str, list[tuple[str, str, str]]] = {
    "1.2.0": [
        ("hdd", "Mac drives, on Windows too",
         "HFS+ and APFS drives open with their real names and folders, Time Machine disks included. Deleted files "
         "come back with their names, from the journal, older checkpoints and Deep scan."),
        ("lock", "Encrypted Mac volumes",
         "FileVault and “APFS (Encrypted)” volumes unlock with the Mac's password. Lifeboat decrypts them "
         "itself; the password stays in memory only."),
        ("palette", "Settings, redesigned",
         "Accent colors, file sizes as Windows or macOS counts them, date formats, Deep scan defaults, and a search "
         "box to find any setting."),
        ("star", "Plans are coming",
         "Free, Pro and Technician, with a free trial, open at launch. Until then every feature is unlocked."),
        ("check", "Self-test",
         "The app checks its decoders and encryption against published test answers: lifeboat-cli selftest."),
    ],
}


def latest() -> tuple[str, list[tuple[str, str, str]]]:
    """The newest version that has notes, and its notes."""
    version = max(NOTES, key=lambda v: tuple(int(p) for p in v.split(".")))
    return version, NOTES[version]
