import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { createServer } from 'node:http';
import net from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const workerPath = resolve(
  repoRoot,
  'src',
  'client_runtime',
  'client-geometry-runtime.worker.js',
);

function findBrowser() {
  const candidates = [
    process.env.MALIEV_BROWSER_PATH,
    process.env.CHROME_PATH,
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    '/usr/bin/google-chrome',
    '/usr/bin/google-chrome-stable',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ].filter(Boolean);

  return candidates.find(path => existsSync(path)) ?? null;
}

function delay(ms) {
  return new Promise(resolveDelay => setTimeout(resolveDelay, ms));
}

async function stopBrowser(browser) {
  if (!browser || browser.exitCode !== null) return;
  browser.kill();
  await Promise.race([
    new Promise(resolveExit => browser.once('exit', resolveExit)),
    delay(3000),
  ]);
}

async function removeDirectory(path) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      rmSync(path, { recursive: true, force: true });
      return;
    } catch (error) {
      if (attempt === 4) throw error;
      await delay(200);
    }
  }
}

async function waitForDevToolsPort(
  userDataDir,
  {
    timeoutMs = 30_000,
    fileExists = existsSync,
    readFile = readFileSync,
    now = Date.now,
    wait = delay,
  } = {},
) {
  const activePortPath = resolve(userDataDir, 'DevToolsActivePort');
  const deadline = now() + timeoutMs;
  while (now() < deadline) {
    if (fileExists(activePortPath)) {
      const [port] = readFile(activePortPath, 'utf8').trim().split(/\r?\n/);
      return Number(port);
    }
    await wait(Math.min(50, deadline - now()));
  }
  throw new Error(
    `Timed out waiting for browser DevTools port after ${timeoutMs}ms.`,
  );
}

async function waitForPageWebSocket(port, pageUrl) {
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      const targets = await response.json();
      const page = targets.find(target => target.type === 'page' && target.url === pageUrl);
      if (page?.webSocketDebuggerUrl) return page.webSocketDebuggerUrl;
    } catch {
      // Browser is still starting.
    }
    await delay(50);
  }
  throw new Error('Timed out waiting for browser page target.');
}

function listen(server) {
  return new Promise((resolveListen, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => resolveListen(server.address().port));
  });
}

function encodeWebSocketFrame(text) {
  const payload = Buffer.from(text, 'utf8');
  const mask = randomBytes(4);
  const headerLength = payload.length < 126 ? 2 : payload.length <= 0xffff ? 4 : 10;
  const frame = Buffer.alloc(headerLength + 4 + payload.length);
  frame[0] = 0x81;
  if (payload.length < 126) {
    frame[1] = 0x80 | payload.length;
  } else if (payload.length <= 0xffff) {
    frame[1] = 0x80 | 126;
    frame.writeUInt16BE(payload.length, 2);
  } else {
    frame[1] = 0x80 | 127;
    frame.writeBigUInt64BE(BigInt(payload.length), 2);
  }
  mask.copy(frame, headerLength);
  for (let index = 0; index < payload.length; index += 1) {
    frame[headerLength + 4 + index] = payload[index] ^ mask[index % 4];
  }
  return frame;
}

class CdpSocket {
  constructor(webSocketUrl) {
    const url = new URL(webSocketUrl);
    this.host = url.hostname;
    this.port = Number(url.port);
    this.path = `${url.pathname}${url.search}`;
    this.nextId = 1;
    this.pending = new Map();
    this.buffer = Buffer.alloc(0);
  }

  connect() {
    return new Promise((resolveConnect, reject) => {
      this.socket = net.connect(this.port, this.host);
      const key = randomBytes(16).toString('base64');
      let handshake = '';

      this.socket.once('error', reject);
      this.socket.on('data', chunk => {
        if (!this.connected) {
          handshake += chunk.toString('binary');
          const headerEnd = handshake.indexOf('\r\n\r\n');
          if (headerEnd === -1) return;
          if (!handshake.startsWith('HTTP/1.1 101')) {
            reject(new Error(`WebSocket handshake failed: ${handshake.slice(0, 80)}`));
            return;
          }
          this.connected = true;
          this.socket.removeListener('error', reject);
          const remaining = Buffer.from(handshake.slice(headerEnd + 4), 'binary');
          if (remaining.length > 0) this.readFrames(remaining);
          resolveConnect();
          return;
        }
        this.readFrames(chunk);
      });

      this.socket.write(
        [
          `GET ${this.path} HTTP/1.1`,
          `Host: ${this.host}:${this.port}`,
          'Upgrade: websocket',
          'Connection: Upgrade',
          `Sec-WebSocket-Key: ${key}`,
          'Sec-WebSocket-Version: 13',
          '\r\n',
        ].join('\r\n'),
      );
    });
  }

