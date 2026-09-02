/**
 * pi-mcp-adapter — MCP client bridge for pi.
 *
 * pi has no built-in MCP support. djinn wires every enabled plugin's MCP
 * servers into ~/.pi/agent/mcp.json (the same mcpServers shape Claude uses)
 * — that file is inert until this extension is installed. At session_start
 * it connects to each configured server, lists its tools, and registers them
 * as pi tools; a /mcp command reports per-server status.
 *
 * Servers are either stdio ({command, args, env}) or remote HTTP ({url,
 * headers}). ${VAR} refs anywhere in url/headers/command/args/env are
 * expanded from pi's process env — the agent shim sources
 * ~/.agent-keys/pi.env with `set -a`, so agent-scoped secrets reach servers
 * without ever being written into the config file. A ref with no matching
 * env var fails that one server with a clear error instead of silently
 * sending an empty credential.
 *
 * Global config only (~/.pi/agent/mcp.json): that is the file djinn owns.
 * Failures are per-server and never block pi startup.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { SSEClientTransport } from "@modelcontextprotocol/sdk/client/sse.js";
import { readFile } from "node:fs/promises";
import { join } from "node:path";

// ── Tunables ────────────────────────────────────────────────────────────────

const CLIENT_NAME = "pi-mcp-adapter";
const CLIENT_VERSION = "0.1.0";
const CONNECT_TIMEOUT_MS = 15_000;
const TOOL_CALL_TIMEOUT_MS = 120_000;
const LIST_TIMEOUT_MS = 15_000;

// ── Config types ────────────────────────────────────────────────────────────

/** One entry of ~/.pi/agent/mcp.json → mcpServers. Kept loose: djinn renders
 * stdio ({command, args, env}) and remote ({type, url, headers}) shapes. */
interface McpServerSpec {
	command?: unknown;
	args?: unknown;
	env?: unknown;
	url?: unknown;
	headers?: unknown;
	type?: unknown;
}

interface McpTool {
	name: string;
	description?: string;
	inputSchema?: unknown;
}

interface ConfigFile {
	mcpServers?: Record<string, unknown>;
}

// ── ${VAR} expansion ────────────────────────────────────────────────────────

const REF_RE = /\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g;

interface Expanded {
	value: unknown;
	missing: string[];
}

/** Expand ${VAR} refs from pi's process env, recursively; collect refs that
 * resolve to nothing so the caller can fail the server with a precise
 * message. Non-string leaves pass through untouched. */
function expandDeep(node: unknown): Expanded {
	if (typeof node === "string") {
		const missing: string[] = [];
		const value = node.replace(REF_RE, (ref, name: string) => {
			const v = process.env[name];
			if (v === undefined || v === "") {
				missing.push(name);
				return "";
			}
			return v;
		});
		return { value, missing };
	}
	if (Array.isArray(node)) {
		const out: unknown[] = [];
		const missing: string[] = [];
		for (const item of node) {
			const r = expandDeep(item);
			out.push(r.value);
			missing.push(...r.missing);
		}
		return { value: out, missing };
	}
	if (node && typeof node === "object") {
		const out: Record<string, unknown> = {};
		const missing: string[] = [];
		for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
			const r = expandDeep(v);
			out[k] = r.value;
			missing.push(...r.missing);
		}
		return { value: out, missing };
	}
	return { value: node, missing: [] };
}

/** Strip ${VAR} refs from text shown to the LLM or user: the resolved values
 * are env secrets; the bare names are harmless but noisy. */
function redactRefs(text: string): string {
	return text.replace(REF_RE, "<redacted>");
}

// ── Misc helpers ────────────────────────────────────────────────────────────

function describeError(err: unknown): string {
	return err instanceof Error ? err.message : String(err);
}

/** pi tool names must match ^[a-z0-9_]+$. */
function sanitizeName(raw: string): string {
	return raw
		.toLowerCase()
		.replace(/[^a-z0-9_]/g, "_")
		.replace(/^_+|_+$/g, "");
}

async function withTimeout<T>(label: string, ms: number, run: (signal: AbortSignal) => Promise<T>): Promise<T> {
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), ms);
	try {
		return await run(controller.signal);
	} catch (err) {
		if (controller.signal.aborted) {
			throw new Error(`${label}: timed out after ${Math.round(ms / 1000)}s`);
		}
		throw err;
	} finally {
		clearTimeout(timer);
	}
}

