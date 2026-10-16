# rhoai-docs

Downloads Red Hat OpenShift AI Self-Managed documentation from docs.redhat.com
for offline use, and keeps it current.

## Install

Python 3.9+ and five packages:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

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
