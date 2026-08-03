// covers: voice-layer:contract-parity
//
// t266 — VOICE-LAYER PARITY + JARGON GATE. Mechanism: none (readFileSync over
// authored prose, zero spawn, zero LLM, zero tokens). Technique: deterministic
// closed predicate over the manifest-discovered harness matrix, so a new
// harness cannot escape the gate.
//
// WHY THIS EXISTS: the conductor's chat voice is prose, and prose drifts
// silently. Three failure modes this closes:
//
//   (a) The voice contract lives in ONE place (the stage protocol every harness
//       loads on every stage). If it is deleted or renamed, no other test
//       notices — the framework keeps working and just starts talking like a
//       framework again. §1 pins the section and its substance.
//   (b) The five authored SKILLs are near-copies maintained by hand. The
//       identity paragraph that points at the contract must be present and
//       WORD-IDENTICAL in all five: a partial edit (the t181 failure mode) would
//       leave some harnesses on the old framework-voice identity. §2 extracts the
//       shared core and compares it across the matrix.
//   (c) The retired framework-voice phrases must not creep back into the
//       user-visible surfaces this effort rewrote. §3 is a deny-list scoped to
//       exactly those authored files/sections — machine-facing uses of the same
//       words elsewhere (the engine's own comments, docs/, reference chapters)
//       stay legal by construction, because they are outside the scanned set.
//
// The gate reads the AUTHORED surfaces; dist is their byte-parity-guarded copy
// (t145 / package.ts --check), so gating the authored source covers every tree.

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { REPO_ROOT } from "../harness/fixtures.ts";
import { HARNESS_MATRIX } from "../harness/harness-matrix.ts";

const PROTOCOL_REL = "core/aidlc-common/protocols/stage-protocol.md";

function read(rel: string): string {
  return readFileSync(join(REPO_ROOT, rel), "utf-8");
}

function authoredSkills(): Array<{ name: string; rel: string }> {
  return HARNESS_MATRIX.map((harness) => ({
    name: harness.name,
    rel: `harness/${harness.name}/skills/aidlc/SKILL.md`,
  })).sort((a, b) => a.name.localeCompare(b.name));
}

