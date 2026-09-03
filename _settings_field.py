"""One field out of data/settings.json, or nothing.

start.bat's equivalent of run.sh's settings_field() bash function — batch has
no inline heredoc, so this is the same handful of lines as its own file.
Prints nothing on a missing file, a missing field, an unreadable one, or an
empty value, so the caller's fallback chain (env var, this, hardcoded
default) behaves the same as the shell version.
"""
import json
import sys

path, key = sys.argv[1], sys.argv[2]
try:
    with open(path) as f:
        data = json.load(f)
except (OSError, ValueError):
    sys.exit(0)
value = data.get(key)
if value not in (None, ""):
    print(value)