  readFrames(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    while (this.buffer.length >= 2) {
      const lengthByte = this.buffer[1] & 0x7f;
      let offset = 2;
      let payloadLength = lengthByte;
      if (lengthByte === 126) {
        if (this.buffer.length < 4) return;
        payloadLength = this.buffer.readUInt16BE(2);
        offset = 4;
      } else if (lengthByte === 127) {
        if (this.buffer.length < 10) return;
        payloadLength = Number(this.buffer.readBigUInt64BE(2));
        offset = 10;
      }
      const frameLength = offset + payloadLength;
      if (this.buffer.length < frameLength) return;

      const opcode = this.buffer[0] & 0x0f;
      const payload = this.buffer.subarray(offset, frameLength);
      this.buffer = this.buffer.subarray(frameLength);
      if (opcode === 0x1) this.handleMessage(payload.toString('utf8'));
      if (opcode === 0x8) this.close();
    }
  }

  handleMessage(text) {
    const message = JSON.parse(text);
    if (!message.id) return;
    const pending = this.pending.get(message.id);
    if (!pending) return;
    this.pending.delete(message.id);
    if (message.error) {
      pending.reject(new Error(JSON.stringify(message.error)));
    } else {
      pending.resolve(message.result);
    }
  }

  send(method, params = {}) {
    const id = this.nextId;
    this.nextId += 1;
    const payload = JSON.stringify({ id, method, params });
    this.socket.write(encodeWebSocketFrame(payload));
    return new Promise((resolveSend, reject) => {
      this.pending.set(id, { resolve: resolveSend, reject });
    });
  }

  close() {
    this.socket?.destroy();
  }
}

async function evaluateWhenPageIsStable(cdp) {
  const params = {
    awaitPromise: true,
    returnByValue: true,
    expression: `new Promise((resolve, reject) => {
      const started = Date.now();
      const tick = () => {
        try {
          if (Date.now() - started >= 8000) {
            resolve({
              status: 'timeout',
              text: document.getElementById('result')?.textContent || ''
            });
            return;
          }
          const root = document.documentElement;
          if (!root) {
            setTimeout(tick, 50);
            return;
          }
          const status = root.dataset.status;
          if (status) {
            resolve({
              status,
              text: document.getElementById('result')?.textContent || ''
            });
            return;
          }
          setTimeout(tick, 50);
        } catch (error) {
          reject(error);
        }
      };
      tick();
    })`,
  };

  let lastFailure;

  for (let attempt = 0; attempt < 10; attempt += 1) {
    let evaluation;
    try {
      evaluation = await cdp.send('Runtime.evaluate', params);
    } catch (error) {
      if (!String(error.message).includes('Execution context was destroyed')) {
        throw error;
      }
      lastFailure = { kind: 'context_destroyed', error };
      await delay(100);
      continue;
    }

    const exceptionDescription = evaluation?.result?.description;
    if (evaluation?.exceptionDetails || evaluation?.result?.subtype === 'error') {
      throw new Error(
        `Runtime.evaluate JavaScript exception: ${exceptionDescription ?? 'unknown'}; ` +
          `response=${JSON.stringify(evaluation)}`,
      );
    }

    const value = evaluation?.result?.value;
    if (
      value !== null &&
      typeof value === 'object' &&
      typeof value.status === 'string' &&
      typeof value.text === 'string'
    ) {
      return evaluation;
    }

    lastFailure = { kind: 'incomplete_result', evaluation };
    await delay(100);
  }

  if (lastFailure?.kind === 'context_destroyed') {
    const errorDetails =
      lastFailure.error instanceof Error
        ? (lastFailure.error.stack ??
          `${lastFailure.error.name}: ${lastFailure.error.message}`)
        : String(lastFailure.error);
    throw new Error(
      'Browser page execution context did not stabilize; last CDP protocol error: ' +
        errorDetails,
      { cause: lastFailure.error },
    );
  }

  const serializedEvaluation = JSON.stringify(lastFailure?.evaluation);
  throw new Error(
    'Browser page execution context did not return a complete CDP result: ' +
      (serializedEvaluation ?? '<missing Runtime.evaluate response>'),
  );
}

