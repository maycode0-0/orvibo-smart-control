"""Verify cards repair a dashboard rendered before their modules finish loading."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).parents[1]

HARNESS = r'''
const fs = require("fs"), vm = require("vm"), assert = require("assert/strict");
const scenario = process.argv[2] || "existing_error";
let type = process.argv[1].split(/[\\/]/).pop().replace(/\.js$/, "");
if (scenario === "temp_password") type = "orvibo-smart-control-temp-password-card";
let now = 0, sequence = 0;
const timers = new Map();
Date.now = () => now;
global.setTimeout = (callback, delay) => {
  const id = ++sequence; timers.set(id, {at: now + delay, callback}); return id;
};
global.clearTimeout = id => timers.delete(id);
function advance(target) {
  let checks = 0;
  while (timers.size) {
    const [id, timer] = [...timers].sort((a,b) => a[1].at - b[1].at)[0];
    if (timer.at > target) break;
    assert(++checks < 200, "unbounded startup timer");
    timers.delete(id); now = timer.at; timer.callback();
  }
  now = target;
}
class Node {
  constructor(name) { this.name = name; this.localName = name; this.tagName = name.toUpperCase(); this.textContent = ""; this.children = []; this.shadowRoot = null; }
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
  querySelectorAll(selector) {
    return this.children.flatMap(child => [
      ...(selector === "*" || child.name === selector ? [child] : []),
      ...child.querySelectorAll(selector),
    ]);
  }
  dispatchEvent(event) {
    this.event = event; events.push(event.type);
    if (scenario !== "persistent_error") this.children = [];
    if (scenario === "recreated_error") addError();
    return true;
  }
}
const events = [];
const documentRoot = new Node("document");
const host = new Node("home-assistant");
host.shadowRoot = new Node("shadow-root");
const panel = new Node("ha-panel-lovelace");
const lovelaceRoot = new Node("hui-root");
host.shadowRoot.children = [panel]; panel.shadowRoot = new Node("panel-shadow");
panel.shadowRoot.children = [lovelaceRoot]; documentRoot.children = [host];
if (["late_root", "no_root"].includes(scenario)) panel.shadowRoot.children = [];
function addError(message = `Custom element doesn't exist: ${type}.`) {
  const error = new Node("hui-error-card"); error.shadowRoot = new Node("error-shadow");
  error.shadowRoot.textContent = `配置错误 ${message} PRIVATE_CONFIG`;
  lovelaceRoot.children.push(error);
}
if (["existing_error", "late_root", "persistent_error", "recreated_error", "both_modules", "temp_password"].includes(scenario)) addError();
if (scenario === "legacy_state") addError();
if (scenario === "not_found") addError(`Custom element not found: ${type}.`);
if (scenario === "nested_error") {
  addError();
  const wrapper = new Node("hui-card"); wrapper.shadowRoot = new Node("wrapper-shadow");
  wrapper.shadowRoot.children = lovelaceRoot.children; lovelaceRoot.children = [wrapper];
}
if (scenario === "foreign_error") addError("Custom element doesn't exist: unrelated-card.");
if (["not_registered", "staggered_modules"].includes(scenario)) {
  type = type.includes("nvr") ? "orvibo-smart-control-door-lock-card" : "orvibo-smart-control-nvr-card";
  addError();
}
if (scenario === "invalid_config") addError(`entity 必须是 camera 实体 (${type})`);
global.document = documentRoot;
global.HTMLElement = class { attachShadow() { this.shadowRoot = new Node("card-shadow"); } };
const definitions = new Map();
global.customElements = { get: name => definitions.get(name), define: (name, cls) => definitions.set(name, cls) };
global.window = { customCards: [] };
if (scenario === "legacy_state") {
  window.__orviboSmartControlCardReady = {timer: setTimeout(() => assert.fail("legacy timer must be replaced"), 0)};
}
const source = fs.readFileSync(process.argv[1], "utf8");
vm.runInThisContext(source);
const otherPath = require("path").join(require("path").dirname(process.argv[1]),
  process.argv[1].includes("nvr-card") ? "orvibo-smart-control-door-lock-card.js" : "orvibo-smart-control-nvr-card.js");
if (scenario === "both_modules") {
  vm.runInThisContext(fs.readFileSync(otherPath, "utf8"));
  assert.equal(timers.size, 1, "modules must coalesce startup checks");
}
advance(0);
if (scenario === "late_root") panel.shadowRoot.children = [lovelaceRoot];
if (scenario === "late_error") addError();
if (scenario === "staggered_modules") {
  advance(35000);
  assert.deepEqual(events, [], "undefined element must not trigger recovery");
  vm.runInThisContext(fs.readFileSync(otherPath, "utf8"));
}
advance(60000);
if (scenario === "staggered_modules") advance(70000);
assert.equal(timers.size, 0, "startup check must terminate");
if (["healthy", "foreign_error", "invalid_config", "no_root", "not_registered"].includes(scenario)) {
  assert.deepEqual(events, [], "healthy/foreign/invalid cards must not reload the dashboard");
} else {
  assert.equal(definitions.size >= 1, true);
  assert.deepEqual(events, Array(scenario === "recreated_error" ? 3 : 1).fill("config-refresh"));
  if (!["persistent_error", "recreated_error"].includes(scenario)) assert.equal(lovelaceRoot.children.length, 0, "late error did not recover");
  assert.equal(lovelaceRoot.event.composed, true);
  assert.equal(lovelaceRoot.event.bubbles, true);
}
console.log(`${scenario} passed`);
'''


@unittest.skipUnless(shutil.which("node"), "Node.js required")
class CardResourceRaceTests(unittest.TestCase):
    def run_scenario(self, scenario, filenames=None):
        cards = ROOT / "custom_components/orvibo_smart_control/www"
        for filename in filenames or ("orvibo-smart-control-nvr-card.js", "orvibo-smart-control-door-lock-card.js"):
            with self.subTest(filename=filename):
                result = subprocess.run(
                    [shutil.which("node"), "-e", HARNESS, str(cards / filename), scenario],
                    text=True,
                    capture_output=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_loaded_module_rebuilds_existing_lovelace_root(self):
        self.run_scenario("existing_error")

    def test_dashboard_appearing_after_module_load_recovers_missing_card(self):
        self.run_scenario("late_root")

    def test_error_rendered_after_first_check_is_recovered(self):
        self.run_scenario("late_error")

    def test_healthy_dashboard_is_not_reloaded(self):
        self.run_scenario("healthy")

    def test_unrelated_missing_card_is_not_reloaded(self):
        self.run_scenario("foreign_error")

    def test_invalid_configuration_is_not_hidden_by_retries(self):
        self.run_scenario("invalid_config")

    def test_persistent_error_does_not_trigger_refresh_loop(self):
        self.run_scenario("persistent_error")

    def test_non_dashboard_page_stops_checking(self):
        self.run_scenario("no_root")

    def test_modules_share_one_startup_check_and_refresh(self):
        self.run_scenario("both_modules")

    def test_new_error_nodes_cannot_cause_unbounded_refreshes(self):
        self.run_scenario("recreated_error")

    def test_undefined_orvibo_element_is_not_refreshed(self):
        self.run_scenario("not_registered")

    def test_late_second_module_restarts_bounded_recovery(self):
        self.run_scenario("staggered_modules")

    def test_temporary_password_card_recovers(self):
        self.run_scenario("temp_password", ("orvibo-smart-control-door-lock-card.js",))

    def test_existing_single_notification_state_is_upgraded(self):
        self.run_scenario("legacy_state")

    def test_not_found_message_is_recognized(self):
        self.run_scenario("not_found")

    def test_error_inside_card_shadow_root_recovers(self):
        self.run_scenario("nested_error")