// ── Config loading ──────────────────────────────────────────────────────────

async function loadConfig(): Promise<{
	servers: Map<string, McpServerSpec>;
	path: string;
	parseError?: string;
	readError?: string;
}> {
	const path = join(process.env.HOME ?? "", ".pi", "agent", "mcp.json");
	const servers = new Map<string, McpServerSpec>();
	let raw: string;
	try {
		raw = await readFile(path, "utf8");
	} catch (err) {
		// ENOENT is the normal non-djinn case; other read failures are odd
		// enough to surface via /mcp.
		const code = (err as NodeJS.ErrnoException)?.code;
		if (code === "ENOENT") return { servers, path };
		return { servers, path, readError: describeError(err) };
	}
	let parsed: ConfigFile;
	try {
		parsed = JSON.parse(raw) as ConfigFile;
	} catch (err) {
		return { servers, path, parseError: describeError(err) };
	}
	const table = parsed?.mcpServers;
	if (table && typeof table === "object" && !Array.isArray(table)) {
		for (const [name, spec] of Object.entries(table)) {
			if (spec && typeof spec === "object" && !Array.isArray(spec)) {
				servers.set(name, spec as McpServerSpec);
			}
		}
	}
	return { servers, path };
}

// ── Connection ──────────────────────────────────────────────────────────────

interface Connection {
	client: Client;
	close: () => Promise<void>;
}

function isHttpSpec(spec: McpServerSpec): boolean {
	// djinn currently renders pi remotes as mcp-remote shims (stdio), but a
	// native {type: "http", url, headers} entry is the shape wiring would
	// switch to once the adapter makes native HTTP safe — support both,
	// keyed off the presence of url with no command.
	return !spec.command && typeof spec.url === "string" && spec.url.length > 0;
}

interface HttpOptions {
	headers: Record<string, string>;
}

function httpOptions(spec: McpServerSpec): HttpOptions {
	const headers: Record<string, string> = {};
	if (spec.headers && typeof spec.headers === "object" && !Array.isArray(spec.headers)) {
		for (const [k, v] of Object.entries(spec.headers as Record<string, unknown>)) {
			if (typeof v === "string") headers[k] = v;
		}
	}
	return { headers };
}

async function openConnection(spec: McpServerSpec): Promise<Connection> {
	if (isHttpSpec(spec)) return openHttp(spec);
	return openStdio(spec);
}

async function openStdio(spec: McpServerSpec): Promise<Connection> {
	const command = String(spec.command ?? "");
	if (!command) throw new Error("stdio server has no command");
	const args = Array.isArray(spec.args) ? (spec.args as unknown[]).map(String) : [];
	const extraEnv =
		spec.env && typeof spec.env === "object" && !Array.isArray(spec.env)
			? (spec.env as Record<string, unknown>)
			: {};
	// Full inherited env: mcp-remote shim entries expand their own ${VAR}
	// refs from the child's env, and stdio servers expect a normal process
	// environment. Entry env wins on conflict.
	const childEnv: Record<string, string> = {};
	for (const [k, v] of Object.entries(process.env)) {
		if (v !== undefined) childEnv[k] = v;
	}
	for (const [k, v] of Object.entries(extraEnv)) {
		if (typeof v === "string") childEnv[k] = v;
	}
	const transport = new StdioClientTransport({
		command,
		args,
		env: childEnv,
		stderr: "pipe",
	});
	const client = new Client({ name: CLIENT_NAME, version: CLIENT_VERSION });
	try {
		await client.connect(transport);
	} catch (err) {
		try {
			await transport.close();
		} catch {
			/* spawn already failed */
		}
		throw err;
	}
	return makeConnection(client);
}

