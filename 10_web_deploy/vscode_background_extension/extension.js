'use strict';

const vscode = require('vscode');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const {BridgeError, MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, sha256, normalizeDocumentEol, resolveAllowedPath, assertExpectedHash, validateCellEdits} = require('./bridge_helpers');

let server;

function authorized(header, token) {
  if (typeof header !== 'string') return false;
  const supplied = Buffer.from(header, 'utf8');
  const expected = Buffer.from(`Bearer ${token}`, 'utf8');
  return supplied.length === expected.length && crypto.timingSafeEqual(supplied, expected);
}

function respond(response, status, value) {
  let body = Buffer.from(JSON.stringify(value), 'utf8');
  if (body.length > MAX_OUTPUT_BYTES) {
    status = 413;
    body = Buffer.from(JSON.stringify({ok: false, error: {code: 'output_too_large', message: 'Response exceeds 4 MiB'}}));
  }
  response.writeHead(status, {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'});
  response.end(body);
}

async function requestBody(request) {
  const chunks = [];
  let length = 0;
  for await (const chunk of request) {
    length += chunk.length;
    if (length > MAX_INPUT_BYTES) throw new BridgeError('input_too_large', 'Request exceeds 2 MiB', 413);
    chunks.push(chunk);
  }
  try {
    const value = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error();
    return value;
  } catch {
    throw new BridgeError('invalid_json', 'Request body must be a JSON object');
  }
}

function wholeDocumentRange(document) {
  return new vscode.Range(document.positionAt(0), document.positionAt(document.getText().length));
}

async function activate(context) {
  const configPath = process.env.XIAORUO1_VSCODE_BRIDGE_CONFIG;
  if (!configPath) return;
  if (!path.isAbsolute(configPath)) throw new Error('Bridge configuration must use an absolute path');
  const config = JSON.parse(await fs.readFile(configPath, 'utf8'));
  if (!Number.isInteger(config.port) || config.port < 1024 || config.port > 65535) throw new Error('Bridge port must be an explicit nonprivileged local port');
  if (!Array.isArray(config.allowed_roots) || config.allowed_roots.length === 0 || config.allowed_roots.some(root => typeof root !== 'string' || !path.isAbsolute(root))) {
    throw new Error('Bridge allowed_roots must contain absolute directories');
  }
  if (typeof config.token_file !== 'string' || !path.isAbsolute(config.token_file)) throw new Error('Bridge token_file must be absolute');
  const roots = await Promise.all(config.allowed_roots.map(root => fs.realpath(root)));
  const denied = await Promise.all([configPath, config.token_file].map(filename => fs.realpath(filename)));
  const token = (await fs.readFile(config.token_file, 'utf8')).trim();
  if (Buffer.byteLength(token, 'utf8') < 32 || token.length > 4096 || /\s/.test(token)) throw new Error('Bridge token must be a strong whitespace-free secret');
  const checkedPath = filename => resolveAllowedPath(filename, roots, denied);
  const ownedEdits = new Map();

  function assertClean(document) {
    if (document.isDirty) throw new BridgeError('document_dirty', 'This VS Code instance has unsaved edits; refusing overwrite', 409);
  }

  async function checkUnchanged(filename, originalDisk, document, version) {
    const current = await checkedPath(filename);
    if (current !== filename || sha256(await fs.readFile(current)) !== sha256(originalDisk) || document.version !== version || document.isDirty) {
      throw new BridgeError('content_conflict', 'Document or disk content changed during the request', 409);
    }
  }

  async function readDocument(filename) {
    const canonical = await checkedPath(filename);
    const bytes = await fs.readFile(canonical);
    const document = await vscode.workspace.openTextDocument(vscode.Uri.file(canonical));
    return {path: canonical, text: document.getText(), sha256: sha256(bytes), text_sha256: sha256(document.getText()), language_id: document.languageId, version: document.version, is_dirty: document.isDirty, api: 'workspace.openTextDocument'};
  }

  async function readNotebook(filename) {
    const canonical = await checkedPath(filename);
    if (path.extname(canonical).toLowerCase() !== '.ipynb') throw new BridgeError('not_ipynb', 'Notebook actions require an .ipynb file');
    const notebook = await vscode.workspace.openNotebookDocument(vscode.Uri.file(canonical));
    return {path: canonical, sha256: sha256(await fs.readFile(canonical)), version: notebook.version, is_dirty: notebook.isDirty, notebook_type: notebook.notebookType, cells: notebook.getCells().map(cell => ({index: cell.index, kind: cell.kind === vscode.NotebookCellKind.Code ? 'code' : 'markdown', language_id: cell.document.languageId, source: cell.document.getText(), sha256: sha256(cell.document.getText()), outputs: cell.outputs.map(output => ({items: output.items.map(item => ({mime: item.mime, data_base64: Buffer.from(item.data).toString('base64')}))}))})), api: 'workspace.openNotebookDocument'};
  }

  async function action(payload) {
    const name = payload.action;
    if (name === 'read_document') return readDocument(payload.path);
    if (name === 'read_notebook') return readNotebook(payload.path);
    if (!['write_document', 'patch_notebook_cells', 'save_document'].includes(name)) throw new BridgeError('unsupported_action', 'The requested action is not available');
    const canonical = await checkedPath(payload.path);
    if (name === 'write_document') {
      if (path.extname(canonical).toLowerCase() === '.ipynb') throw new BridgeError('notebook_patch_required', 'Use explicit code-cell edits for notebooks');
      if (typeof payload.text !== 'string' || Buffer.byteLength(payload.text, 'utf8') > MAX_INPUT_BYTES) throw new BridgeError('invalid_text', 'Text must fit within 2 MiB');
      const originalDisk = await fs.readFile(canonical);
      assertExpectedHash(originalDisk, payload.expected_sha256);
      const document = await vscode.workspace.openTextDocument(vscode.Uri.file(canonical));
      assertClean(document);
      const eol = document.eol === vscode.EndOfLine.CRLF ? 'CRLF' : 'LF';
      const expectedText = normalizeDocumentEol(payload.text, eol);
      const version = document.version;
      await checkUnchanged(canonical, originalDisk, document, version);
      const edit = new vscode.WorkspaceEdit();
      edit.replace(document.uri, wholeDocumentRange(document), expectedText);
      if (!await vscode.workspace.applyEdit(edit)) throw new BridgeError('edit_rejected', 'VS Code rejected the edit', 409);
      ownedEdits.set(canonical, {kind: 'text', document, version: document.version, original_sha256: sha256(originalDisk), expected_text: expectedText});
      if (document.getText() !== expectedText) throw new BridgeError('edit_verification_failed', 'The document does not match the requested text', 409);
      return {path: canonical, saved: false, is_dirty: document.isDirty, version: document.version, eol, input_text_sha256: sha256(payload.text), text_sha256: sha256(document.getText()), api: 'workspace.applyEdit', next_action: 'save_document'};
    }
    if (name === 'patch_notebook_cells') {
      const originalDisk = await fs.readFile(canonical);
      if (path.extname(canonical).toLowerCase() !== '.ipynb') throw new BridgeError('not_ipynb', 'Notebook actions require an .ipynb file');
      const notebook = await vscode.workspace.openNotebookDocument(vscode.Uri.file(canonical));
      assertClean(notebook);
      const cells = notebook.getCells();
      if (cells.some(cell => cell.document.isDirty)) throw new BridgeError('document_dirty', 'A notebook cell has unsaved edits', 409);
      const originalSources = cells.map(cell => cell.document.getText());
      const prepared = validateCellEdits(payload.edits, cells.map(cell => ({kind: cell.kind === vscode.NotebookCellKind.Code ? 'code' : 'markdown', source: cell.document.getText()})));
      const expectedSources = originalSources.slice();
      for (const patch of prepared) expectedSources[patch.index] = patch.source;
      const version = notebook.version;
      await checkUnchanged(canonical, originalDisk, notebook, version);
      const edit = new vscode.WorkspaceEdit();
      for (const patch of prepared) {
        const cell = cells[patch.index];
        edit.replace(cell.document.uri, wholeDocumentRange(cell.document), patch.source);
      }
      if (!await vscode.workspace.applyEdit(edit)) throw new BridgeError('edit_rejected', 'VS Code rejected notebook cell edits', 409);
      ownedEdits.set(canonical, {kind: 'notebook', document: notebook, version: notebook.version, original_sha256: sha256(originalDisk), expected_sources: expectedSources});
      if (prepared.some(patch => notebook.cellAt(patch.index).document.getText() !== patch.source)) throw new BridgeError('edit_verification_failed', 'Notebook content does not match the explicit edits', 409);
      return {path: canonical, saved: false, is_dirty: notebook.isDirty, version: notebook.version, edited_cells: prepared.map(patch => ({index: patch.index, sha256: sha256(patch.source)})), api: 'workspace.applyEdit(cell.document.uri)', next_action: 'save_document'};
    }
    const owned = ownedEdits.get(canonical);
    if (!owned) {
      const open = vscode.workspace.notebookDocuments.find(document => document.uri.fsPath === canonical) || vscode.workspace.textDocuments.find(document => document.uri.scheme === 'file' && document.uri.fsPath === canonical);
      if (open && open.isDirty) throw new BridgeError('document_dirty', 'Unsaved content was not created by this bridge; refusing save', 409);
      return {path: canonical, saved: true, unchanged: true};
    }
    if (owned.document.version !== owned.version || sha256(await fs.readFile(canonical)) !== owned.original_sha256) throw new BridgeError('content_conflict', 'Document or disk changed after the bridge edit; refusing save', 409);
    const saved = await owned.document.save();
    if (!saved || owned.document.isDirty) throw new BridgeError('save_failed', 'VS Code did not confirm the document was saved', 409);
    if (owned.kind === 'text' && owned.document.getText() !== owned.expected_text) throw new BridgeError('saved_content_changed', 'A save participant changed the requested document content', 409);
    if (owned.kind === 'notebook' && (owned.document.cellCount !== owned.expected_sources.length || owned.document.getCells().some((cell, index) => cell.document.getText() !== owned.expected_sources[index]))) throw new BridgeError('saved_content_changed', 'A save participant changed notebook cell sources', 409);
    ownedEdits.delete(canonical);
    return {path: canonical, saved: true, sha256: sha256(await fs.readFile(canonical)), version: owned.document.version, api: owned.kind === 'notebook' ? 'NotebookDocument.save' : 'TextDocument.save'};
  }

  async function status() {
    const documents = [];
    for (const document of vscode.workspace.textDocuments.slice(0, 100)) {
      if (document.uri.scheme !== 'file') continue;
      try {
        const filename = await checkedPath(document.uri.fsPath);
        documents.push({path: filename, language_id: document.languageId, version: document.version, is_dirty: document.isDirty});
      } catch { /* Do not expose other workspace or authentication paths. */ }
    }
    return {vscode_version: vscode.version, extension_version: context.extension.packageJSON.version, workspace_folders: (vscode.workspace.workspaceFolders || []).map(folder => folder.uri.fsPath), documents, dirty_detection_scope: 'this_vscode_instance', notebook_execution: 'not_provided_use_separate_kernel_tool', ui_input: false};
  }

  // Serialize mutations from this bridge, keeping concurrent HTTP requests
  // from applying stale document versions to each other.
  let serial = Promise.resolve();
  server = http.createServer((request, response) => {
    const run = async () => {
      if (!authorized(request.headers.authorization, token)) return respond(response, 401, {ok: false, error: {code: 'unauthorized', message: 'Valid bridge authentication is required'}});
      if (request.headers.origin || request.headers['sec-fetch-site']) return respond(response, 403, {ok: false, error: {code: 'browser_requests_disallowed', message: 'Use the authenticated local tool client'}});
      const endpoint = request.url;
      if (request.method === 'GET' && endpoint === '/status') return respond(response, 200, {ok: true, result: await status()});
      if (request.method !== 'POST' || endpoint !== '/request') throw new BridgeError('not_found', 'Endpoint is unavailable', 404);
      const payload = await requestBody(request);
      const result = await action(payload);
      respond(response, 200, {ok: true, result});
    };
    const safeRun = async () => {
      try { await run(); }
      catch (error) {
        if (error instanceof BridgeError) respond(response, error.httpStatus, {ok: false, error: {code: error.code, message: error.message}});
        else respond(response, 500, {ok: false, error: {code: 'vscode_api_error', message: 'The local workspace API operation failed'}});
      }
    };
    serial = serial.then(safeRun, safeRun);
  });
  server.requestTimeout = 30000;
  server.headersTimeout = 10000;
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen({host: '127.0.0.1', port: config.port, exclusive: true}, resolve);
  });
  context.subscriptions.push({dispose: () => { if (server) server.close(); }});
}

function deactivate() {
  if (server) server.close();
}

module.exports = {activate, deactivate};
