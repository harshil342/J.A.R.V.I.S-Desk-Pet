const assert = require("node:assert");
const { describe, it } = require("node:test");

const {
  POLICIES,
  decide,
  describePolicy,
  policyFromArgv,
  policyOf,
  resolvePolicy,
  serializePolicy,
  validatePolicy,
} = require("../src/permissions/policy.js");

describe("approval policy", () => {
  it("has exactly three values", () => {
    assert.deepStrictEqual(POLICIES, ["ask", "review", "trust"]);
  });

  describe("policyOf - read-through, so nothing needs migrating", () => {
    it("prefers the new field", () => {
      assert.strictEqual(policyOf({ policy: "review", autoApprove: true }), "review");
    });
    it("falls back to the legacy boolean", () => {
      // A row written before this existed must still answer correctly.
      assert.strictEqual(policyOf({ autoApprove: true }), "trust");
      assert.strictEqual(policyOf({ autoApprove: false }), "ask");
    });
    it("defaults to ask", () => {
      assert.strictEqual(policyOf({}), "ask");
      assert.strictEqual(policyOf(undefined), "ask");
    });
    it("ignores a nonsense policy rather than trusting it", () => {
      assert.strictEqual(policyOf({ policy: "yolo", autoApprove: false }), "ask");
    });
  });

  describe("serializePolicy", () => {
    it("keeps the boolean as a view, never a second truth", () => {
      assert.deepStrictEqual(serializePolicy("trust"), { policy: "trust", autoApprove: true });
      assert.deepStrictEqual(serializePolicy("review"), { policy: "review", autoApprove: false });
    });
    it("round-trips through policyOf", () => {
      for (const p of POLICIES) {
        assert.strictEqual(policyOf(serializePolicy(p)), p);
      }
    });
    it("falls back rather than persisting nonsense", () => {
      assert.deepStrictEqual(serializePolicy("nonsense"), { policy: "ask", autoApprove: false });
    });
  });

  describe("old flags alias enum values, not booleans", () => {
    it("maps --auto to trust and --no-auto to ask", () => {
      assert.strictEqual(policyFromArgv(["--auto"]), "trust");
      assert.strictEqual(policyFromArgv(["--no-auto"]), "ask");
    });
    it("reads --policy=value and the two-entry form", () => {
      assert.strictEqual(policyFromArgv(["--policy=review"]), "review");
      assert.strictEqual(resolvePolicy({ argv: ["--policy", "trust"] }), "trust");
    });
    it("returns undefined when no policy flag is present", () => {
      assert.strictEqual(policyFromArgv(["--headless"]), undefined);
      assert.strictEqual(policyFromArgv([]), undefined);
    });
  });

  describe("validatePolicy fails loudly", () => {
    it("rejects an unknown value instead of defaulting it away", () => {
      assert.throws(() => validatePolicy("yolo"), /unknown policy/);
      assert.throws(() => validatePolicy(undefined), /unknown policy/);
    });
    it("normalises case and padding", () => {
      assert.strictEqual(validatePolicy(" Trust "), "trust");
    });
  });

  describe("resolvePolicy will not let the CLI clobber a deliberate choice", () => {
    it("an explicit --policy wins over what is stored", () => {
      assert.strictEqual(resolvePolicy({ argv: ["--policy=ask"] }, { policy: "trust" }), "ask");
    });
    it("no flag means the stored choice stands", () => {
      // The runner was started with no policy argument; resetting the owner's
      // dashboard choice here would be silent and wrong.
      assert.strictEqual(resolvePolicy({ argv: [] }, { policy: "review" }), "review");
      assert.strictEqual(resolvePolicy({}, { autoApprove: true }), "trust");
    });
    it("nothing anywhere means ask", () => {
      assert.strictEqual(resolvePolicy({}), "ask");
    });
  });

  describe("decide - trust is a policy, never a bypass", () => {
    it("still records under every policy", () => {
      // This is the whole module. A bypass would skip the record.
      for (const p of POLICIES) {
        assert.strictEqual(decide(p).record, true, `${p} must record`);
      }
    });
    it("trust skips the prompt but attributes the decision", () => {
      const d = decide("trust");
      assert.strictEqual(d.askOwner, false);
      assert.strictEqual(d.decidedBy, "trust");
    });
    it("review attributes to the reviewer, not to trust", () => {
      const d = decide("review");
      assert.strictEqual(d.askOwner, false);
      assert.strictEqual(d.decidedBy, "reviewer");
    });
    it("ask actually asks", () => {
      assert.strictEqual(decide("ask").askOwner, true);
    });
    it("an unknown policy behaves as ask, not as trust", () => {
      // Fail closed. Defaulting a typo to the permissive value would be the
      // worst possible place to be clever.
      assert.strictEqual(decide("nonsense").askOwner, true);
      assert.strictEqual(policyOf({ policy: "nonsense" }), "ask");
    });
  });

  describe("describePolicy is honest about trust", () => {
    it("says trust is still recorded", () => {
      assert.match(describePolicy("trust"), /record/i);
    });
    it("describes all three", () => {
      for (const p of POLICIES) {
        assert.ok(describePolicy(p).length > 5, `${p} needs a description`);
      }
    });
  });
});
