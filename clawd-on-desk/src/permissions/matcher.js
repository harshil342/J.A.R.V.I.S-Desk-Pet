/**
 * matcher.js â€” the permission matcher.
 *
 * This is the security boundary, so it is a standalone module with **no
 * Electron imports and no I/O**. It is a pure function over strings, which
 * means it is testable without spawning a window and callable from the Python
 * side over IPC if we ever want one owner in two processes.
 *
 * Adapted from Perry (https://github.com/TheM1N9/perry), MIT licensed,
 * Copyright (c) 2026 The Perry contributors, from `convex/approvals.ts`.
 * Perry is in turn built on ideas from vercel/eve (Apache-2.0). The logic below
 * is a JavaScript port with the comments kept, because the comments explain
 * *why* and that is the part worth carrying.
 *
 * The one rule everything else serves:
 *
 *   A prefix rule allows a command and nothing chained onto it.
 *
 * "git status" must never also permit "git status; Remove-Item -Recurse ~".
 */

/**
 * Anything that would chain a second command onto an allowed prefix.
 * Covers PowerShell's `;` and call operator `&`, pipes, redirection, the
 * backtick escape, newlines, and `$(` subexpression.
 */
const CHAINED = /[;&|`<>\r\n]|\$\(/;

/**
 * The command inside a shell wrapper.
 *
 * A coding agent on Windows asks about `"...\powershell.exe" -Command "git
 * status"`, but a prefix rule is about the `git status` part - otherwise the
 * stored rule is nonsense and matches nothing next time.
 *
 * Three wrappers, tried in order: PowerShell `-Command`, `cmd /c`, `sh -c`.
 * Details that matter:
 *   - `(?:\.exe)?` optional, so pwsh and pwsh.exe both match
 *   - quoted paths, so `"C:\â€¦\powershell.exe"` matches
 *   - `(?:\-\w+\s+)*?` skips interleaved flags like -NoProfile -NonInteractive
 *     and is lazy, so it stops at the *first* -Command
 *   - one layer of outer quotes is stripped afterwards
 *
 * @param {string} command
 * @returns {string} the inner command, or the input trimmed if unwrapped
 */
function innerCommand(command) {
  const trimmed = String(command ?? "").trim();
  const inner =
    /^(?:"[^"]*(?:powershell|pwsh)(?:\.exe)?"|\S*(?:powershell|pwsh)(?:\.exe)?)\s+(?:-\w+\s+)*?-Command\s+([\s\S]+)$/i.exec(trimmed)?.[1] ??
    /^(?:"[^"]*cmd(?:\.exe)?"|\S*cmd(?:\.exe)?)\s+(?:\/\w\s+)*?\/c\s+([\s\S]+)$/i.exec(trimmed)?.[1] ??
    /^(?:\S*\/)?(?:ba|z)?sh\s+-l?c\s+([\s\S]+)$/.exec(trimmed)?.[1];
  if (!inner) return trimmed;
  const quoted = /^(["'])([\s\S]*)\1$/.exec(inner.trim());
  return quoted ? quoted[2] : inner.trim();
}

/**
 * Does `command` start with `prefix`, with nothing chained after it?
 *
 * Two conditions, and both matter:
 *   1. exact equality, or the prefix followed by a literal space â€” so a rule for
 *      `git log` does not also allow `git logall`
 *   2. only the text *after* the prefix is tested against CHAINED, so a
 *      metacharacter inside the allowed prefix itself is not what blocks it;
 *      only chaining past the end of the granted span does
 */
function startsWithCommand(command, prefix) {
  const inner = innerCommand(command);
  if (inner !== prefix && !inner.startsWith(`${prefix} `)) return false;
  return !CHAINED.test(inner.slice(prefix.length));
}

/**
 * The prefix an engine claims is safe to allow from now on, if it really is one.
 *
 * The engine's own `proposedExecpolicyAmendment` is a **hint, never trusted**.
 * Three gates: non-empty, not itself chained, and provably a prefix of the
 * unwrapped inner command. A proposal equal to the whole command is rejected -
 * storing that as a "prefix" rule would lie to the UI about what it permits.
 *
 * @param {string} command
 * @param {string[]} [amendment] the engine's proposal, if it made one
 */
function suggestedPrefix(command, amendment) {
  const prefix = Array.isArray(amendment) ? amendment.join(" ").trim() : "";
  if (!prefix || CHAINED.test(prefix)) return undefined;
  const inner = innerCommand(command);
  return inner !== prefix && startsWithCommand(command, prefix) ? prefix : undefined;
}

/** A command this long is not a prefix worth remembering. */
const MAX_PRECISE_COMMAND = 4000;

/**
 * Compare paths the way the owner's filesystem does.
 *
 * Windows ignores case and slash direction; POSIX does not ignore case, and
 * lowercasing there would make two genuinely different paths compare equal.
 */
function normalPath(p) {
  const slashed = String(p ?? "").replace(/\\/g, "/").replace(/\/+$/, "");
  return /^[a-z]:(\/|$)/i.test(slashed) || slashed.startsWith("//") ? slashed.toLowerCase() : slashed;
}

/**
 * Is `p` inside `folder`?
 *
 * The trailing slash matters: without it `C:/Users/me2` would be "inside"
 * `C:/Users/me`, which is a real folder and not ours.
 */
function inside(p, folder) {
  const child = normalPath(p);
  const parent = normalPath(folder);
  return child === parent || child.startsWith(`${parent}/`);
}

/**
 * The deepest folder holding every path - unless that is too broad to be safe.
 *
 * Two files in unrelated places must never mint a rule for their common ancestor:
 * `C:\Users\me\a.txt` and `C:\Users\me2\b.txt` share only `C:\Users`, and a rule
 * for that is an enormous grant. Hence the depth check.
 *
 * @param {string[]} paths
 * @returns {string|undefined}
 */
function commonFolder(paths) {
  const list = (paths ?? []).filter(Boolean);
  if (list.length === 0) return undefined;
  const windows = /^[a-z]:[\\/]/i.test(list[0]);
  const same = (a, b) => (windows ? a.toLowerCase() === b.toLowerCase() : a === b);
  let shared = list[0].replace(/\\/g, "/").split("/").slice(0, -1);
  for (const p of list.slice(1)) {
    const parts = p.replace(/\\/g, "/").split("/").slice(0, -1);
    let i = 0;
    while (i < shared.length && i < parts.length && same(shared[i], parts[i])) i++;
    shared = shared.slice(0, i);
  }
  // `C:/Users/me` is three parts, and so is `/home/me` with its empty root.
  if (shared.length < 3) return undefined;
  const folder = shared.join("/");
  return windows ? folder.replace(/\//g, "\\") : folder;
}

/**
 * What a rule allows, in words.
 *
 * A permission system whose grants cannot be read in prose is one nobody trusts.
 * @param {{command?: string, prefix?: boolean, pathPrefix?: string}} rule
 * @param {string} [cwd]
 */
function describeRule(rule, cwd) {
  if (!rule) return "nothing";
  if (rule.pathPrefix) return `file changes under ${rule.pathPrefix}`;
  const where = cwd ? ` in ${cwd}` : "";
  return rule.prefix ? `commands starting with "${rule.command}"${where}` : `this exact command${where}`;
}

/**
 * Does a stored rule cover this request?
 *
 * Note the asymmetry, which is deliberate: a prefix rule matches by prefix for
 * **commands**, but a file rule requires **every** path to be inside the folder.
 * Broadening a file rule silently would be exactly the bug this avoids.
 *
 * @param {{kind: string, command?: string, prefix?: boolean, cwd?: string, pathPrefix?: string}} rule
 * @param {{kind: string, title?: string, cwd?: string, paths?: string[]}} request
 */
function ruleMatches(rule, request) {
  if (!rule || !request) return false;
  if (request.kind === "command") {
    if (rule.kind !== "command" || !rule.command) return false;
    if (rule.cwd && !(request.cwd && inside(request.cwd, rule.cwd))) return false;
    return rule.prefix
      ? startsWithCommand(request.title ?? "", rule.command)
      : (request.title ?? "") === rule.command;
  }
  const folder = rule.pathPrefix;
  return (
    rule.kind === "file" &&
    Boolean(folder) &&
    Array.isArray(request.paths) &&
    request.paths.length > 0 &&
    request.paths.every((p) => inside(p, folder))
  );
}

/**
 * Commands that are never a good idea from an agent, regardless of approval.
 *
 * This is a backstop, not the security model. A denylist cannot catch every
 * dangerous command and does not try to - the approval prompt is the real
 * control. This only removes the most obviously catastrophic typos, so that
 * "yes" to something catastrophic is at least a deliberate act.
 *
 * The `[^|;&]*` guards stop a chained *benign* command from tripping a
 * dangerous-looking rule.
 */
const DENY = [
  { pattern: /\brm\s+(-[a-zA-Z]*\s+)*(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\b[^|;&]*\s\/(?:\s|$)/, why: "rm -rf on the filesystem root" },
  { pattern: /\bmkfs(\.\w+)?\b/, why: "formatting a filesystem" },
  { pattern: /\bdd\b[^|;&]*\bof=\/dev\//, why: "writing directly to a device" },
  { pattern: /\bRemove-Item\b[^|;&]*-[-a-zA-Z]*r[a-zA-Z]*\b[^|;&]*\b[A-Za-z]:\\?\s*$/, why: "recursive delete of a drive root" },
  { pattern: /\bformat\s+[a-z]:/i, why: "formatting a drive" },
  { pattern: /\b(shutdown|halt|poweroff)\b[^|;&]*\/[a-z]/i, why: "shutting the machine down" },
  { pattern: /\bgit\s+(?:config|push)[^|;&]*--global\b/, why: "changing global git config" },
  { pattern: /\bcurl\b[^|;&]*\s\|\s*(?:ba|z)?sh\b/i, why: "downloading and running code in one step" },
  { pattern: /\bInvoke-(?:WebRequest|Expression)\b[^|;&]*\|\s*(?:Invoke-Expression|iex)\b/i, why: "downloading and running code in one step" },
  { pattern: /:(?:\(\)|\{)\s*(?:fromcharcode|iex)\b/i, why: "PowerShell obfuscated execution" },
];

/** @returns {{blocked: boolean, why?: string}} */
function denied(command) {
  const inner = innerCommand(command);
  for (const rule of DENY) {
    if (rule.pattern.test(inner)) return { blocked: true, why: rule.why };
  }
  return { blocked: false };
}

module.exports = {
  CHAINED,
  innerCommand,
  startsWithCommand,
  suggestedPrefix,
  MAX_PRECISE_COMMAND,
  normalPath,
  inside,
  commonFolder,
  describeRule,
  ruleMatches,
  DENY,
  denied,
};