async function openHttp(spec: McpServerSpec): Promise<Connection> {
	const url = String(spec.url ?? "");
	const { headers } = httpOptions(spec);
	const client = new Client({ name: CLIENT_NAME, version: CLIENT_VERSION });
	// Streamable HTTP first; fall back to legacy HTTP+SSE for older servers
	// (the SDK's documented probe order).
	try {
		const transport = new StreamableHTTPClientTransport(new URL(url), {
			requestInit: { headers },
		});
		try {
			await client.connect(transport);
			return makeConnection(client);
		} catch (streamableErr) {
			try {
				await transport.close();
			} catch {
				/* already gone */
			}
			const sse = new SSEClientTransport(new URL(url), {
				// EventSourceInit has no headers field in the DOM types, but the
				// SDK's SSE transport reads one at runtime (its documented pattern).
				eventSourceInit: { headers } as unknown as EventSourceInit,
				requestInit: { headers },
			});
			try {
				await client.connect(sse);
				return makeConnection(client);
			} catch (sseErr) {
				try {
					await sse.close();
				} catch {
					/* already gone */
				}
				throw new Error(
					`streamable HTTP: ${describeError(streamableErr)}; SSE fallback: ${describeError(sseErr)}`,
				);
			}
		}
	} catch (err) {
		throw err;
	}
}

function makeConnection(client: Client): Connection {
	return {
		client,
		close: async () => {
			try {
				await client.close();
			} catch {
				/* transport already gone */
			}
		},
	};
}

/** openConnection bounded by a timeout. A timed-out open whose underlying
 * connect later succeeds is closed instead of leaked: neither transport
 * accepts an abort signal, so the only way to bound a hung handshake is to
 * give up waiting and clean up whenever the open eventually settles. */
async function openConnectionBounded(spec: McpServerSpec, ms: number): Promise<Connection> {
	let aborted = false;
	const box: { conn?: Connection } = {};
	const opening = openConnection(spec)
		.then((conn) => {
			box.conn = conn;
			return conn;
		})
		.then((conn) => {
			if (aborted) {
				void conn.close();
				throw new Error("connect timed out");
			}
			return conn;
		});
	void opening.catch(() => {}); // no unhandled rejection if the timeout wins the race
	try {
		return await withTimeout("connect", ms, () => opening);
	} catch (err) {
		aborted = true;
		if (box.conn) await box.conn.close();
		throw err;
	}
}

/** Connect one server and list its tools. Never throws — every failure mode
 * comes back as {error}. */
async function connectServer(
	name: string,
	spec: McpServerSpec,
): Promise<{ connection?: Connection; tools: McpTool[]; error?: string }> {
	if (spec.command === undefined && spec.url === undefined) {
		return { tools: [], error: "entry has neither command nor url" };
	}
	// Expand refs up front so a missing secret fails fast with a precise
	// message instead of a confusing auth error from inside the server.
	const expanded = expandDeep({
		command: spec.command,
		args: spec.args ?? [],
		env: spec.env ?? {},
		url: spec.url ?? "",
		headers: spec.headers ?? {},
	});
	if (expanded.missing.length > 0) {
		const uniq = [...new Set(expanded.missing)];
		return {
			tools: [],
			error: `missing env var(s) ${uniq.join(", ")} — this container's pi.env may be incomplete`,
		};
	}
	const effSpec = expanded.value as McpServerSpec;

	let connection: Connection;
	try {
		connection = await openConnectionBounded(effSpec, CONNECT_TIMEOUT_MS);
	} catch (err) {
		return { tools: [], error: describeError(err) };
	}

	try {
		const result = await withTimeout(`tools/list ${name}`, LIST_TIMEOUT_MS, (signal) =>
			connection.client.listTools(undefined, { signal }),
		);
		const tools = Array.isArray(result?.tools) ? (result.tools as McpTool[]) : [];
		return { connection, tools };
	} catch (err) {
		await connection.close();
		return { tools: [], error: `tools/list failed: ${describeError(err)}` };
	}
}

// ── Tool result mapping ─────────────────────────────────────────────────────

interface McpContentBlock {
	type: string;
	text?: string;
	data?: string;
	mimeType?: string;
	uri?: string;
	name?: string;
	embeddedResource?: { resource?: { text?: string; uri?: string; mimeType?: string } };
}

interface McpToolResult {
	content?: McpContentBlock[];
	isError?: boolean;
	structuredContent?: unknown;
}

type PiContent = { type: "text"; text: string } | { type: "image"; data: string; mimeType: string };