function scriptedCdp(steps) {
  let calls = 0;
  return {
    cdp: {
      async send() {
        const step = steps[Math.min(calls, steps.length - 1)];
        calls += 1;
        if (step instanceof Error) throw step;
        return step;
      },
    },
    calls: () => calls,
  };
}

function passingEvaluation() {
  return {
    result: {
      type: 'object',
      value: {
        status: 'pass',
        text: '{"ok":true}',
      },
    },
  };
}

async function capturePageEvaluationExpression() {
  let expression;
  const cdp = {
    async send(method, params) {
      assert.equal(method, 'Runtime.evaluate');
      expression = params.expression;
      return passingEvaluation();
    },
  };

  await evaluateWhenPageIsStable(cdp);
  assert.equal(typeof expression, 'string');
  return expression;
}

test('DevTools port wait allows startup beyond ten seconds within its default bound', async () => {
  let elapsedMs = 0;

  const port = await waitForDevToolsPort('unused', {
    fileExists: () => elapsedMs >= 10_050,
    readFile: () => '9222\n/devtools/browser/test',
    now: () => elapsedMs,
    wait: async milliseconds => {
      elapsedMs += milliseconds;
    },
  });

  assert.equal(port, 9222);
  assert.equal(elapsedMs, 10_050);
  assert(elapsedMs < 30_000);
});

test('DevTools port wait remains bounded when the browser never starts', async () => {
  let elapsedMs = 0;

  await assert.rejects(
    waitForDevToolsPort('unused', {
      fileExists: () => false,
      readFile: () => '',
      now: () => elapsedMs,
      wait: async milliseconds => {
        elapsedMs += milliseconds;
      },
    }),
    /Timed out waiting for browser DevTools port/,
  );

  assert.equal(elapsedMs, 30_000);
});

test('CDP evaluation waits for the document root before reading page status', async () => {
  let documentRootReads = 0;
  const document = {
    get documentElement() {
      documentRootReads += 1;
      return documentRootReads === 1 ? null : { dataset: { status: 'pass' } };
    },
    getElementById() {
      return { textContent: '{"ok":true}' };
    },
  };
  const cdp = {
    async send(method, params) {
      assert.equal(method, 'Runtime.evaluate');
      const execute = Function(
        'document',
        'setTimeout',
        `return ${params.expression};`,
      );
      const value = await execute(document, callback => callback());
      return { result: { type: 'object', value } };
    },
  };

  const evaluation = await evaluateWhenPageIsStable(cdp);

  assert.equal(evaluation.result.value.status, 'pass');
  assert.equal(documentRootReads, 2);
});

test('page polling reaches its deadline while the document root remains absent', async () => {
  const expression = await capturePageEvaluationExpression();
  let elapsedMs = 0;
  let documentRootReads = 0;
  let scheduledTicks = 0;
  const document = {
    get documentElement() {
      documentRootReads += 1;
      return null;
    },
    getElementById() {
      return { textContent: 'pending' };
    },
  };
  const execute = Function(
    'document',
    'Date',
    'setTimeout',
    `return ${expression};`,
  );

  const value = await execute(
    document,
    { now: () => elapsedMs },
    callback => {
      scheduledTicks += 1;
      if (scheduledTicks > 1) {
        throw new Error('page polling continued after its eight-second deadline');
      }
      elapsedMs = 8_000;
      callback();
    },
  );

  assert.deepEqual(value, { status: 'timeout', text: 'pending' });
  assert.equal(documentRootReads, 1);
  assert.equal(scheduledTicks, 1);
});

