"""Verify cards repair a dashboard rendered before their modules finish loading."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).parents[1]

HARNESS = r'''
const fs = require("fs"), vm = require("vm"), assert = require("assert/strict");
class Node {
  constructor(name) { this.name = name; this.children = []; this.shadowRoot = null; }
  querySelector(selector) {
    if (selector === "hui-root") {
      if (this.name === "hui-root") return this;
      for (const child of this.children) {
        const found = child.querySelector(selector);
        if (found) return found;
      }
    }
    return null;
  }
  querySelectorAll() { return this.children; }
  dispatchEvent(event) { this.event = event; events.push(event.type); return true; }
}
const events = [];
const documentRoot = new Node("document");
const host = new Node("home-assistant");
host.shadowRoot = new Node("shadow-root");
const panel = new Node("ha-panel-lovelace");
const lovelaceRoot = new Node("hui-root");
host.shadowRoot.children = [panel]; panel.shadowRoot = new Node("panel-shadow");
panel.shadowRoot.children = [lovelaceRoot]; documentRoot.children = [host];
global.document = documentRoot;
global.HTMLElement = class { attachShadow() { this.shadowRoot = new Node("card-shadow"); } };
const definitions = new Map();
global.customElements = { get: name => definitions.get(name), define: (name, cls) => definitions.set(name, cls) };
global.window = { customCards: [] };
const source = fs.readFileSync(process.argv[1], "utf8");
vm.runInThisContext(source);
setTimeout(() => {
  assert.equal(definitions.size >= 1, true);
  assert.deepEqual(events, ["config-refresh"]);
  assert.equal(lovelaceRoot.event.composed, true);
  assert.equal(lovelaceRoot.event.bubbles, true);
  console.log("custom card resource race refresh passed");
}, 10);
'''


class CardResourceRaceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_loaded_module_rebuilds_existing_lovelace_root(self):
        cards = ROOT / "custom_components/orvibo_smart_control/www"
        for filename in ("orvibo-smart-control-nvr-card.js", "orvibo-smart-control-door-lock-card.js"):
            with self.subTest(filename=filename):
                result = subprocess.run(
                    [shutil.which("node"), "-e", HARNESS, str(cards / filename)],
                    text=True,
                    capture_output=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