interface PiToolResult {
	content: PiContent[];
	details: { server: string; tool: string };
	isError?: boolean;
}

function mapContent(blocks: McpContentBlock[] | undefined): PiContent[] {
	const out: PiContent[] = [];
	for (const block of blocks ?? []) {
		switch (block.type) {
			case "text":
				out.push({ type: "text", text: block.text ?? "" });
				break;
			case "image":
				if (typeof block.data === "string") {
					out.push({ type: "image", data: block.data, mimeType: block.mimeType ?? "image/png" });
				}
				break;
			case "resource": {
				const r = block.embeddedResource?.resource;
				if (r?.text !== undefined) {
					out.push({ type: "text", text: `${r.uri ? `${r.uri}\n` : ""}${r.text}` });
				} else {
					out.push({
						type: "text",
						text: `[resource ${r?.uri ?? block.uri ?? "(no uri)"} — binary ${r?.mimeType ?? "unknown"}, not rendered]`,
					});
				}
				break;
			}
			case "resource_link":
				out.push({ type: "text", text: `${block.uri ?? ""}${block.name ? ` (${block.name})` : ""}`.trim() });
				break;
			case "audio":
				out.push({ type: "text", text: `[audio ${block.mimeType ?? "unknown"} — pi cannot render audio]` });
				break;
			default:
				out.push({ type: "text", text: JSON.stringify(block, null, 2) });
		}
	}
	if (out.length === 0) out.push({ type: "text", text: "(empty result)" });
	return out;
}

function mapResult(result: McpToolResult, server: string, tool: string): PiToolResult {
	let content = mapContent(result.content);
	if ((!result.content || result.content.length === 0) && result.structuredContent !== undefined) {
		content = [{ type: "text", text: JSON.stringify(result.structuredContent, null, 2) }];
	}
	return {
		content,
		details: { server, tool },
		...(result.isError ? { isError: true } : {}),
	};
}

// ── Schema bridging ─────────────────────────────────────────────────────────

/** MCP tools carry plain JSON Schema; TypeBox validates against the same
 * dialect, so wrap rather than translate. TypeBox's checker ignores keywords
 * it does not know. */
function toPiSchema(inputSchema: unknown) {
	if (inputSchema && typeof inputSchema === "object" && !Array.isArray(inputSchema)) {
		return Type.Unsafe(inputSchema as Record<string, unknown>);
	}
	return Type.Object({});
}

// ── Extension ───────────────────────────────────────────────────────────────

