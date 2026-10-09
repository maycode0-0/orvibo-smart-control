"""Exercise the actual GitHub Actions scripts with an in-memory GitHub API."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CURRENT_SHA = "a" * 40
RELEASE_SHA = "b" * 40
ASSET = {"id": 7, "name": "orvibo-smart-control.zip", "state": "uploaded", "size": 100}


def workflow_scripts(source: str) -> list[str]:
    """Read literal JS blocks without adding a YAML dependency to unit tests."""

    lines = source.splitlines()
    scripts = []
    for index, line in enumerate(lines):
        if line.strip() != "script: |":
            continue
        indentation = len(line) - len(line.lstrip())
        block = []
        for following in lines[index + 1 :]:
            if following.strip() and len(following) - len(following.lstrip()) <= indentation:
                break
            block.append(following)
        scripts.append(textwrap.dedent("\n".join(block)))
    return scripts


NODE_HARNESS = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const options = input.options;
const calls = [];
const outputs = {};
let ref = options.ref || null;
let release = options.release || null;
const error = status => Object.assign(new Error(`HTTP ${status}`), {status});
const github = {rest: {
  git: {
    async getRef(args) {
      if (options.refError) throw error(options.refError);
      if (!ref) throw error(404);
      return {data: ref};
    },
    async createRef(args) {
      calls.push({method: 'createRef', args});
      if (ref) throw error(422);
      ref = {object: {type: 'commit', sha: args.sha}};
      return {data: ref};
    },
    async getTag(args) {
      calls.push({method: 'getTag', args});
      const object = options.tagObjects?.[args.tag_sha];
      if (!object) throw error(404);
      return {data: {object}};
    },
  },
  repos: {
    async getReleaseByTag(args) {
      if (options.releaseError) throw error(options.releaseError);
      if (!release) throw error(404);
      return {data: release};
    },
    async createRelease(args) {
      calls.push({method: 'createRelease', args});
      release = {id: 42, assets: [], ...args};
      return {data: release};
    },
    async deleteReleaseAsset(args) {
      calls.push({method: 'deleteReleaseAsset', args});
      release.assets = release.assets.filter(asset => asset.id !== args.asset_id);
    },
    async uploadReleaseAsset(args) {
      calls.push({method: 'uploadReleaseAsset', args: {...args, data: args.data.toString()}});
      if (options.uploadError) throw error(options.uploadError);
      const asset = {id: 8, name: args.name, state: 'uploaded', size: args.data.length};
      release.assets.push(asset);
      return {data: asset};
    },
    async updateRelease(args) {
      calls.push({method: 'updateRelease', args});
      Object.assign(release, args);
      return {data: release};
    },
  },
}};
const context = {
  eventName: options.eventName || 'push',
  ref: options.eventRef || 'refs/heads/main',
  sha: options.sha,
  repo: {owner: 'owner', repo: 'repository'},
};
const core = {
  setOutput(name, value) {outputs[name] = value;},
  notice() {},
  setFailed(message) {throw new Error(message);},
};
const fakeRequire = name => {
  if (name !== 'fs') throw new Error(`Unexpected require: ${name}`);
  return {readFileSync(path) {
    if (path.endsWith('manifest.json')) return JSON.stringify({version: options.version});
    return Buffer.from('zip data');
  }};
};
const fakeProcess = {env: {
  RELEASE_TAG: `v${options.version}`,
  RELEASE_SHA: options.sourceSha || options.sha,
}};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
async function run(script) {
  await new AsyncFunction('github', 'context', 'core', 'require', 'process', script)(
    github, context, core, fakeRequire, fakeProcess,
  );
}
(async () => {
  let failure = null;
  try {
    if (input.mode !== 'publish') await run(input.scripts[0]);
    if (input.mode === 'release') fakeProcess.env.RELEASE_SHA = outputs.sha;
    if (input.mode === 'publish' || (input.mode === 'release' && outputs.publish === 'true')) {
      await run(input.scripts[1]);
    }
  } catch (exception) {failure = exception.message;}
  process.stdout.write(JSON.stringify({outputs, calls, release, ref, error: failure}));
})();
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is required to test Actions scripts")
class ReleaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (ROOT / ".github/workflows/hacs-release.yml").read_text(encoding="utf-8")
        cls.scripts = workflow_scripts(source)
        if len(cls.scripts) != 2:
            raise AssertionError("Expected tag resolution and release publication scripts")

    def run_workflow(self, mode="resolve", **options):
        options = {"version": "0.2.0", "sha": CURRENT_SHA, **options}
        result = subprocess.run(
            [shutil.which("node"), "-e", NODE_HARNESS],
            input=json.dumps({"scripts": self.scripts, "mode": mode, "options": options}),
            text=True,
            capture_output=True,
            check=True,
            timeout=15,
        )
        return json.loads(result.stdout)

    def commit_ref(self, sha=RELEASE_SHA):
        return {"object": {"type": "commit", "sha": sha}}

    def release(self, assets=None, **options):
        return {
            "id": 42,
            "target_commitish": RELEASE_SHA,
            "draft": False,
            "prerelease": False,
            "assets": [ASSET] if assets is None else assets,
            **options,
        }

    def methods(self, result):
        return [call["method"] for call in result["calls"]]

    def test_main_push_creates_tag_before_release_and_publishes_after_upload(self):
        result = self.run_workflow("release")
        self.assertIsNone(result["error"])
        self.assertEqual(
            self.methods(result),
            ["createRef", "createRelease", "uploadReleaseAsset", "updateRelease"],
        )
        self.assertEqual(result["calls"][0]["args"]["ref"], "refs/tags/v0.2.0")
        self.assertEqual(result["calls"][0]["args"]["sha"], CURRENT_SHA)
        self.assertTrue(result["calls"][1]["args"]["draft"])
        self.assertFalse(result["release"]["draft"])
        self.assertEqual(result["release"]["target_commitish"], CURRENT_SHA)

    def test_existing_tag_without_release_recovers_same_version(self):
        result = self.run_workflow("release", ref=self.commit_ref())
        self.assertIsNone(result["error"])
        self.assertNotIn("createRef", self.methods(result))
        self.assertEqual(result["outputs"]["sha"], RELEASE_SHA)
        self.assertEqual(result["release"]["target_commitish"], RELEASE_SHA)

    def test_complete_release_on_older_commit_skips_without_tag_conflict(self):
        result = self.run_workflow("release", ref=self.commit_ref(), release=self.release())
        self.assertIsNone(result["error"])
        self.assertEqual(result["outputs"]["publish"], "false")
        self.assertEqual(result["calls"], [])

    def test_nested_annotated_tag_resolves_to_release_commit(self):
        result = self.run_workflow(
            "release",
            ref={"object": {"type": "tag", "sha": "c" * 40}},
            tagObjects={
                "c" * 40: {"type": "tag", "sha": "d" * 40},
                "d" * 40: {"type": "commit", "sha": RELEASE_SHA},
            },
        )
        self.assertIsNone(result["error"])
        self.assertEqual(result["outputs"]["sha"], RELEASE_SHA)
        self.assertEqual(result["release"]["target_commitish"], RELEASE_SHA)

    def test_existing_release_without_zip_uploads_missing_asset(self):
        result = self.run_workflow(
            "release", ref=self.commit_ref(), release=self.release(assets=[])
        )
        self.assertIsNone(result["error"])
        self.assertEqual(self.methods(result), ["uploadReleaseAsset"])

    def test_incomplete_zip_is_replaced(self):
        for invalid in ({**ASSET, "size": 0}, {**ASSET, "state": "starter"}):
            with self.subTest(asset=invalid):
                result = self.run_workflow(
                    "release", ref=self.commit_ref(), release=self.release(assets=[invalid])
                )
                self.assertIsNone(result["error"])
                self.assertEqual(self.methods(result), ["deleteReleaseAsset", "uploadReleaseAsset"])

    def test_failed_upload_retries_even_when_next_push_has_same_version(self):
        failed = self.run_workflow("release", uploadError=503)
        self.assertEqual(failed["error"], "HTTP 503")
        self.assertTrue(failed["release"]["draft"])
        retried = self.run_workflow(
            "release", sha=RELEASE_SHA, ref=failed["ref"], release=failed["release"]
        )
        self.assertIsNone(retried["error"])
        self.assertEqual(retried["outputs"]["sha"], CURRENT_SHA)
        self.assertEqual(self.methods(retried), ["uploadReleaseAsset", "updateRelease"])
        self.assertFalse(retried["release"]["draft"])

    def test_uploaded_draft_is_published_without_reuploading(self):
        result = self.run_workflow(
            "release", ref=self.commit_ref(), release=self.release(draft=True)
        )
        self.assertIsNone(result["error"])
        self.assertEqual(self.methods(result), ["updateRelease"])
        self.assertFalse(result["release"]["draft"])

    def test_stable_tag_marked_prerelease_is_finalized(self):
        result = self.run_workflow(
            "release", ref=self.commit_ref(), release=self.release(prerelease=True)
        )
        self.assertIsNone(result["error"])
        self.assertFalse(result["release"]["prerelease"])
        self.assertEqual(self.methods(result), ["updateRelease"])

    def test_schedule_repairs_stable_release_without_creating_beta_tag(self):
        result = self.run_workflow("release", eventName="schedule")
        self.assertIsNone(result["error"])
        self.assertEqual(result["outputs"]["tag"], "v0.2.0")
        self.assertFalse(result["release"]["prerelease"])

    def test_manual_main_run_can_create_missing_tag(self):
        result = self.run_workflow("release", eventName="workflow_dispatch")
        self.assertIsNone(result["error"])
        self.assertIn("createRef", self.methods(result))

    def test_tag_push_must_match_manifest(self):
        result = self.run_workflow(eventRef="refs/tags/v0.1.9")
        self.assertIn("does not match manifest", result["error"])
        self.assertEqual(result["calls"], [])

    def test_matching_tag_push_publishes_without_recreating_tag(self):
        result = self.run_workflow(
            "release", eventRef="refs/tags/v0.2.0", ref=self.commit_ref(CURRENT_SHA)
        )
        self.assertIsNone(result["error"])
        self.assertNotIn("createRef", self.methods(result))

    def test_invalid_manifest_versions_fail_before_writes(self):
        for version in ("0.02.0", "0.2.00", "0.2", "0.2.0b1", "v0.2.0"):
            with self.subTest(version=version):
                result = self.run_workflow(version=version)
                self.assertIn("Invalid stable version", result["error"])
                self.assertEqual(result["calls"], [])

    def test_non_404_api_errors_do_not_create_tags(self):
        for error in ({"refError": 403}, {"releaseError": 503}):
            with self.subTest(error=error):
                result = self.run_workflow(**error)
                self.assertIsNotNone(result["error"])
                self.assertEqual(result["calls"], [])

    def test_retagging_during_packaging_aborts_upload(self):
        result = self.run_workflow("publish", ref=self.commit_ref(), sourceSha=CURRENT_SHA)
        self.assertIn("changed while packaging", result["error"])
        self.assertEqual(result["calls"], [])

    def test_deleted_tag_is_restored_at_original_release_commit(self):
        result = self.run_workflow("release", release=self.release())
        self.assertIsNone(result["error"])
        self.assertEqual(self.methods(result), ["createRef"])
        self.assertEqual(result["calls"][0]["args"]["sha"], RELEASE_SHA)
        self.assertEqual(result["outputs"]["publish"], "false")

    def test_deleted_tag_with_ambiguous_release_commit_is_not_rebound(self):
        result = self.run_workflow(release=self.release(target_commitish="main"))
        self.assertIn("Cannot safely restore", result["error"])
        self.assertEqual(result["calls"], [])


if __name__ == "__main__":
    unittest.main()
