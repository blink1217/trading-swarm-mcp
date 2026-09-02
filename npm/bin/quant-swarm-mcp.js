#!/usr/bin/env node
/**
 * quant-swarm-mcp — Node launcher for the quant-swarm MCP servers.
 *
 * Usage:
 *   quant-swarm-mcp <server>                    stdio server via uvx
 *   quant-swarm-mcp --remote <data|warden|gym>  hosted streamable-HTTP via mcp-remote
 *
 * <server> is swarm-data-mcp | swarm-warden-mcp | swarm-gym-mcp (short
 * aliases: data | warden | gym). SWARM_MCP_ACCESS_TOKEN is forwarded as-is;
 * without one, tools still list and each call returns an access-required
 * envelope pointing at https://1.21initiative.com/mcp/.
 *
 * The stdio path requires uv (https://docs.astral.sh/uv/); the remote path
 * requires npx to resolve mcp-remote.
 */
'use strict';

const { spawn } = require('node:child_process');

const SERVERS = {
  data: 'swarm-data-mcp',
  warden: 'swarm-warden-mcp',
  gym: 'swarm-gym-mcp',
  'swarm-data-mcp': 'swarm-data-mcp',
  'swarm-warden-mcp': 'swarm-warden-mcp',
  'swarm-gym-mcp': 'swarm-gym-mcp',
};
const REMOTE_PATHS = { data: '/mcp/data', warden: '/mcp/warden', gym: '/mcp/gym' };
const REMOTE_BASE =
  process.env.SWARM_MCP_REMOTE_URL || 'https://swarm-mcp-503318750546.europe-west1.run.app';
const USAGE = [
  'usage:',
  '  quant-swarm-mcp <server>                     start a stdio server via uvx',
  '  quant-swarm-mcp --remote <data|warden|gym>   connect to the hosted endpoint',
  '',
  'servers: swarm-data-mcp | swarm-warden-mcp | swarm-gym-mcp (aliases: data | warden | gym)',
  'token:   set SWARM_MCP_ACCESS_TOKEN (free at https://1.21initiative.com/mcp/)',
].join('\n');

function fail(message) {
  process.stderr.write(`quant-swarm-mcp: ${message}\n`);
  process.exit(1);
}

function runNoShell(command, args, onMissing) {
  // No shell: argv is passed as-is, so nothing in it can be interpreted as
  // command syntax (uvx is a native executable on every platform).
  const child = spawn(command, args, { stdio: 'inherit', shell: false });
  child.on('error', (err) => {
    if (err.code === 'ENOENT') fail(onMissing);
    else fail(`${command} failed: ${err.message}`);
  });
  child.on('exit', (code) => process.exit(code ?? 1));
}

function runShell(command, args, onMissing) {
  // npx is npx.cmd on Windows and needs a shell; every argument passed here
  // is allowlisted or constructed server-side (never user free text).
  const child = spawn(command, args, { stdio: 'inherit', shell: process.platform === 'win32' });
  child.on('error', (err) => {
    if (err.code === 'ENOENT') fail(onMissing);
    else fail(`${command} failed: ${err.message}`);
  });
  child.on('exit', (code) => process.exit(code ?? 1));
}

const argv = process.argv.slice(2);
if (!argv.length || argv[0] === '--help' || argv[0] === '-h') {
  process.stdout.write(`${USAGE}\n`);
  process.exit(argv.length ? 0 : 2);
}
// Only the documented selectors are accepted — no free-form argument
// pass-through (the servers need none, and forwarding user text into a
// shell was an injection vector).
if (argv.length > 2 || (argv[0] === '--remote' && argv.length !== 2)) {
  fail(`unexpected arguments: ${argv.slice(1).join(' ')}\n\n${USAGE}`);
}

const token = process.env.SWARM_MCP_ACCESS_TOKEN || '';

if (argv[0] === '--remote') {
  const which = argv[1] || '';
  const remotePath = REMOTE_PATHS[which];
  if (!remotePath) {
    fail(`unknown remote server '${which}' — one of: data, warden, gym\n\n${USAGE}`);
  }
  const args = ['-y', 'mcp-remote', `${REMOTE_BASE}${remotePath}`];
  if (token) args.push('--header', `Authorization: Bearer ${token}`);
  runShell('npx', args,
    'npx not found — install Node.js (npm) to use the hosted endpoint, or use the stdio form with uv');
  return;
}

const server = SERVERS[argv[0]];
if (!server) {
  fail(`unknown server '${argv[0]}'\n\n${USAGE}`);
}
runNoShell('uvx', ['--from', 'quant-swarm', server],
  'uv not found — install it from https://docs.astral.sh/uv/ (the servers ship as the ' +
  'quant-swarm Python package); alternatively use: quant-swarm-mcp --remote data');