export default function piMcpAdapter(pi: ExtensionAPI) {
	const servers = new Map<string, ServerState>();
	const registeredNames = new Set<string>();

	interface ServerState {
		spec: McpServerSpec;
		status: "connected" | "failed";
		tools: McpTool[];
		client?: Client;
		error?: string;
	}

	function closeAll(): Promise<void> {
		const jobs: Promise<void>[] = [];
		for (const state of servers.values()) {
			if (state.client) {
				const client = state.client;
				state.client = undefined;
				jobs.push(client.close().catch(() => {}));
			}
		}
		servers.clear();
		registeredNames.clear();
		return Promise.all(jobs).then(() => undefined);
	}

	function toolCount(): number {
		let n = 0;
		for (const s of servers.values()) n += s.tools.length;
		return n;
	}

	function registerTools(serverName: string, state: ServerState, client: Client) {
		const prefix = sanitizeName(serverName);
		if (!prefix) return; // Server name sanitizes to nothing — unusable.
		for (const tool of state.tools) {
			if (!tool?.name) continue;
			const base = `mcp_${prefix}_${sanitizeName(tool.name)}`;
			let name = base;
			let n = 2;
			while (registeredNames.has(name)) name = `${base}_${n++}`;
			registeredNames.add(name);
			const description = redactRefs(tool.description ?? `MCP tool ${tool.name} from ${serverName}`);
			pi.registerTool({
				name,
				label: `${tool.name} (${serverName})`,
				description,
				promptSnippet: description.slice(0, 120),
				parameters: toPiSchema(tool.inputSchema),
				async execute(
					_toolCallId: string,
					params: Record<string, unknown>,
					signal?: AbortSignal,
				) {
					const current = servers.get(serverName);
					if (!current || current.status !== "connected" || !current.client) {
						return {
							content: [
								{
									type: "text",
									text: `MCP server '${serverName}' is not connected (${current?.error ?? "not connected"}). Run /mcp for status.`,
								},
							],
							details: { server: serverName, tool: tool.name },
							isError: true,
						} satisfies PiToolResult;
					}
					let result: McpToolResult;
					try {
						result = (await client.callTool(
							{ name: tool.name, arguments: (params ?? {}) as Record<string, unknown> },
							undefined,
							{ signal, timeout: TOOL_CALL_TIMEOUT_MS },
						)) as McpToolResult;
					} catch (err) {
						return {
							content: [
								{ type: "text", text: `MCP call to ${serverName}/${tool.name} failed: ${describeError(err)}` },
							],
							details: { server: serverName, tool: tool.name },
							isError: true,
						} satisfies PiToolResult;
					}
					return mapResult(result, serverName, tool.name);
				},
			});
		}
	}

	async function connectAll(ui: { notify: (msg: string, level?: string) => void } | undefined): Promise<void> {
		const config = await loadConfig();
		if (config.servers.size === 0) {
			servers.clear();
			if (config.parseError && ui) {
				ui.notify(`pi-mcp-adapter: ${config.path} parse error — ${config.parseError}`, "error");
			}
			return;
		}
		await closeAll();
		const names = [...config.servers.keys()];
		await Promise.all(
			names.map(async (name) => {
				const spec = config.servers.get(name)!;
				const state: ServerState = { spec, status: "failed", tools: [] };
				servers.set(name, state);
				const { connection, tools, error } = await connectServer(name, spec);
				if (error || !connection) {
					state.error = error ?? "unknown error";
					return;
				}
				state.client = connection.client;
				state.status = "connected";
				state.tools = tools;
				registerTools(name, state, connection.client);
			}),
		);
		if (!ui) return;
		const failed = names.filter((n) => servers.get(n)?.status === "failed");
		let msg = `pi-mcp-adapter: ${servers.size - failed.length}/${servers.size} server(s), ${toolCount()} tool(s)`;
		if (failed.length > 0) {
			msg += ` — failed: ${failed.map((n) => `${n} (${redactRefs(servers.get(n)?.error ?? "?")})`).join("; ")} — see /mcp`;
		}
		ui.notify(msg, failed.length > 0 ? "warning" : "info");
	}

	pi.on("session_start", async (_event, ctx) => {
		try {
			await connectAll(ctx.hasUI ? ctx.ui : undefined);
		} catch (err) {
			// Never block pi startup over a broken MCP config.
			if (ctx.hasUI) {
				try {
					ctx.ui.notify(`pi-mcp-adapter: ${describeError(err)}`, "error");
				} catch {
					/* headless */
				}
			}
		}
	});

	pi.on("session_shutdown", async () => {
		await closeAll();
	});

	pi.registerCommand("mcp", {
		description: "Show MCP server status (pi-mcp-adapter)",
		handler: async (_args, ctx) => {
			const config = await loadConfig();
			const lines: string[] = [];
			if (config.parseError) lines.push(`${config.path}: parse error — ${config.parseError}`);
			if (config.readError) lines.push(`${config.path}: unreadable — ${config.readError}`);
			if (config.servers.size === 0 && !config.parseError && !config.readError) {
				lines.push(`${config.path}: no mcpServers configured`);
			}
			for (const [name, spec] of config.servers) {
				const state = servers.get(name);
				const shape = isHttpSpec(spec)
					? redactRefs(String(spec.url ?? ""))
					: `${String(spec.command ?? "?")} ${redactRefs((Array.isArray(spec.args) ? spec.args : []).join(" "))}`.trim();
				if (!state) {
					lines.push(`? ${name} — not connected yet (${shape})`);
				} else if (state.status === "connected") {
					lines.push(`✓ ${name} — ${state.tools.length} tool(s): ${state.tools.map((t) => t.name).join(", ")}`);
				} else {
					lines.push(`✗ ${name} — ${redactRefs(state.error ?? "failed")} (${shape})`);
				}
			}
			if (ctx.hasUI) ctx.ui.notify(lines.join("\n") || "pi-mcp-adapter: nothing configured", "info");
		},
	});
}