test('page polling rejects an exception raised after an initially absent root', async () => {
  const expression = await capturePageEvaluationExpression();
  let documentRootReads = 0;
  let scheduledTick;
  const document = {
    get documentElement() {
      documentRootReads += 1;
      if (documentRootReads === 1) return null;
      throw new Error('post-null DOM failure');
    },
  };
  const execute = Function(
    'document',
    'Date',
    'setTimeout',
    `return ${expression};`,
  );
  const evaluation = execute(
    document,
    { now: () => 0 },
    callback => {
      scheduledTick = callback;
    },
  );
  const rejection = assert.rejects(evaluation, /post-null DOM failure/);

  assert.equal(documentRootReads, 1);
  assert.equal(typeof scheduledTick, 'function');
  assert.doesNotThrow(() => scheduledTick());
  await rejection;
  assert.equal(documentRootReads, 2);
});

test('CDP evaluation retries a destroyed execution context and then succeeds', async () => {
  const { cdp, calls } = scriptedCdp([
    new Error('Execution context was destroyed during navigation'),
    passingEvaluation(),
  ]);

  const evaluation = await evaluateWhenPageIsStable(cdp);

  assert.equal(evaluation.result.value.status, 'pass');
  assert.equal(calls(), 2);
});

test('CDP evaluation surfaces a non-context protocol error without retrying', async () => {
  const protocolError = new Error('CDP socket closed with code 1006');
  const { cdp, calls } = scriptedCdp([protocolError, passingEvaluation()]);

  await assert.rejects(evaluateWhenPageIsStable(cdp), error => error === protocolError);
  assert.equal(calls(), 1);
});

test('CDP evaluation retains the latest context error when retries are exhausted', async () => {
  const errors = Array.from(
    { length: 10 },
    (_, index) =>
      new Error(`Execution context was destroyed at transport attempt ${index + 1}`),
  );
  const { cdp, calls } = scriptedCdp(errors);

  await assert.rejects(evaluateWhenPageIsStable(cdp), error => {
    assert.match(error.message, /transport attempt 10/);
    assert.doesNotMatch(error.message, /undefined/);
    assert.equal(error.cause, errors[9]);
    return true;
  });
  assert.equal(calls(), 10);
});

test('CDP evaluation replaces a stale incomplete result with the latest context error', async () => {
  const incomplete = {
    diagnostic: 'stale-incomplete-envelope',
    result: {
      type: 'object',
      value: {},
    },
  };
  const contextErrors = Array.from(
    { length: 9 },
    (_, index) =>
      new Error(`Execution context was destroyed at later attempt ${index + 2}`),
  );
  const { cdp, calls } = scriptedCdp([incomplete, ...contextErrors]);

  await assert.rejects(evaluateWhenPageIsStable(cdp), error => {
    assert.match(error.message, /later attempt 10/);
    assert.doesNotMatch(error.message, /stale-incomplete-envelope/);
    assert.equal(error.cause, contextErrors[8]);
    return true;
  });
  assert.equal(calls(), 10);
});

test('CDP evaluation retains the complete final incomplete envelope on exhaustion', async () => {
  const incompleteResults = Array.from({ length: 10 }, (_, index) => ({
    transportAttempt: index + 1,
    diagnostic: {
      kind: 'incomplete_result',
      details: [`envelope-${index + 1}`],
    },
    result: {
      type: 'object',
      value: {},
    },
  }));
  const { cdp, calls } = scriptedCdp(incompleteResults);

  await assert.rejects(evaluateWhenPageIsStable(cdp), error => {
    assert.match(error.message, /"transportAttempt":10/);
    assert.match(error.message, /"details":\["envelope-10"\]/);
    assert.doesNotMatch(error.message, /"transportAttempt":9/);
    return true;
  });
  assert.equal(calls(), 10);
});

test('CDP evaluation retries a missing serialized transport result', async () => {
  let calls = 0;
  const cdp = {
    async send() {
      calls += 1;
      if (calls === 1) {
        return {
          result: {
            type: 'object',
            value: {},
          },
        };
      }
      return {
        result: {
          type: 'object',
          value: {
            status: 'pass',
            text: '{"ok":true}',
          },
        },
      };
    },
  };

  const evaluation = await evaluateWhenPageIsStable(cdp);

  assert.equal(evaluation.result.value.status, 'pass');
  assert.equal(calls, 2);
});

test('CDP evaluation surfaces JavaScript exception details without retrying', async () => {
  let calls = 0;
  const cdp = {
    async send() {
      calls += 1;
      return {
        result: {
          type: 'object',
          subtype: 'error',
          description:
            'Error: Execution context was destroyed by the evaluated page script',
        },
        exceptionDetails: {
          text: 'Uncaught',
          exceptionId: 1,
        },
      };
    },
  };

  await assert.rejects(
    evaluateWhenPageIsStable(cdp),
    /Execution context was destroyed by the evaluated page script.*exceptionDetails/,
  );
  assert.equal(calls, 1);
});