// =========================================================================
// §1 — The voice contract exists, in the file every harness loads per stage.
// =========================================================================
describe("t266 §1 voice contract lives in the per-stage protocol", () => {
  const protocol = read(PROTOCOL_REL);

  test("the contract section is present as a real heading", () => {
    expect(/^### Talking to the user \(the voice contract\)$/m.test(protocol))
      .toBe(true);
  });

  // The contract is only useful if it still carries the two halves that make it
  // actionable: the reserved-word list, and the "wording not mechanics" bound
  // that keeps a voice edit from being read as permission to change behaviour.
  // Matched against a whitespace-flattened copy: the authored file hard-wraps,
  // so a phrase can straddle a newline without changing what it says.
  const flat = protocol.replace(/\s+/g, " ");
  const SUBSTANCE = [
    "Reserved internal vocabulary",
    "software developer building THEIR project",
    "never for chat narration",
  ] as const;
  for (const token of SUBSTANCE) {
    test(`the contract still carries: "${token}"`, () => {
      expect(flat).toContain(token);
    });
  }

  // Every reserved word must actually be listed, else a rewrite could quietly
  // shrink the deny-list the conductor is told to honour.
  const RESERVED = [
    "engine",
    "directive",
    "dispatch",
    "conductor",
    "harness",
    "scope grid",
    "steering",
    "swarm",
  ] as const;
  test("the reserved-vocabulary sentence names every reserved word", () => {
    const start = flat.indexOf("Reserved internal vocabulary");
    const sentence = flat.slice(start, start + 400);
    const missing = RESERVED.filter((w) => !sentence.includes(w));
    expect(missing).toEqual([]);
  });

  test("the contract states that mechanics are unchanged by it", () => {
    // The behaviour-safety clause: a voice rule must never be read as licence
    // to skip a step, rename an audit event, or paraphrase a tool's own output.
    expect(flat).toContain("governs the WORDS you say");
    expect(flat).toContain("VERBATIM");
  });
});

// =========================================================================
// §2 — All five authored SKILLs point at the contract, identically.
// =========================================================================
describe("t266 §2 the SKILL identity paragraph is shared verbatim", () => {
  const skills = authoredSkills();

  for (const { name, rel } of skills) {
    test(`${name}: SKILL.md points at the voice contract`, () => {
      const body = read(rel);
      expect(body).toContain("Talking to the user");
      expect(body).toContain("protocols/stage-protocol.md");
    });
  }

  test("the identity paragraph is byte-identical across every harness", () => {
    // The paragraph is authored once and copied; extract it by its stable
    // opening and compare. A partial edit shows up as a set with >1 member.
    const MARKER = "**Who you are to the user:";
    const paragraphs = new Map<string, string[]>();
    for (const { name, rel } of skills) {
      const body = read(rel);
      const start = body.indexOf(MARKER);
      expect(start, `${name} lacks the identity paragraph`).toBeGreaterThan(-1);
      const paragraph = body.slice(start).split("\n\n")[0];
      const seen = paragraphs.get(paragraph) ?? [];
      seen.push(name);
      paragraphs.set(paragraph, seen);
    }
    // One distinct paragraph text => every harness agrees.
    expect([...paragraphs.values()].map((v) => v.sort())).toHaveLength(1);
  });
});

// =========================================================================
// §3 — Jargon deny-list, scoped to the rewritten user-visible surfaces.
// =========================================================================
describe("t266 §3 retired framework-voice phrases stay out of user-visible prose", () => {
  // Scoped deliberately: these are the authored files whose PROSE the user
  // reads (skill identity/narration guidance, the per-stage protocol, the
  // onboarding doc + its per-harness fills). Machine-facing prose elsewhere
  // (engine source comments, docs/, reference chapters) is out of scope and may
  // keep using the precise internal names.
  function scannedFiles(): string[] {
    return [
      "core/templates/onboarding.md",
      ...HARNESS_MATRIX.map((h) => `harness/${h.name}/onboarding.fills.ts`),
    ].sort();
  }

  // Each phrase is a framework-voice tell the voice layer replaced. They are
  // matched case-insensitively so a capitalised reintroduction cannot slip by.
  const DENIED = [
    "orchestration engine",
    "auto-birth",
    "flag-precedence ladder",
  ] as const;

  for (const rel of scannedFiles()) {
    test(`${rel} carries no retired framework-voice phrase`, () => {
      const body = read(rel).toLowerCase();
      const hits = DENIED.filter((phrase) => body.includes(phrase.toLowerCase()));
      expect(hits).toEqual([]);
    });
  }

  // The protocol needs its own scan with ONE carve-out: the voice contract
  // quotes the retired phrases on purpose (it is the section that bans them), so
  // scanning it whole would forbid the ban from naming what it bans. Everything
  // AFTER the contract is user-visible template prose and is scanned normally.
  test("the protocol's user-visible templates carry no retired phrase", () => {
    const protocol = read(PROTOCOL_REL);
    const contractStart = protocol.indexOf(
      "### Talking to the user (the voice contract)",
    );
    const contractEnd = protocol.indexOf(
      "### Structured questions (harness-neutral contract)",
    );
    expect(contractStart).toBeGreaterThan(-1);
    expect(contractEnd).toBeGreaterThan(contractStart);
    // Template prose = everything outside the contract section.
    const scanned = (
      protocol.slice(0, contractStart) + protocol.slice(contractEnd)
    ).toLowerCase();
    // "orchestration engine" survives in ONE machine-facing paragraph
    // (per-unit iteration mechanics, not a user-facing template), so this scan
    // covers the two phrases with zero legitimate survivors.
    const hits = ["auto-birth", "flag-precedence ladder"].filter((p) =>
      scanned.includes(p),
    );
    expect(hits).toEqual([]);
  });

  // The 27-file stage boilerplate used to read "The engine owns all lifecycle
  // transitions and advancement" — a line an echoing model would narrate. It is
  // rephrased to instruct-and-do-not-narrate; this pins that it stays gone.
  test("no stage file reintroduces the narratable engine-owns boilerplate", () => {
    const { readdirSync, statSync } = require("node:fs") as typeof import("node:fs");
    const stagesRoot = join(REPO_ROOT, "core", "aidlc-common", "stages");
    const offenders: string[] = [];
    const walk = (dir: string): void => {
      for (const entry of readdirSync(dir)) {
        const abs = join(dir, entry);
        if (statSync(abs).isDirectory()) {
          walk(abs);
        } else if (entry.endsWith(".md")) {
          if (readFileSync(abs, "utf-8").includes(
            "The engine owns all lifecycle transitions and advancement.",
          )) {
            offenders.push(abs.slice(REPO_ROOT.length + 1));
          }
        }
      }
    };
    walk(stagesRoot);
    expect(offenders).toEqual([]);
  });
});
