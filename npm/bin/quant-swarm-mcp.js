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
  core: 'quant-swarm',
  'quant-swarm': 'quant-swarm',
  data: 'swarm-data-mcp',
  warden: 'swarm-warden-mcp',
  gym: 'swarm-gym-mcp',
  'swarm-data-mcp': 'swarm-data-mcp',
  'swarm-warden-mcp': 'swarm-warden-mcp',
  'swarm-gym-mcp': 'swarm-gym-mcp',
};
const REMOTE_PATHS = {
  core: '/mcp',
  'quant-swarm': '/mcp',
  data: '/mcp/data',
  warden: '/mcp/warden',
  gym: '/mcp/gym',
};
const REMOTE_BASE =
  process.env.SWARM_MCP_REMOTE_URL || 'https://mcp.1.21initiative.com';
const USAGE = [
  'usage:',
  '  quant-swarm-mcp [core|warden|data|gym]          start a stdio server via uvx (default: core)',
  '  quant-swarm-mcp --remote [core|warden|data|gym] connect to the hosted endpoint',
  '',
  'servers: quant-swarm (core, 12 tools) | swarm-warden-mcp | swarm-data-mcp | swarm-gym-mcp',
  'token:   optional locally; set SWARM_MCP_ACCESS_TOKEN for hosted PIT data (https://1.21initiative.com/mcp/)',
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
if (argv[0] === '--help' || argv[0] === '-h') {
  process.stdout.write(`${USAGE}\n`);
  process.exit(0);
}
const effectiveArgv = argv.length ? argv : ['core'];
if (effectiveArgv.length > 2 || (effectiveArgv[0] === '--remote' && effectiveArgv.length > 2)) {
  fail(`unexpected arguments: ${effectiveArgv.slice(1).join(' ')}\n\n${USAGE}`);
}

const token = process.env.SWARM_MCP_ACCESS_TOKEN || '';

if (effectiveArgv[0] === '--remote') {
  const which = effectiveArgv[1] || 'core';
  const remotePath = REMOTE_PATHS[which];
  if (!remotePath) {
    fail(`unknown remote server '${which}' — one of: core, warden, data, gym\n\n${USAGE}`);
  }
  const args = ['-y', 'mcp-remote', `${REMOTE_BASE}${remotePath}`];
  if (token) args.push('--header', `Authorization: Bearer ${token}`);
  runShell('npx', args,
    'npx not found — install Node.js (npm) to use the hosted endpoint, or use the stdio form with uv');
  return;
}

const server = SERVERS[effectiveArgv[0]];
if (!server) {
  fail(`unknown server '${effectiveArgv[0]}'\n\n${USAGE}`);
}
runNoShell('uvx', ['--from', 'quant-swarm==1.1.1', server],
  'uv not found — install it from https://docs.astral.sh/uv/ (the servers ship as the ' +
  'quant-swarm Python package); alternatively use: quant-swarm-mcp --remote core');