test('CDP evaluation returns a semantic DFM failure without retrying', async () => {
  let calls = 0;
  const cdp = {
    async send() {
      calls += 1;
      return {
        result: {
          type: 'object',
          value: {
            status: 'fail',
            text: '{"ok":false,"error":"worker assertion failed"}',
          },
        },
      };
    },
  };

  const evaluation = await evaluateWhenPageIsStable(cdp);

  assert.equal(evaluation.result.value.status, 'fail');
  assert.equal(calls, 1);
});

test('browser runtime executes in a real Web Worker and returns local-primary DFM', async t => {
  const browserPath = findBrowser();
  if (!browserPath) {
    t.skip('Chrome or Edge is required for the browser runtime smoke test.');
    return;
  }

  const workerSource = readFileSync(workerPath, 'utf8');
  const html = `<!doctype html>
<meta charset="utf-8">
<title>Geometry runtime browser smoke</title>
<pre id="result">pending</pre>
<script>
const result = document.getElementById('result');
const worker = new Worker('/worker.js');
worker.onmessage = event => {
  const message = event.data;
  result.textContent = JSON.stringify(message);
  document.documentElement.dataset.status =
    message.ok &&
    message.result.authority === 'local_primary' &&
    message.result.executionMode === 'primary_interactive' &&
    message.result.processCode === 'FDM' &&
    message.result.issues.some(issue => issue.category === 'overhang')
      ? 'pass'
      : 'fail';
  worker.terminate();
};
worker.onerror = event => {
  result.textContent = JSON.stringify({ ok: false, error: event.message });
  document.documentElement.dataset.status = 'fail';
  worker.terminate();
};
worker.postMessage({
  id: 'browser-smoke',
  processCode: 'FDM',
  input: {
    meshBuffers: {
      positions: [
        0, 0, 0,
        0, 0, 10,
        10, 0, 10,
        0, 10, 10
      ],
      indices: [0, 1, 2, 1, 3, 2]
    }
  }
});
</script>`;

  const server = createServer((request, response) => {
    if (request.url === '/worker.js') {
      response.writeHead(200, { 'content-type': 'text/javascript; charset=utf-8' });
      response.end(workerSource);
      return;
    }

    response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    response.end(html);
  });

  const appPort = await listen(server);
  const pageUrl = `http://127.0.0.1:${appPort}/`;
  const userDataDir = mkdtempSync(resolve(tmpdir(), 'maliev-geometry-browser-'));
  let browser;
  let cdp;

  try {
    browser = spawn(
      browserPath,
      [
        '--headless=new',
        '--disable-gpu',
        '--disable-extensions',
        '--disable-sync',
        '--disable-component-update',
        '--disable-background-networking',
        '--disable-default-apps',
        '--disable-features=MediaRouter,Translate,OptimizationHints',
        '--no-first-run',
        '--no-default-browser-check',
        '--remote-debugging-port=0',
        `--user-data-dir=${userDataDir}`,
        pageUrl,
      ],
      { stdio: 'ignore' },
    );

    const debugPort = await waitForDevToolsPort(userDataDir);
    const webSocketUrl = await waitForPageWebSocket(debugPort, pageUrl);
    cdp = new CdpSocket(webSocketUrl);
    await cdp.connect();

    const evaluation = await evaluateWhenPageIsStable(cdp);

    const value = evaluation.result.value;
    assert.equal(value.status, 'pass', value.text);
    const message = JSON.parse(value.text);
    assert.equal(message.ok, true);
    assert.equal(message.result.authority, 'local_primary');
    assert.equal(message.result.executionMode, 'primary_interactive');
    assert.equal(message.result.serverRole, 'fallback_and_final_validation');
    assert.equal(message.result.processCode, 'FDM');
    assert(message.result.issues.some(issue => issue.category === 'overhang'));
  } finally {
    cdp?.close();
    await stopBrowser(browser);
    await new Promise(resolveClose => server.close(resolveClose));
    await removeDirectory(userDataDir);
  }
});
