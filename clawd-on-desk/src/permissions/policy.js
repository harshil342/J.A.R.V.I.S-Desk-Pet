/**
 * policy.js — the approval policy, as three values instead of a boolean.
 *
 * Adapted from Perry (https://github.com/TheM1N9/perry), MIT licensed,
 * Copyright (c) 2026 The Perry contributors, from `convex/runner.ts` and
 * `runner/engines/claude.ts`.
 *
 * Why not a boolean
 * -----------------
 * The setting used to be `autoApprove: true | false`. "Ask" and "review the
 * request first and only then run it" are both non-default, and collapsing them
 * into `false` loses the difference between supervision and delegation. A
 * boolean is also unrecoverable: once a turn is running with everything
 * approved, you cannot take it back mid-turn.
 *
 * The rule this file exists to enforce:
 *
 *   `trust` is a policy that still writes an audit row. It is never a bypass
 *   that routes around the gate.
 *
 * Perry puts the reasoning better than I can, in `claude.ts`:
 *   "Full is not bypassPermissions, which could not be taken back mid-turn and
 *    never consults canUseTool: it accepts edits, and canUseTool allows the
 *    rest without asking."
 *
 * Migration, deliberately not a migration
 * ---------------------------------------
 * The new field is optional and the old boolean is kept in sync as a
 * denormalised view. That means no migration, no downtime, and no dual-write
 * requirement at read time - `policyOf` derives the answer from whichever is
 * present. A future change can drop `autoApprove` in one step if it wants.
 */

const POLICIES = Object.freeze(["ask", "review", "trust"]);

const DEFAULTS = Object.freeze({
  /** The machine's default when nothing has ever been chosen. */
  default: "ask",
  /** A per-chat override that has not been set. */
  fallback: "ask",
});

/**
 * The effective policy.
 *
 * Read-through, so a row written before this existed still answers correctly.
 * @param {{policy?: string, autoApprove?: boolean}} [settings]
 * @returns {"ask"|"review"|"trust"}
 */
function policyOf(settings) {
  const s = settings ?? {};
  if (POLICIES.includes(s.policy)) return s.policy;
  // The legacy boolean, still honoured for anything that only set it.
  if (typeof s.autoApprove === "boolean") return s.autoApprove ? "trust" : "ask";
  return DEFAULTS.default;
}

/**
 * The pair to persist, so the legacy boolean stays a view and never a second
 * source of truth. `policy` is authoritative; `autoApprove` exists only so
 * older code and older rows keep working.
 */
function serializePolicy(policy) {
  const p = POLICIES.includes(policy) ? policy : DEFAULTS.default;
  return { policy: p, autoApprove: p === "trust" };
}

/**
 * Old CLI flags become aliases for enum **values**, not for booleans.
 *
 * `--auto` means "trust", not "the old truthy thing". That is what makes the
 * migration safe: there is no way to express the old intent ambiguously.
 * @returns {string|undefined} the policy requested, or undefined if none was
 */
function policyFromArgv(argv) {
  for (const arg of argv ?? []) {
    if (arg === "--auto") return "trust";
    if (arg === "--no-auto") return "ask";
    if (arg.startsWith("--policy=")) return validatePolicy(arg.slice("--policy=".length));
    if (arg === "--policy") return undefined; // the next argv entry carries it
  }
  return undefined;
}

/**
 * @throws if not one of the three
 * @returns {"ask"|"review"|"trust"}
 */
function validatePolicy(value) {
  const v = String(value ?? "").trim().toLowerCase();
  if (!POLICIES.includes(v)) {
    throw new Error(`unknown policy ${JSON.stringify(value)}; expected one of ${POLICIES.join(", ")}`);
  }
  return v;
}

/**
 * Resolve what to store when the owner states a preference, without letting a
 * command line silently override a choice made in the UI.
 *
 * Precedence: an explicit --policy wins, then an explicit --auto/--no-auto,
 * then whatever is already stored, then the default. The point of the last two
 * is that a runner started with no policy argument must not reset a choice the
 * owner made deliberately somewhere else.
 *
 * @param {{policy?: string, argv?: string[]}} args
 * @param {{policy?: string, autoApprove?: boolean}} [stored]
 * @returns {"ask"|"review"|"trust"}
 */
function resolvePolicy(args, stored) {
  const argv = args?.argv ?? [];
  const fromArgv = policyFromArgv(argv);

  if (fromArgv) return fromArgv;

  // `--policy ask` as two argv entries.
  const idx = argv.indexOf("--policy");
  if (idx >= 0 && idx + 1 < argv.length) return validatePolicy(argv[idx + 1]);

  if (args?.policy) return validatePolicy(args.policy);
  return policyOf(stored);
}

/**
 * What a policy means in words, for the settings UI and for the audit log.
 * A permission the user cannot read is a permission they cannot trust.
 */
function describePolicy(policy) {
  const p = POLICIES.includes(policy) ? policy : DEFAULTS.default;
  if (p === "trust") {
    // Deliberately says it is still recorded.
    return "run without asking, but record every request in the audit log";
  }
  if (p === "review") return "decide each request automatically, then record it";
  return "ask before each request";
}

/**
 * Whether a policy is allowed to run a request without asking the owner.
 *
 * Note what this does NOT do: it does not bypass the gate. A `trust` request
 * still flows through the matcher and still produces an audit row; only the
 * interactive prompt is skipped. That is the distinction the whole module is
 * built around, and it is why `trust` is a value here rather than a bypass
 * flag elsewhere.
 *
 * @returns {{askOwner: boolean, record: true, decidedBy: string}}
 */
function decide(policy) {
  const p = POLICIES.includes(policy) ? policy : DEFAULTS.default;
  if (p === "trust") return { askOwner: false, record: true, decidedBy: "trust" };
  if (p === "review") return { askOwner: false, record: true, decidedBy: "reviewer" };
  return { askOwner: true, record: true, decidedBy: "terminal" };
}

module.exports = {
  POLICIES,
  DEFAULTS,
  policyOf,
  serializePolicy,
  policyFromArgv,
  validatePolicy,
  resolvePolicy,
  describePolicy,
  decide,
};
