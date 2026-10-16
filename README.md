# rhoai-docs

Downloads Red Hat OpenShift AI Self-Managed documentation from docs.redhat.com
for offline use, and keeps it current.

## Install

Python 3.9+ and five packages:

```bash
git clone <repo-url> && cd rhoai-docs-helper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

To run it from anywhere, put it on your PATH — `~/.local/bin` is already
there on most systems:

```bash
ln -s "$PWD/rhoai_docs.py" ~/.local/bin/rhoai-docs
rhoai-docs
```

The symlink keeps working after `git pull`. If you installed into a venv,
point the link at a one-line wrapper instead, so the right interpreter is
used no matter where you run it from:

```bash
printf '#!/bin/sh\nexec %s/.venv/bin/python %s/rhoai_docs.py "$@"\n' "$PWD" "$PWD" \
  > ~/.local/bin/rhoai-docs && chmod +x ~/.local/bin/rhoai-docs
```

### Your own settings

`rhoai_docs.yaml` next to the script holds the defaults and is tracked by
git, so editing it in place makes your checkout dirty. Keep your settings
outside the clone instead — the first of these that exists wins:

1. `$RHOAI_DOCS_CONFIG` — a specific file, handy for a one-off run
2. `~/.config/rhoai-docs/config.yaml`
3. the bundled `rhoai_docs.yaml`

```bash
mkdir -p ~/.config/rhoai-docs
cp rhoai_docs.yaml ~/.config/rhoai-docs/config.yaml   # then edit that copy
```

Any key you leave out falls back to the bundled file, then to the
built-in defaults.

## Usage

```bash
python3 rhoai_docs.py                     # interactive browser (default)
python3 rhoai_docs.py --check             # show what changed, download nothing
python3 rhoai_docs.py --all               # fetch everything missing or updated
python3 rhoai_docs.py --version 3.5       # work on one version only
python3 rhoai_docs.py --verify            # re-hash local files against the manifest
python3 rhoai_docs.py --output ~/my-docs  # override the output directory
```

Run `--help` for the full list.

## Configuration

`rhoai_docs.yaml`, read from next to the script — not the working directory.

```yaml
output_dir: ~/rhoai-docs
format: pdf          # pdf | html-single | both
versions: []         # e.g. [3.5, 3.4] to limit the menu
```

Settings resolve most-specific first: a CLI flag beats the yaml, which beats
the `DEFAULT_*` constants in the script (those apply only if the yaml is
missing or omits a key).

## What you get

```
~/rhoai-docs/
├── manifest.json            # hashes, sizes, publication dates, history
└── 3.5/
    ├── _INDEX.md            # the docs page's own category tree
    ├── _CHANGELOG.md        # append-only: what changed, and when
    ├── release_notes.pdf
    └── archive/             # superseded copies, never overwritten
```

## How updates are detected

Red Hat publishes no per-z-stream URL — a 3.5.2 release silently overwrites the
`3.5` docs in place. So each guide is compared against the manifest on two
signals: the PDF's own `/CreationDate` and its byte size. Size is what catches a
same-day re-publish that the date alone would miss.

Nothing is ever overwritten in place. A superseded guide moves to
`<version>/archive/` tagged with the publication date it carried, and when the
z-stream itself advances the whole directory is frozen as a hardlinked snapshot
(`3.5.1/`), which costs no extra disk for unchanged guides.

## Tests

```bash
python3 -m unittest test_rhoai_docs        # add -v to list them
```

Standard library only, no network: every test that would reach
docs.redhat.com stubs the request, so the suite is deterministic and runs in under a second.
