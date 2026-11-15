# Xiaoruo1 local VS Code workspace bridge

This extension runs inside the locally installed VS Code extension host. It
loads and edits workspace documents through VS Code APIs without displaying an
editor, moving focus, sending input, executing commands, or running terminals.
It is intended for a separate hidden local VS Code profile prepared by the
Xiaoruo1 host. It does not create a VM or replace VS Code with a parser.

Set `XIAORUO1_VSCODE_BRIDGE_CONFIG` to an absolute JSON configuration filename:

```json
{
  "port": 48912,
  "token_file": "C:\\private-config\\vscode-bridge.token",
  "allowed_roots": ["C:\\freechatmcp-workspace"]
}
```

The host must create a private token file containing a random secret of at
least 32 bytes. The bridge listens only on `127.0.0.1`, authenticates every
request using `Authorization: Bearer <secret>`, rejects browser-origin requests,
does not log credentials, and fails activation if the port is already occupied.
There is no unauthenticated or alternate-port fallback. Configuration and token
files cannot be accessed through document endpoints.

`GET /status` returns actual VS Code version, workspace, and loaded text document
state. `POST /request` accepts exactly these actions:

- `read_document`: existing absolute `path`; returns API text and disk SHA256.
- `write_document`: `path`, `text`, mandatory `expected_sha256` from the last read.
  Refuses dirty or changed documents and applies an in-memory WorkspaceEdit.
- `read_notebook`: existing `.ipynb` `path`; loads it with
  `workspace.openNotebookDocument` and returns cell sources, source SHA256, and
  existing outputs. The existing local Jupyter notebook serializer is required.
- `patch_notebook_cells`: `path`, `edits` containing explicit zero-based `index`,
  new `source`, and each cell's `expected_sha256`. Only code cells are editable.
  Edits target cell text documents, preserving other cells, metadata and outputs.
- `save_document`: saves only the exact unchanged document version modified by
  this bridge. Another edit or a disk change causes a conflict instead of a save.

Writes and cell edits remain unsaved until `save_document`. Each response states
whether it saved. Input is limited to 2 MiB; output is limited to 4 MiB. Existing
paths are resolved to their real locations and checked against allowed roots.
Symlinks and junctions cannot escape that scope.

This bridge does not execute notebooks or claim to select or attach to the
user's live kernel. Execution uses Xiaoruo1's separate selected-interpreter
kernel tool. No arbitrary VS Code command, authentication provider, extension
installation, shell, clipboard, editor display, or window focus API is exposed.

Dirty-buffer protection covers this VS Code instance. A separately launched
profile cannot inspect unsaved buffers owned by another VS Code process. When
another instance may have unsaved work, use an output copy or a bridge loaded in
that original instance; do not assume a matching disk hash covers unsaved work.
