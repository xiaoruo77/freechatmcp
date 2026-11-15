'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');

const MAX_INPUT_BYTES = 2 * 1024 * 1024;
const MAX_OUTPUT_BYTES = 4 * 1024 * 1024;

class BridgeError extends Error {
  constructor(code, message, httpStatus = 400) {
    super(message);
    this.code = code;
    this.httpStatus = httpStatus;
  }
}

function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function normalizeDocumentEol(text, eol) {
  return text.replace(/\r\n|\r|\n/g, eol === 'CRLF' ? '\r\n' : '\n');
}

function insideRoot(candidate, root) {
  const relative = path.relative(root, candidate);
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
}

async function resolveAllowedPath(filename, roots, deniedPaths = []) {
  if (typeof filename !== 'string' || !path.isAbsolute(filename) || filename.includes('\0')) {
    throw new BridgeError('invalid_path', 'An absolute local file path is required');
  }
  let canonical;
  try {
    canonical = await fs.realpath(filename);
  } catch {
    throw new BridgeError('file_not_found', 'The requested existing file is unavailable', 404);
  }
  if (!roots.some(root => insideRoot(canonical, root))) {
    throw new BridgeError('path_outside_allowed_roots', 'The resolved path is outside allowed roots', 403);
  }
  if (deniedPaths.some(denied => path.relative(denied, canonical) === '')) {
    throw new BridgeError('protected_bridge_file', 'Bridge authentication and configuration files cannot be accessed', 403);
  }
  const stat = await fs.stat(canonical);
  if (!stat.isFile()) throw new BridgeError('not_a_file', 'The requested path must be a file');
  if (stat.size > MAX_INPUT_BYTES) throw new BridgeError('file_too_large', 'Input files are limited to 2 MiB', 413);
  return canonical;
}

function assertExpectedHash(value, expected, name = 'expected_sha256') {
  if (typeof expected !== 'string' || !/^[0-9a-f]{64}$/i.test(expected)) {
    throw new BridgeError('expected_hash_required', `${name} must contain a SHA256 digest`);
  }
  if (sha256(value) !== expected.toLowerCase()) {
    throw new BridgeError('content_conflict', 'Content changed after it was read; reread before editing', 409);
  }
}

function validateCellEdits(edits, cells) {
  if (!Array.isArray(edits) || edits.length === 0 || edits.length > 100) {
    throw new BridgeError('invalid_cell_edits', 'Provide between 1 and 100 explicit cell edits');
  }
  const indexes = new Set();
  let bytes = 0;
  return edits.map(edit => {
    if (!edit || !Number.isInteger(edit.index) || edit.index < 0 || edit.index >= cells.length || indexes.has(edit.index)) {
      throw new BridgeError('invalid_cell_index', 'Cell indexes must be valid and unique');
    }
    indexes.add(edit.index);
    const cell = cells[edit.index];
    if (cell.kind !== 'code') throw new BridgeError('code_cells_only', 'Only explicitly addressed code cells can be changed');
    if (typeof edit.source !== 'string') throw new BridgeError('invalid_cell_source', 'Cell source must be text');
    assertExpectedHash(cell.source, edit.expected_sha256, 'cell expected_sha256');
    bytes += Buffer.byteLength(edit.source, 'utf8');
    if (bytes > MAX_INPUT_BYTES) throw new BridgeError('input_too_large', 'Cell input exceeds 2 MiB', 413);
    return {index: edit.index, source: edit.source};
  });
}

module.exports = {BridgeError, MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, sha256, normalizeDocumentEol, insideRoot, resolveAllowedPath, assertExpectedHash, validateCellEdits};
